# fleet-bus — the fleet message bus, on Durable Objects

Replaces the Valkey container on the mini (`robogeosociety/infra#26`). The bus
*contract* is unchanged — same envelope, same topic names, same delivery
semantics — so this is a transport swap, not a redesign.

`supervisor/bus_contract.py` remains canonical for the schema. `src/contract.js`
mirrors only the routing facts the Worker needs (delivery class, TTL, stream name,
subscribers). Per-field payload validation stays with the producer, which has the
generated schema and fails closed; a second copy here would be a second thing to
drift.

## Why Durable Objects

The topic is the coordination atom. Every operation on `fleet.supervisor.tick` is
serialized against that topic and nothing else, and routing is `getByName(topic)`
so a name always lands on the same object with no registry to keep in sync. A
single bus-wide DO would have been the obvious shape and is exactly the bottleneck
the platform warns about.

Storage is SQLite: one retained row per topic, an append-only stream we range-scan
by id, and a job table. All relational-shaped.

## Delivery classes

| Class | Replaces | Semantics |
| --- | --- | --- |
| `telemetry` | `SET retain:<topic> EX n` | retained last-value with TTL; **the TTL alarm is the liveness signal** |
| `event` | `XADD stream:<name>` | append to a capped (1000) stream |
| `work` | *new* | durable queue, at-least-once, acks, quota-aware parking |

### Liveness is free

A telemetry topic's retained value has a TTL. If a heartbeat stops arriving, the
alarm fires with nothing to refresh it — and *that* is the silence signal. A
heartbeat that must be observed is therefore just a topic with a TTL and a
subscriber. No cron, no separate liveness service, no poller.

Silence is announced once per outage, not once per alarm; the next successful
publish emits a recovery.

### Why `work` is its own class

The other two are drop-safe: a lost heartbeat is replaced by the next one. A
GitHub notification awaiting a summary has no successor — drop it and the summary
never exists.

The failure it must survive is a **token quota outage**, which can last hours. A
capped stream would discard the backlog exactly when the backlog matters, and
naive per-item retry would burn the whole queue against a quota that is still
exhausted, turning one outage into N terminal failures. So:

| Outcome | Trigger | Effect |
| --- | --- | --- |
| ack | 2xx | delete |
| **parked** | 429, or 200 `{parked:true}` | **whole queue** stops until cooldown; attempts **not** incremented |
| failed (terminal) | 4xx | dead-letter |
| failed (transient) | 5xx | exponential backoff, dead-letter after 5 attempts |

Parking is queue-wide on purpose: quota is shared, so if item 1 was rejected for
quota, items 2..N will be too. `Retry-After` is honoured when supplied.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | unauthenticated |
| POST | `/publish` | one envelope; routed by topic |
| GET | `/retained/:topic` | 404 once the TTL lapses |
| GET | `/stream/:name` | `?since=&limit=` |
| GET | `/stat` | per-topic health |
| POST | `/digest` | render the rollup now (cron does this hourly) |

All except `/health` need `Authorization: Bearer $BUS_TOKEN`.

## #ops output

Two halves, deliberately:

- **Edge-triggered** (`deliver.js`) — silence and recovery alarms, as they happen.
- **Level-triggered** (`digest.js`) — one message, *edited in place* hourly,
  showing every topic's current state. Editing rather than posting follows the
  discobot memory panels; a channel accumulating one health post per hour is a
  channel nobody reads.

The digest is a rollup, not a dashboard. Per-topic rates over time belong in
Analytics Engine, which already receives them — putting graphs back into Discord
would re-create what the TIG retirement removed.

## Telemetry

Every publish also writes one row to the **existing** `host_vitals` dataset, using
the positional column map shared with `workers/host-vitals` (see its README — do
not guess it). Metric name is `bus.<topic>`, collector `bus`.

That is what lets existing alerting see bus liveness without a second query path:
a fourth vitals signal in `cicd-collector` can reuse the `silent` query verbatim
against a different metric name.

Analytics Engine permits 25 `writeDataPoint()` per invocation and **throws** past
it — the failure `host-vitals` already hit. One publish writes one point, far
inside the limit; the guard exists so a future batch endpoint cannot quietly
reintroduce that 500.

## Config

```
wrangler secret put BUS_TOKEN        # shared bearer; the mini is the only publisher
wrangler secret put WEBHOOK_OPS      # #ops webhook — the `ops` subscriber route
wrangler secret put HANDLER_SUMMARY  # endpoint that summarises a GitHub notification
wrangler secret put HANDLER_TOKEN    # optional bearer presented to handlers
```

A handler **must** answer 429 (or `200 {parked:true}`) when its token quota is
exhausted. That is what parks the queue instead of dead-lettering a backlog of
healthy work.

## Tests

```sh
node --test "test/*.test.mjs"
```

No dependencies, matching `workers/cicd-collector`. These cover the pure core:
catalogue consistency, envelope validation, and both renderers. DO behaviour
(retained TTL, alarm-driven silence, queue parking) needs the workerd runtime and
was verified against the live deployment — see the proposal for the transcript.

## Not built

WebSocket fan-out. Subscribers are server-side, so the DO calls them directly on
publish — no persistent mini→Cloudflare connection and no reconnect path to own on
a host whose disk wedges. The seam is marked in `topic.js` if a subscriber ever
needs a live socket.
