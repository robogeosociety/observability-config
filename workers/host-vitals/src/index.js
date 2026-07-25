// host-vitals — ingest Worker for the mini's Vector agent (host telemetry
// after TIG retirement, observability-config#156; Tommy's 2026-07-22 ruling:
// "vector it is, pushing to native cloudflare off the box").
//
// Vector (supervisor repo, ops/vector/vector.toml) POSTs http-sink batches
// here. The wire format — captured from a live vector 0.57.0 run, not guessed:
//   • body: newline-delimited JSON (one event per line), gzip-compressed
//     (content-encoding: gzip; the sink sets no content-type header)
//   • each line is the native_json codec's tagged union:
//       {"metric": {name, namespace, tags{...}, timestamp, kind,
//                   gauge:{value}|counter:{value}}}          — host_metrics
//       {"log":    {type, serial_number, hub_sn, obs|ob, host, stream,
//                   source_type, timestamp, ...}}            — Tempest UDP
//
// Routing:
//   metric               → VITALS  (dataset host_vitals)
//   log type=obs_st      → WEATHER (dataset weather_obs), one row per obs row
//   log type=rapid_wind  → WEATHER (dataset weather_obs)
//   anything else        → counted in `skipped`, never an error
//
// Analytics Engine stamps rows at write time; the source timestamp therefore
// rides in a double (host_vitals double2, weather_obs double1) — query on
// that when Vector has been buffering, not on the row timestamp.
//
// Column maps (also in README.md):
//   host_vitals   blob1=host  blob2=metric name  blob3=tags collapsed
//                 ("k=v,k=v", sorted, host/collector dropped)  blob4=collector
//                 double1=value  double2=source epoch secs     index1=host
//   weather_obs   blob1=type  blob2=serial_number  blob3=raw obs row (JSON)
//                 blob4=host  double1..N=the obs array verbatim (obs_st: 18
//                 values, [0]=epoch; rapid_wind ob: [epoch, m/s, deg])
//                 index1=serial_number

const WEATHER_TYPES = new Set(["obs_st", "rapid_wind"]);
const MAX_DOUBLES = 20; // Analytics Engine limit per data point
// Analytics Engine allows 25 writeDataPoint() calls per Worker invocation, and one
// POST is one invocation. Exceeding it THROWS, which surfaced as a 500 that vector's
// http sink retried forever — the same batch resent indefinitely while nothing landed
// (2026-07-25). Senders should batch <= 25; if one arrives oversized we write what we
// can and report the rest as dropped, because a partial write plus an honest count
// beats a wedged pipeline.
const MAX_WRITES = 25;

const enc = new TextEncoder();

/** Constant-time-ish bearer check: compare SHA-256 digests, not the strings
 *  (portable — Workers' timingSafeEqual doesn't exist in the node test rig). */
async function authorized(req, env) {
  const got = req.headers.get("authorization") || "";
  const want = `Bearer ${env.VITALS_INGEST_KEY || ""}`;
  if (!env.VITALS_INGEST_KEY) return false;
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(got)),
    crypto.subtle.digest("SHA-256", enc.encode(want)),
  ]);
  const ua = new Uint8Array(a), ub = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < ua.length; i++) diff |= ua[i] ^ ub[i];
  return diff === 0;
}

/** Read the request body, transparently gunzipping when the sink compressed it. */
async function readBody(req) {
  const encoding = (req.headers.get("content-encoding") || "").toLowerCase();
  if (encoding === "gzip" && req.body) {
    const ds = new DecompressionStream("gzip");
    return await new Response(req.body.pipeThrough(ds)).text();
  }
  return await req.text();
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** "k=v,k=v" with a stable order; host + collector ride their own columns. */
export function collapseTags(tags) {
  return Object.keys(tags || {})
    .filter((k) => k !== "host" && k !== "collector")
    .sort()
    .map((k) => `${k}=${tags[k]}`)
    .join(",");
}

/** host_metrics native_json event → one host_vitals data point (or null). */
export function metricPoint(m) {
  const value = m.gauge?.value ?? m.counter?.value;
  if (!isNum(value)) return null; // e.g. autofs used_ratio arrives as null
  const host = m.tags?.host || "unknown";
  const ts = m.timestamp ? Date.parse(m.timestamp) / 1000 : Date.now() / 1000;
  return {
    blobs: [host, m.name || "", collapseTags(m.tags), m.tags?.collector || ""],
    doubles: [value, ts],
    indexes: [host],
  };
}

/** Tempest log event → weather_obs data points (obs_st can carry >1 row). */
export function weatherPoints(log) {
  if (!WEATHER_TYPES.has(log.type)) return [];
  const serial = log.serial_number || "unknown";
  const host = log.host || "";
  const rows = log.type === "obs_st" ? log.obs : [log.ob];
  const out = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!Array.isArray(row)) continue;
    const doubles = row.slice(0, MAX_DOUBLES).map((v) => (isNum(v) ? v : 0));
    out.push({
      blobs: [log.type, serial, JSON.stringify(row), host],
      doubles,
      indexes: [serial],
    });
  }
  return out;
}

/** Parse one NDJSON batch → {vitals: [...], weather: [...], skipped}.
 *  Throws on malformed JSON (caller answers 400). */
export function parseBatch(text) {
  const vitals = [], weather = [];
  let skipped = 0;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const ev = JSON.parse(line); // malformed line → throw → 400
    if (ev.metric) {
      const p = metricPoint(ev.metric);
      p ? vitals.push(p) : skipped++;
    } else if (ev.log) {
      const pts = weatherPoints(ev.log);
      pts.length ? weather.push(...pts) : skipped++;
    } else {
      skipped++;
    }
  }
  return { vitals, weather, skipped };
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });

export default {
  async fetch(req, env) {
    const { pathname } = new URL(req.url);

    // /health, plus GET /ingest so Vector's sink healthcheck (a GET against
    // the sink uri) sees a 200. No auth — it reveals nothing.
    if (pathname === "/health" || (pathname === "/ingest" && req.method === "GET"))
      return json({ ok: true, service: "host-vitals" });

    if (pathname !== "/ingest") return json({ error: "not found" }, 404);
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);
    if (!(await authorized(req, env))) return json({ error: "unauthorized" }, 401);

    let batch;
    try {
      batch = parseBatch(await readBody(req));
    } catch {
      return json({ error: "malformed batch" }, 400);
    }

    // Vitals first: the freshness heartbeat matters more than a weather sample.
    let budget = MAX_WRITES;
    const vitals = batch.vitals.slice(0, budget);
    for (const p of vitals) env.VITALS.writeDataPoint(p);
    budget -= vitals.length;
    const weather = batch.weather.slice(0, Math.max(0, budget));
    for (const p of weather) env.WEATHER.writeDataPoint(p);

    const dropped =
      batch.vitals.length - vitals.length + (batch.weather.length - weather.length);
    if (dropped > 0) {
      console.warn(
        `batch exceeded ${MAX_WRITES}-write cap: wrote ${vitals.length}+${weather.length}, dropped ${dropped} — reduce the sender's batch.max_events`,
      );
    }

    return json({
      ok: true,
      written: { host_vitals: vitals.length, weather_obs: weather.length },
      dropped,
      skipped: batch.skipped,
    });
  },
};
