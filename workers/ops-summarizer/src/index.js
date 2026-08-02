// ops-summarizer — the handler behind fleet-bus's `work` topics.
//
// Two lanes, one machine:
//   fleet.github.notification    a GitHub notification → a short summary
//   fleet.ops.alarm.repeated     an alarm that keeps firing → what is actually
//                                going on, grounded in the dev vault
//
// It is a fleet-bus HANDLER, which fixes its contract: answer 2xx to ack, and
// answer 429 (or 200 {parked:true}) when the token quota is gone. That second
// case is the whole reason the queue has a park state — an out-of-credit account
// must stop the queue, not dead-letter a backlog of healthy work.
//
// Retrieval is Vectorize (see rag.js for why not the mini's vecserve). Turn
// limits live in llm.js so both lanes share one ceiling.
import { complete, QuotaError, DEFAULT_MAX_TURNS } from "./llm.js";
import { retrieve, asContext, ingest } from "./rag.js";
import { handleMcp } from "./mcp.js";

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

// Prompts are terse on purpose. #ops is read while something is broken; a summary
// that buries the finding under preamble costs more than it saves.
const SYSTEM = {
  "fleet.github.notification":
    "You summarise GitHub notifications for an operator who is scanning, not reading. " +
    "Two or three sentences. Lead with what changed and whether it needs them. " +
    "No preamble, no restating the question.",
  "fleet.ops.alarm.repeated":
    "You explain repeated operational alarms to the engineer who owns the host. " +
    "You are given an alarm that has fired several times and notes from their own " +
    "dev vault. Say what is most likely happening, cite the note that supports it " +
    "by title, and give the single next check worth running. Be concrete and brief. " +
    "If the vault notes do not actually explain the alarm, say so plainly rather " +
    "than forcing a connection.",
};

/** The retrieval query for a job — what we search the vault for. */
function ragQuery(topic, data) {
  if (topic === "fleet.ops.alarm.repeated") {
    return `${data.title || ""} ${data.reason || ""} ${data.topic || ""}`.trim();
  }
  return `${data.repo || ""} ${data.title || ""}`.trim();
}

function renderUser(topic, data, context) {
  if (topic === "fleet.ops.alarm.repeated") {
    const body =
      `Alarm: ${data.title || data.topic || "unknown"}\n` +
      `Fired ${data.count ?? "several"} times` +
      (data.windowMin ? ` in ${data.windowMin} minutes` : "") +
      ".\n" +
      (data.reason ? `Reported reason: ${data.reason}\n` : "") +
      (data.samples?.length ? `Recent occurrences:\n${data.samples.slice(0, 5).join("\n")}\n` : "");
    return context ? `${body}\n${context}` : body;
  }
  const body =
    `Repository: ${data.repo || "unknown"}\n` +
    (data.title ? `Title: ${data.title}\n` : "") +
    (data.n ? `Number: #${data.n}\n` : "") +
    (data.body ? `Body:\n${String(data.body).slice(0, 4000)}\n` : "");
  return context ? `${body}\n${context}` : body;
}

/** Post the finished summary to #ops. Best-effort — see below. */
async function postSummary(env, topic, data, text, meta) {
  if (!env.WEBHOOK_OPS) return { posted: false };
  const title =
    topic === "fleet.ops.alarm.repeated"
      ? `🔁 ${data.title || data.topic || "repeated alarm"}`
      : `📋 ${data.repo || "github"}${data.n ? ` #${data.n}` : ""}`;
  const footer = [
    "ops-summarizer",
    `${meta.turns}/${meta.maxTurns} turns`,
    meta.grounded ? `${meta.grounded} vault notes` : "no vault context",
    meta.truncated ? "TRUNCATED at turn ceiling" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  try {
    const res = await fetch(env.WEBHOOK_OPS, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        embeds: [{ title, description: text.slice(0, 3800), color: 0x9b59b6, footer: { text: footer } }],
      }),
    });
    return { posted: res.ok };
  } catch {
    return { posted: false };
  }
}

async function handleSummarize(req, env) {
  let envelope;
  try {
    envelope = await req.json();
  } catch {
    return json({ error: "body is not JSON" }, 400);
  }

  const topic = envelope?.topic;
  const data = envelope?.data || {};
  if (!SYSTEM[topic]) return json({ error: `no lane for topic ${topic}` }, 400);

  const maxTurns = Number(env.MAX_TURNS || DEFAULT_MAX_TURNS);

  const matches = await retrieve(env, ragQuery(topic, data));
  const context = asContext(matches);

  let result;
  try {
    result = await complete(env, {
      system: SYSTEM[topic],
      user: renderUser(topic, data, context),
      maxTurns,
    });
  } catch (e) {
    if (e instanceof QuotaError) {
      // THE contract with fleet-bus: park the queue, do not charge the item.
      return json({ parked: true, reason: e.message, retryAfterMs: e.retryAfterMs });
    }
    // 4xx from Anthropic is our bug (bad request, bad key) and will not fix
    // itself — let the queue dead-letter it. 5xx gets the queue's backoff.
    return json({ error: String(e) }, e.status && e.status < 500 ? 400 : 502);
  }

  const meta = { turns: result.turns, maxTurns, truncated: result.truncated, grounded: matches.length };
  const posted = await postSummary(env, topic, data, result.text, meta);

  return json({ ok: true, ...meta, usage: result.usage, ...posted });
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
        anthropicKey: Boolean(env.ANTHROPIC_API_KEY),
        vectorize: Boolean(env.VECTORIZE),
        ai: Boolean(env.AI),
        maxTurns: Number(env.MAX_TURNS || DEFAULT_MAX_TURNS),
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
