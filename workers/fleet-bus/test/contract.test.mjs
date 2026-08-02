// Unit tests for the parts of fleet-bus that need no runtime: the topic
// catalogue's internal consistency, envelope validation, and the #ops renderer.
// Plain `node --test`, no deps — same shape as workers/cicd-collector/test.
//
// What is NOT covered here is the Durable Object behaviour itself (retained TTL,
// alarm-driven silence, queue parking), which needs the workerd runtime. Those
// are exercised against a real deployment in test/smoke.mjs.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CATALOG,
  CLASSES,
  ENVELOPE_VERSION,
  SRCS,
  TYPES,
  specFor,
  validateEnvelope,
} from "../src/contract.js";
import { render } from "../src/deliver.js";

test("catalogue entries are internally consistent", () => {
  for (const [name, spec] of Object.entries(CATALOG)) {
    assert.ok(CLASSES.includes(spec.cls), `${name}: bad class ${spec.cls}`);
    assert.ok(SRCS.includes(spec.src), `${name}: bad src ${spec.src}`);
    assert.ok(TYPES.includes(spec.type), `${name}: bad type ${spec.type}`);

    // A telemetry topic without a TTL has no retained semantics and no liveness
    // signal — it would be silently undetectable, which is the bug this bus exists
    // to remove.
    if (spec.cls === "telemetry") {
      assert.ok(Number.isInteger(spec.ttl) && spec.ttl > 0, `${name}: telemetry needs a ttl`);
    }
    if (spec.cls === "event") {
      assert.ok(typeof spec.stream === "string" && spec.stream, `${name}: event needs a stream`);
    }
    if (spec.cls === "work") {
      assert.ok(typeof spec.handler === "string" && spec.handler, `${name}: work needs a handler`);
      assert.equal(spec.ttl, undefined, `${name}: work must not carry a ttl`);
    }
  }
});

test("stream names are unique — /stream/:name resolves to one topic", () => {
  const streams = Object.values(CATALOG)
    .map((s) => s.stream)
    .filter(Boolean);
  assert.equal(new Set(streams).size, streams.length, "duplicate stream name");
});

test("every subscriber route is a bare name, not a URL", () => {
  // Routes resolve to WEBHOOK_<ROUTE> in env; a URL here would leak a secret into
  // the catalogue and into git.
  for (const [name, spec] of Object.entries(CATALOG)) {
    for (const route of spec.subscribers ?? []) {
      assert.match(route, /^[a-z][a-z0-9_]*$/, `${name}: route ${route} is not a bare name`);
    }
  }
});

const good = {
  v: ENVELOPE_VERSION,
  ts: 1785695281.4,
  src: "supervisor",
  topic: "fleet.supervisor.tick",
  type: "update",
  data: { ok: true },
};

test("a well-formed envelope validates", () => {
  assert.deepEqual(validateEnvelope(good, specFor(good.topic)), []);
});

test("envelope version is pinned", () => {
  const problems = validateEnvelope({ ...good, v: 2 }, specFor(good.topic));
  assert.ok(problems.some((p) => p.includes("v must be")));
});

test("a topic declared update cannot be published as event", () => {
  // The rule bus_contract.py enforces on the producing side; the Worker must not
  // be the weaker gate.
  const problems = validateEnvelope({ ...good, type: "event" }, specFor(good.topic));
  assert.ok(problems.some((p) => p.includes("!= declared")));
});

test("src must match the declared producer", () => {
  const problems = validateEnvelope({ ...good, src: "tommybot" }, specFor(good.topic));
  assert.ok(problems.some((p) => p.includes("!= declared")));
});

test("unknown src is rejected even without a spec", () => {
  const problems = validateEnvelope({ ...good, src: "nope" }, null);
  assert.ok(problems.some((p) => p.includes("not in SRCS")));
});

test("missing data is rejected — absent is not the same as empty", () => {
  const { data, ...without } = good;
  void data;
  assert.ok(validateEnvelope(without, specFor(good.topic)).some((p) => p.includes("data")));
});

test("unknown topics have no spec", () => {
  assert.equal(specFor("fleet.nope"), null);
  // Guard against prototype keys resolving as topics.
  assert.equal(specFor("constructor"), null);
  assert.equal(specFor("__proto__"), null);
});

test("silence renders as an alarm naming the last sighting", () => {
  const e = render({ kind: "silent", topic: "fleet.discobot.heartbeat", lastSeenSec: 412 });
  assert.match(e.title, /silent/);
  assert.match(e.description, /412s ago/);
});

test("recovery renders green and reports the outage length", () => {
  const e = render({ kind: "recovered", topic: "fleet.supervisor.tick", silentForSec: 900 });
  assert.match(e.title, /recovered/);
  assert.match(e.description, /900s/);
});

test("an unrenderable payload degrades instead of throwing", () => {
  const circular = {};
  circular.self = circular;
  const e = render({ kind: "event", topic: "fleet.wiki.request.pending", envelope: { data: circular } });
  assert.match(e.description, /not renderable/);
});
