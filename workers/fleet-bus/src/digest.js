// digest — the periodic loop-health rollup for #ops.
//
// The silence and recovery alarms in deliver.js are edge-triggered: they fire when
// something changes and are quiet otherwise. That is right for alarms and wrong for
// answering "is the fleet healthy right now", which is the question the retired
// Grafana dashboards used to answer at a glance.
//
// So this is level-triggered: one message, edited in place on a schedule, showing
// every topic's current state. Editing rather than posting is deliberate — the
// pattern the discobot memory panels already use in #dashboards. A channel that
// accumulates one health post per interval is a channel nobody reads.
//
// It is a rollup, NOT a metrics dashboard. Per-topic publish rates and stream
// depth over time belong in Analytics Engine, which already receives them; putting
// graphs back in Discord would re-create what the TIG retirement removed.

const OK = "🟢";
const SILENT = "🔴";
const IDLE = "⚪";
const WARN = "🟡";

function agoText(sec) {
  if (sec == null) return "never";
  if (sec < 90) return `${sec}s ago`;
  if (sec < 5400) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

/**
 * Render the digest body from a /stat payload.
 * Pure — exported so it can be tested without a runtime or a network.
 */
export function renderDigest(topics, now = Date.now()) {
  const lines = [];
  let worst = OK;

  for (const [topic, s] of Object.entries(topics)) {
    if (s.cls === "work") {
      const parked = s.parkedUntil && s.parkedUntil > now;
      // A parked queue is not a failure — it is the quota backstop doing its job.
      // It only deserves attention when the backlog is also growing.
      const glyph = parked ? WARN : s.dead > 0 ? WARN : OK;
      if (glyph === WARN && worst === OK) worst = WARN;
      const bits = [`depth ${s.depth}`];
      if (s.dead) bits.push(`**${s.dead} dead**`);
      if (parked) {
        bits.push(`**parked** ${Math.round((s.parkedUntil - now) / 60000)}m (${s.parkedReason || "quota"})`);
      }
      if (s.oldestAgeSec != null) bits.push(`oldest ${agoText(s.oldestAgeSec)}`);
      lines.push(`${glyph} \`${topic}\` — ${bits.join(", ")}`);
      continue;
    }

    if (s.cls === "event") {
      lines.push(`${IDLE} \`${topic}\` — stream depth ${s.streamDepth}`);
      continue;
    }

    // telemetry
    if (s.silent) {
      worst = SILENT;
      const since = s.silentSince ? agoText(Math.round((now - s.silentSince) / 1000)) : "unknown";
      lines.push(`${SILENT} \`${topic}\` — **silent** since ${since}`);
    } else if (s.retained) {
      lines.push(`${OK} \`${topic}\` — last ${agoText(s.retained.ageSec)}`);
    } else {
      // No retained value and not flagged silent: nothing has ever published it.
      // Distinguished from silence on purpose — "never started" and "stopped" are
      // different problems and the digest should not conflate them.
      lines.push(`${IDLE} \`${topic}\` — no data yet`);
    }
  }

  return {
    title: `${worst} fleet bus`,
    description: lines.join("\n") || "_no topics_",
    color: worst === SILENT ? 0xe74c3c : worst === WARN ? 0xf1c40f : 0x2ecc71,
  };
}

/**
 * Post or edit the digest message. Discord webhooks can edit a prior message given
 * its id, which is what keeps this to one message instead of a stream of them.
 * The id is held in DIGEST KV; losing it costs one duplicate, not correctness.
 */
export async function publishDigest(env, topics) {
  const url = env.WEBHOOK_OPS;
  if (!url) return { skipped: "no WEBHOOK_OPS" };

  const embed = renderDigest(topics);
  const body = JSON.stringify({
    embeds: [{ ...embed, footer: { text: "fleet-bus · loop health" } }],
  });

  const priorId = env.DIGEST ? await env.DIGEST.get("message_id") : null;

  if (priorId) {
    const res = await fetch(`${url}/messages/${priorId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body,
    });
    if (res.ok) return { edited: priorId };
    // The message was deleted, or the webhook rotated — fall through and repost.
  }

  const res = await fetch(`${url}?wait=true`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  if (!res.ok) return { error: res.status };
  try {
    const posted = await res.json();
    if (posted?.id && env.DIGEST) await env.DIGEST.put("message_id", posted.id);
    return { posted: posted?.id ?? null };
  } catch {
    return { posted: null };
  }
}
