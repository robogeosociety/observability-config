// Unit tests for the digest renderer. Pure — no runtime, no network.
import assert from "node:assert/strict";
import { test } from "node:test";
import { renderDigest } from "../src/digest.js";

const NOW = 1_785_700_000_000;

test("a healthy fleet renders green", () => {
  const e = renderDigest(
    {
      "fleet.supervisor.tick": { cls: "telemetry", silent: false, retained: { ageSec: 42 } },
      "fleet.github.notification": { cls: "work", depth: 0, dead: 0, parkedUntil: null },
    },
    NOW,
  );
  assert.match(e.title, /🟢/);
  assert.equal(e.color, 0x2ecc71);
});

test("one silent topic makes the whole digest red", () => {
  const e = renderDigest(
    {
      "fleet.supervisor.tick": { cls: "telemetry", silent: false, retained: { ageSec: 10 } },
      "fleet.discobot.heartbeat": {
        cls: "telemetry",
        silent: true,
        silentSince: NOW - 600_000,
        retained: null,
      },
    },
    NOW,
  );
  assert.match(e.title, /🔴/);
  assert.match(e.description, /silent/);
  assert.equal(e.color, 0xe74c3c);
});

test("never-published is distinguished from gone-silent", () => {
  // Conflating these would send someone hunting for a producer that stopped, when
  // in fact one was never wired up.
  const e = renderDigest(
    { "fleet.host.disk": { cls: "telemetry", silent: false, retained: null } },
    NOW,
  );
  assert.match(e.description, /no data yet/);
  assert.doesNotMatch(e.description, /silent/);
});

test("a parked queue is amber, not red, and says why and for how long", () => {
  // Parking is the quota backstop working. Rendering it as a failure would train
  // the reader to ignore the one state that actually needs patience.
  const e = renderDigest(
    {
      "fleet.github.notification": {
        cls: "work",
        depth: 12,
        dead: 0,
        parkedUntil: NOW + 600_000,
        parkedReason: "429 from handler",
        oldestAgeSec: 900,
      },
    },
    NOW,
  );
  assert.match(e.title, /🟡/);
  assert.match(e.description, /parked/);
  assert.match(e.description, /10m/);
  assert.match(e.description, /429 from handler/);
  assert.equal(e.color, 0xf1c40f);
});

test("dead-lettered work is surfaced even when the queue is drained", () => {
  const e = renderDigest(
    { "fleet.github.notification": { cls: "work", depth: 0, dead: 3, parkedUntil: null } },
    NOW,
  );
  assert.match(e.description, /3 dead/);
  assert.match(e.title, /🟡/);
});

test("silence outranks a parked queue for the headline glyph", () => {
  const e = renderDigest(
    {
      "fleet.github.notification": { cls: "work", depth: 1, dead: 1, parkedUntil: NOW + 60_000 },
      "fleet.supervisor.tick": { cls: "telemetry", silent: true, silentSince: NOW - 60_000, retained: null },
    },
    NOW,
  );
  assert.match(e.title, /🔴/);
});

test("age formatting stays readable across scales", () => {
  const e = renderDigest(
    {
      a: { cls: "telemetry", silent: false, retained: { ageSec: 30 } },
      b: { cls: "telemetry", silent: false, retained: { ageSec: 600 } },
      c: { cls: "telemetry", silent: false, retained: { ageSec: 7200 } },
    },
    NOW,
  );
  assert.match(e.description, /30s ago/);
  assert.match(e.description, /10m ago/);
  assert.match(e.description, /2h ago/);
});

test("an empty catalogue does not render an empty embed", () => {
  const e = renderDigest({}, NOW);
  assert.match(e.description, /no topics/);
});
