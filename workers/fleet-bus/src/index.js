// fleet-bus — the fleet message bus, on Durable Objects.
//
// Replaces the Valkey container on the mini (robogeosociety/infra#26). The bus
// contract is unchanged — envelope `{v,ts,src,topic,type,data}`, the two delivery
// classes, the same topic names — so this is a transport swap, not a redesign.
// `supervisor/bus_contract.py` stays canonical for the schema; src/contract.js
// mirrors the routing facts the Worker needs.
//
// Shape:
//   POST /publish          one envelope; routed to its topic's DO by name
//   GET  /retained/:topic  the retained last-value, or 404 once its TTL lapses
//   GET  /stream/:name     range-scan an event stream (?since=&limit=)
//   GET  /stat             per-topic health, for the #ops digest
//
// Telemetry goes to the SAME Analytics Engine dataset the host lane already uses
// (`host_vitals`), not a parallel store. That is what lets the existing alerting
// see bus liveness without a second query path: the fourth vitals signal in
// cicd-collector reads `bus.<topic>` rows exactly the way the `silent` signal
// reads Vector's.
//
// Auth mirrors host-vitals: a shared bearer token, checked with a timing-safe
// compare. The mini is the only publisher.
import { TopicDO } from "./topic.js";
import { QueueDO } from "./queue.js";
import { CATALOG, specFor, validateEnvelope } from "./contract.js";
import { publishDigest } from "./digest.js";

export { TopicDO, QueueDO };

/** Per-topic health, shared by GET /stat and the cron digest. */
async function collectStat(env) {
  const out = {};
  for (const [topic, spec] of Object.entries(CATALOG)) {
    out[topic] =
      spec.cls === "work"
        ? { cls: "work", ...(await env.QUEUE.getByName(topic).stat()) }
        : { cls: spec.cls, ...(await env.TOPIC.getByName(topic).stat(topic)) };
  }
  return out;
}

// Analytics Engine permits 25 writeDataPoint() calls per invocation and THROWS
// past it — the failure host-vitals already hit and documented. One publish is one
// invocation and writes one point, so we are far inside the limit; the guard is
// here so a future batch endpoint cannot quietly reintroduce that 500.
const MAX_AE_WRITES = 25;

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function authorized(req, env) {
  if (!env.BUS_TOKEN) return false; // fail closed when unconfigured
  const got = req.headers.get("authorization") || "";
  return timingSafeEqual(got, `Bearer ${env.BUS_TOKEN}`);
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

/**
 * Mirror a publish into `host_vitals` so the existing lane can see the bus.
 *
 * Column map is positional and shared with host-vitals (workers/host-vitals/
 * README.md) — do not guess it:
 *   blobs   [host, metric, tags, collector]
 *   doubles [value, source epoch seconds]
 *   indexes [host]
 *
 * `value` is the publish age in seconds, which is 0 at publish time. That looks
 * redundant but is the point: the freshness question the alert asks is "how long
 * since the newest row", and encoding the metric as `bus.<topic>` lets the fourth
 * signal reuse the `silent` query verbatim against a different metric name.
 */
function writeVitals(env, envelope, writes) {
  if (!env.VITALS || writes.n >= MAX_AE_WRITES) return;
  const host = env.BUS_HOST || "Tommys-Mac-mini";
  env.VITALS.writeDataPoint({
    blobs: [host, `bus.${envelope.topic}`, `src=${envelope.src},type=${envelope.type}`, "bus"],
    doubles: [0, Math.floor(envelope.ts)],
    indexes: [host],
  });
  writes.n += 1;
}

async function handlePublish(req, env) {
  let envelope;
  try {
    envelope = await req.json();
  } catch {
    return json({ error: "body is not JSON" }, 400);
  }

  const spec = specFor(envelope?.topic);
  if (!spec) return json({ error: `unknown topic ${envelope?.topic}` }, 404);

  const problems = validateEnvelope(envelope, spec);
  if (problems.length) return json({ error: "invalid envelope", problems }, 422);

  const writes = { n: 0 };
  let result;

  if (spec.cls === "work") {
    // Durable queue, its own DO class — still addressed by topic name, so routing
    // stays uniform even though the behaviour differs.
    const q = env.QUEUE.getByName(envelope.topic);
    // The handler route travels with the job so the queue stays generic.
    const job = spec.handler
      ? { ...envelope, data: { ...envelope.data, handler: spec.handler } }
      : envelope;
    result = await q.enqueue(job);
  } else if (spec.cls === "telemetry") {
    result = await env.TOPIC.getByName(envelope.topic).publishRetained(
      envelope.topic,
      envelope,
      spec.ttl,
    );
  } else {
    result = await env.TOPIC.getByName(envelope.topic).publishEvent(envelope.topic, envelope);
  }

  writeVitals(env, envelope, writes);
  return json({ ok: true, topic: envelope.topic, cls: spec.cls, ...result });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/health") return json({ ok: true, service: "fleet-bus" });

    if (!authorized(req, env)) return json({ error: "unauthorized" }, 401);

    if (req.method === "POST" && path === "/publish") return handlePublish(req, env);

    if (req.method === "GET" && path.startsWith("/retained/")) {
      const topic = decodeURIComponent(path.slice("/retained/".length));
      if (!specFor(topic)) return json({ error: `unknown topic ${topic}` }, 404);
      const got = await env.TOPIC.getByName(topic).retained();
      return got ? json(got) : json({ error: "no live retained value" }, 404);
    }

    if (req.method === "GET" && path.startsWith("/stream/")) {
      const name = decodeURIComponent(path.slice("/stream/".length));
      // A stream is declared by the topic that writes it, so resolve name → topic
      // rather than letting callers address DOs by an unvalidated string.
      const topic = Object.entries(CATALOG).find(([, s]) => s.stream === name)?.[0];
      if (!topic) return json({ error: `unknown stream ${name}` }, 404);
      const since = Number(url.searchParams.get("since") || 0);
      const limit = Number(url.searchParams.get("limit") || 100);
      return json({ stream: name, events: await env.TOPIC.getByName(topic).readStream(since, limit) });
    }

    if (req.method === "GET" && path === "/stat") return json({ topics: await collectStat(env) });

    // Manual digest trigger, so the rollup can be exercised without waiting for
    // cron — the same entry point the scheduled handler uses.
    if (req.method === "POST" && path === "/digest") {
      return json(await publishDigest(env, await collectStat(env)));
    }

    return json({ error: "not found" }, 404);
  },

  /** Cron — the level-triggered half of #ops. Alarms cover the edges. */
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        await publishDigest(env, await collectStat(env));
      })(),
    );
  },
};
