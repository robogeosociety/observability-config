// contract — the topic catalogue, mirrored from supervisor/bus_contract.py.
//
// The Python side stays canonical: `supervisor/bus_contract.py` owns the envelope
// version, the closed src/type/class enums, and each topic's data schema, and
// generates `contract/bus.schema.json` from it. This file is the Worker's read of
// the same truth, and it is deliberately a mirror rather than a second source —
// adding a topic here without registering it there will be rejected by the
// producing side's own validation before it ever reaches us.
//
// Kept narrow on purpose: the Worker needs delivery class, TTL, and stream name to
// route correctly. Per-field payload validation stays with the producer, which has
// the generated schema and fails closed. Re-implementing it here would be a second
// copy to drift.
export const ENVELOPE_VERSION = 1;

// "ops-buttons" is the Discord interactions Worker (discobots): it is the only producer
// that publishes a COMMAND rather than telemetry, which is why it needs its own src --
// an action attributed to a heartbeat producer would be indistinguishable from one.
export const SRCS = ["supervisor", "discobot-live", "wikiserve", "tommybot", "gateway", "ops-buttons"];
export const TYPES = ["update", "event"];

// `work` is new here and must be added to bus_contract.py's CLASSES before any
// producer can publish it — see the migration note in the proposal. The two
// existing classes are drop-safe; work is at-least-once with a durable backlog,
// because the thing it carries (a GitHub notification awaiting a summary) has no
// successor to make good a loss.
export const CLASSES = ["telemetry", "event", "work"];

/**
 * @typedef {{cls: "telemetry"|"event"|"work", src: string, type: "update"|"event",
 *            ttl?: number, stream?: string, subscribers?: string[],
 *            handler?: string}} TopicSpec
 */

/**
 * `subscribers` names the delivery routes a topic fans out to (see deliver.js).
 * A telemetry topic with a `ttl` and a subscriber is, by construction, a
 * monitored heartbeat: the TTL alarm firing IS the silence signal.
 *
 * @type {Record<string, TopicSpec>}
 */
export const CATALOG = {
  // ---- existing topics, carried over unchanged from bus_contract.py ----
  "fleet.supervisor.tick": {
    cls: "telemetry",
    src: "supervisor",
    type: "update",
    ttl: 180,
    subscribers: ["ops"],
  },
  "fleet.supervisor.lifecycle": {
    cls: "telemetry",
    src: "supervisor",
    type: "update",
    ttl: 600,
    subscribers: ["ops"],
  },
  // Fleet action buttons. ops-buttons (discobots) publishes an approved button press here;
  // fleet_button_worker.py on the mini drains it and runs fleet-ctl.
  //
  // TELEMETRY, not event: the mini polls /retained, so last-value is the right shape. The
  // Discord interaction id rides along as a nonce, so the executor can re-read the same
  // retained envelope without running the action twice.
  //
  // NO subscribers: this is a command for the mini to drain, not something to fan out to
  // Discord. The executor reports the outcome to #ops itself, once it knows what happened.
  //
  // ttl 300 matches the executor's staleness bound -- a queued action that sat through a
  // restart should expire rather than fire minutes later at an operator who has moved on.
  "fleet.button.request": {
    cls: "telemetry",
    src: "ops-buttons",
    type: "update",
    ttl: 300,
  },
  "fleet.wiki.request.pending": {
    cls: "event",
    src: "wikiserve",
    type: "event",
    stream: "wiki.request",
  },

  // ---- new topics: host health + discobot heartbeats ----
  // These are the consumers that justified server-side fan-out. Each is a
  // telemetry topic with a TTL, so liveness needs no poller: miss the window and
  // the alarm announces it.
  "fleet.host.disk": {
    cls: "telemetry",
    src: "gateway",
    type: "update",
    ttl: 900,
    subscribers: ["ops"],
  },
  "fleet.host.memory": {
    cls: "telemetry",
    src: "gateway",
    type: "update",
    ttl: 900,
    subscribers: ["ops"],
  },
  "fleet.discobot.heartbeat": {
    cls: "telemetry",
    src: "discobot-live",
    type: "update",
    // Bots tick fast; 5 min of silence is a real outage rather than a slow loop.
    ttl: 300,
    subscribers: ["ops"],
  },

  // ---- work: durable, at-least-once, quota-aware ----
  // A GitHub notification that should become a haiku summary. Unlike a heartbeat
  // there is no successor event to make good a loss, and the expected failure is
  // a token-quota outage lasting hours — so this is queued durably and the queue
  // parks wholesale rather than burning the backlog against an exhausted quota.
  "fleet.github.notification": {
    cls: "work",
    src: "gateway",
    type: "event",
    handler: "summary",
  },

  // An #ops alarm that has fired repeatedly, sent for explanation against the
  // operator's own vault notes. Work rather than telemetry: the point is the
  // written explanation, and a quota outage must not discard the backlog of
  // alarms that were worth explaining.
  //
  // NOTE: nothing publishes this yet. Detecting that an alarm is REPEATING —
  // counting occurrences in a window and firing once per burst rather than once
  // per alarm — is not built. Registering the topic makes the lane addressable
  // and testable; the producer is still owed.
  "fleet.ops.alarm.repeated": {
    cls: "work",
    src: "gateway",
    type: "event",
    handler: "summary",
  },
};

/** @returns {TopicSpec | null} */
export function specFor(topic) {
  return Object.prototype.hasOwnProperty.call(CATALOG, topic) ? CATALOG[topic] : null;
}

/**
 * Envelope shape check: `{v, ts, src, topic, type, data}`.
 * Rejects on the closed enums and on class/type disagreement, so a topic declared
 * `event` cannot be published as `update` — the same rule the Python side enforces.
 * @returns {string[]} problems, empty when valid
 */
export function validateEnvelope(env, spec) {
  const problems = [];
  if (env == null || typeof env !== "object") return ["envelope is not an object"];
  if (env.v !== ENVELOPE_VERSION) problems.push(`v must be ${ENVELOPE_VERSION}, got ${env.v}`);
  if (typeof env.ts !== "number") problems.push("ts must be a number");
  if (typeof env.topic !== "string") problems.push("topic must be a string");
  if (!SRCS.includes(env.src)) problems.push(`src ${JSON.stringify(env.src)} not in SRCS`);
  if (!TYPES.includes(env.type)) problems.push(`type ${JSON.stringify(env.type)} not in TYPES`);
  if (env.data === undefined) problems.push("data is required");
  if (spec) {
    if (env.src !== spec.src) problems.push(`src ${env.src} != declared ${spec.src}`);
    if (env.type !== spec.type) problems.push(`type ${env.type} != declared ${spec.type}`);
  }
  return problems;
}
