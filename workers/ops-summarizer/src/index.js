// ops-summarizer — the handler behind fleet-bus's `work` topics.
//
// Two lanes, one machine:
//   fleet.github.notification    a GitHub notification → a short summary
//   fleet.ops.alarm.repeated     an alarm that keeps firing → what is actually
//                                going on, grounded in the dev vault
//
// It is a fleet-bus HANDLER, but it does NOT run the model. Claude work in this
// fleet runs in GitHub Actions under subscription auth — the CLI holds that auth
// and a Worker cannot run the CLI. So /summarize fires a repository_dispatch and
// reports the job as LEASED; the workflow acks fleet-bus when it is done. See
// dispatch.js for why this is not an implementation detail.
//
// This Worker keeps the two things it is genuinely good at: Vectorize retrieval
// (also exposed over MCP, so `claude -p` in Actions can ground itself) and the
// #ops telemetry rendering.
import { retrieve, ingest } from "./rag.js";
import { dispatchSummary, LEASE_MS } from "./dispatch.js";
import { handleMcp } from "./mcp.js";
import { render as renderTelemetry } from "./telemetry.js";

const json = (b, status = 200) =>
  new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

const authorized = (req, env) =>
  Boolean(env.HANDLER_TOKEN) &&
  timingSafeEqual(req.headers.get("authorization") || "", `Bearer ${env.HANDLER_TOKEN}`);

/** The retrieval query for a job — what we search the vault for. */
function ragQuery(topic, data) {
  if (topic === "fleet.ops.alarm.repeated") {
    return `${data.title || ""} ${data.reason || ""} ${data.topic || ""}`.trim();
  }
  return `${data.repo || ""} ${data.title || ""}`.trim();
}

const LANES = new Set(["fleet.github.notification", "fleet.ops.alarm.repeated"]);

/**
 * Hand one job to Actions.
 *
 * Retrieval still happens HERE, not in the workflow. Two reasons: the vault
 * grounding is the part most likely to be wrong, and doing it here means it is
 * visible in this Worker's own logs and telemetry rather than buried in a run
 * log. The workflow also has the MCP server if it wants to search further.
 */
async function handleSummarize(req, env) {
  let envelope;
  try {
    envelope = await req.json();
  } catch {
    return json({ error: "body is not JSON" }, 400);
  }

  const topic = envelope?.topic;
  const data = envelope?.data || {};
  if (!LANES.has(topic)) return json({ error: `no lane for topic ${topic}` }, 400);

  // fleet-bus stamps the queue id so the workflow can ack the exact row.
  const jobId = envelope?.jobId ?? data.jobId;

  const ragStart = Date.now();
  const matches = await retrieve(env, ragQuery(topic, data));
  const ragMs = Date.now() - ragStart;

  const result = await dispatchSummary(env, {
    topic,
    jobId,
    data: {
      ...data,
      // Pre-fetched grounding travels with the job so the workflow does not have
      // to repeat the search to produce a comparable answer.
      vaultContext: matches.map((m) => ({
        vault: m.vault,
        title: m.title,
        score: m.score,
        text: m.text,
      })),
    },
  });

  if (!result.ok) {
    // 4xx from GitHub is our misconfiguration and will not fix itself; 5xx might.
    return json({ error: result.error }, result.status >= 400 && result.status < 500 ? 400 : 502);
  }

  return json({ ok: true, leased: true, leaseMs: LEASE_MS, ragHits: matches.length, ragMs, jobId });
}

/**
 * Callback from the Actions run: render, post to #ops, and ack the queue.
 *
 * The workflow does not post to Discord itself. "Actions owns the model and the
 * words, the Worker owns the Discord identity" is the settled split in this
 * fleet, and it is worth keeping — the webhook lives in exactly one place, the
 * telemetry renderer is not duplicated into bash, and a workflow that dies after
 * posting cannot leave a message with no ack behind it.
 *
 * Ack forwarding is here for the same reason: one call from the workflow instead
 * of two, so a summary cannot be posted and then left un-acked because the second
 * request failed.
 */
async function handlePost(req, env) {
  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "body is not JSON" }, 400);
  }

  const { topic, jobId, data = {}, text, telemetry = {} } = body || {};
  if (!LANES.has(topic)) return json({ error: `no lane for topic ${topic}` }, 400);
  if (typeof text !== "string" || !text.trim()) return json({ error: "text is required" }, 400);

  const heading =
    topic === "fleet.ops.alarm.repeated"
      ? `🔁 **${data.title || data.topic || "repeated alarm"}**`
      : `📋 **${data.repo || "github"}${data.n ? ` #${data.n}` : ""}**`;

  const tel = renderTelemetry({
    model: telemetry.model || "claude -p",
    inputTokens: telemetry.inputTokens,
    freshInputTokens: telemetry.freshInputTokens,
    cacheWriteTokens: telemetry.cacheWriteTokens,
    cacheReadTokens: telemetry.cacheReadTokens,
    outputTokens: telemetry.outputTokens,
    turns: telemetry.turns ?? 1,
    maxTurns: telemetry.maxTurns ?? Number(env.MAX_TURNS || 3),
    toolCalls: telemetry.toolCalls ?? 0,
    truncated: Boolean(telemetry.truncated),
    ragHits: telemetry.ragHits ?? (telemetry.hits || []).length,
    hits: telemetry.hits || [],
    ragMs: telemetry.ragMs,
    llmMs: telemetry.apiMs ?? telemetry.llmMs,
    totalMs: telemetry.totalMs,
  });

  let posted = false;
  if (env.WEBHOOK_OPS) {
    try {
      const res = await fetch(env.WEBHOOK_OPS, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ content: `${heading}\n${text.slice(0, 1900 - tel.length)}\n${tel}` }),
      });
      posted = res.ok;
    } catch {
      posted = false;
    }
  }

  // Ack the queue even if Discord was down: the summary was produced, and making
  // the workflow re-run a model because a webhook blipped is the wrong trade.
  let acked = null;
  if (env.BUS_URL && env.BUS_TOKEN && Number.isInteger(jobId)) {
    try {
      const res = await fetch(`${env.BUS_URL.replace(/\/+$/, "")}/ack`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${env.BUS_TOKEN}` },
        body: JSON.stringify({ topic, id: jobId }),
      });
      acked = res.ok;
    } catch {
      acked = false;
    }
  }

  return json({ ok: true, posted, acked });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);

    if (url.pathname === "/health") {
      // Reports capability, not just liveness — "is the key set" is the question
      // being asked of this Worker most often.
      return json({
        ok: true,
        service: "ops-summarizer",
        vectorize: Boolean(env.VECTORIZE),
        ai: Boolean(env.AI),
        // The model runs in Actions, so readiness here is "can we dispatch",
        // not "do we hold a model credential".
        dispatch: Boolean(env.DISPATCH_REPO && env.APP_ID && env.APP_PRIVATE_KEY),
        dispatchRepo: env.DISPATCH_REPO || null,
      });
    }

    if (!authorized(req, env)) return json({ error: "unauthorized" }, 401);

    // The dev vault as an MCP tool. Same bearer as everything else here, so a
    // client is configured with one credential rather than a second scheme.
    // Reached by every Claude surface that speaks MCP — headless `claude -p`
    // jobs, the channel bots, interactive sessions — because MCP config is
    // per project/user, not per session.
    if (url.pathname === "/mcp") return handleMcp(req, env, { retrieve });

    if (req.method === "POST" && url.pathname === "/summarize") return handleSummarize(req, env);
    if (req.method === "POST" && url.pathname === "/post") return handlePost(req, env);

    // Explicit failure from a run that died. Forwarded to the queue so backoff
    // applies now rather than after a 20-minute lease expiry.
    if (req.method === "POST" && url.pathname === "/fail") {
      let b;
      try {
        b = await req.json();
      } catch {
        return json({ error: "body is not JSON" }, 400);
      }
      if (!LANES.has(b?.topic)) return json({ error: `no lane for topic ${b?.topic}` }, 400);
      if (!env.BUS_URL || !env.BUS_TOKEN || !Number.isInteger(b?.jobId)) {
        return json({ ok: true, forwarded: false });
      }
      try {
        const res = await fetch(`${env.BUS_URL.replace(/\/+$/, "")}/ack`, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: `Bearer ${env.BUS_TOKEN}` },
          body: JSON.stringify({ topic: b.topic, id: b.jobId, failed: true, reason: b.reason }),
        });
        return json({ ok: true, forwarded: res.ok });
      } catch {
        return json({ ok: true, forwarded: false });
      }
    }

    // Admin: push dev-vault chunks in. Kept here rather than in a script with an
    // API token so every Cloudflare credential stays a binding.
    if (req.method === "POST" && url.pathname === "/ingest") {
      let body;
      try {
        body = await req.json();
      } catch {
        return json({ error: "body is not JSON" }, 400);
      }
      if (!Array.isArray(body?.chunks)) return json({ error: "chunks[] required" }, 400);
      try {
        return json({ ok: true, upserted: await ingest(env, body.chunks) });
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }

    if (req.method === "GET" && url.pathname === "/search") {
      const q = url.searchParams.get("q") || "";
      if (!q) return json({ error: "q required" }, 400);
      const vault = url.searchParams.get("vault") || null;
      return json({ matches: await retrieve(env, q, Number(url.searchParams.get("k") || 5), vault) });
    }

    return json({ error: "not found" }, 404);
  },
};
