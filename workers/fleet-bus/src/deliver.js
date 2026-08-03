// deliver — server-side fan-out from a topic to its subscribers.
//
// Subscribers are named routes, not URLs, and a topic lists route names in its
// CATALOG entry. The mapping from route to destination lives in wrangler.toml, so
// adding a consumer is a config change and the topic catalogue stays about topics.
//
// Today there is one route, `ops`, which posts to the #ops Discord webhook. That
// is the same channel the vitals lane already alerts to (observability-config#171
// moved alarms out of #dev), so bus events land where the investigator already
// looks instead of opening a second stream of alarms.
//
// Delivery is best-effort and never throws. The bus contract is at-most-once and
// drop-safe by design; a Discord outage must not fail a publish or wedge a DO
// alarm, because the producer treats an unreachable bus as a silent no-op and
// would otherwise never learn the difference.

const COLORS = {
  silent: 0xe74c3c, // red — a producer has gone quiet
  recovered: 0x2ecc71, // green — it came back
  event: 0x3498db, // blue — ordinary event
};

/** Route name → webhook URL, from env. Unmapped routes are skipped, not fatal. */
function routeUrl(env, route) {
  const key = `WEBHOOK_${route.toUpperCase()}`;
  return env[key] || null;
}

/**
 * Render a notification. Kept terse deliberately: #ops is an alarm channel, and
 * the digest is where periodic detail belongs.
 *
 * Exported for tests — the repo's convention is `node --test` over the pure core,
 * with no runtime and no network.
 */
export function render(note) {
  const { kind, topic } = note;
  if (kind === "silent") {
    return {
      title: `🔴 ${topic} is silent`,
      description:
        `No publish within the topic's TTL. Last seen ${note.lastSeenSec}s ago.\n` +
        "For a heartbeat topic this means the producer stopped, not that the bus did — " +
        "the bus noticed precisely because nothing arrived to refresh the retained value.",
      color: COLORS.silent,
    };
  }
  if (kind === "recovered") {
    const forSec = note.silentForSec == null ? "an unknown period" : `${note.silentForSec}s`;
    return {
      title: `🟢 ${topic} recovered`,
      description: `Publishing again after ${forSec} of silence.`,
      color: COLORS.recovered,
    };
  }
  // Ordinary event — show the payload compactly; these are low-rate by contract.
  let data = "";
  try {
    data = "```json\n" + JSON.stringify(note.envelope?.data ?? {}, null, 2).slice(0, 1400) + "\n```";
  } catch {
    data = "_payload not renderable_";
  }
  return {
    title: `${topic}`,
    description: data,
    color: COLORS.event,
  };
}

/**
 * Fan out one notification to a topic's subscribers.
 * @param {Record<string, string>} env
 * @param {string} topic
 * @param {{kind: "silent"|"recovered"|"event", topic: string, [k: string]: unknown}} note
 */
export async function deliver(env, topic, note) {
  // Imported lazily to keep the DO module's import graph free of the catalogue,
  // which the Worker entrypoint already validates against before routing here.
  const { specFor } = await import("./contract.js");
  const spec = specFor(topic);
  const routes = spec?.subscribers ?? [];
  if (!routes.length) return { delivered: 0 };

  const embed = render(note);
  let delivered = 0;

  for (const route of routes) {
    const url = routeUrl(env, route);
    if (!url) continue;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          embeds: [{ ...embed, footer: { text: `fleet-bus · ${topic}` } }],
        }),
      });
      if (res.ok) delivered += 1;
    } catch {
      // Swallowed on purpose — see the header. A failed post is not a failed publish.
    }
  }
  return { delivered };
}
