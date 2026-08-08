// vaults.toml decides what the cloud index holds and who reads what, so its failures
// are access and quality failures rather than crashes.
//
// The policy exists in two places by necessity — a Worker is a bundle and cannot read
// a file at runtime, so its slice lives in wrangler.toml [vars]. Two copies are only
// safe if something fails when they disagree. That is most of this file.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { parsePolicy, loadPolicy } from "../scripts/vault-policy.mjs";
import { alarmVaults } from "../src/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));

const GOOD = `
mirrored = ["dev", "home"]

[consumers.alarms]
vaults = ["dev"]
`;

// ── the parser is strict on purpose ─────────────────────────────────────────

test("a policy round-trips", () => {
  const p = parsePolicy(GOOD);
  assert.deepEqual(p.mirrored, ["dev", "home"]);
  assert.deepEqual(p.consumers.alarms.vaults, ["dev"]);
});

test("a consumer reading a vault the mirror does not ship is refused", () => {
  // It would read an empty index and look exactly like a vault with no answers —
  // the failure shape that hid 187 missing notes for days.
  const bad = GOOD.replace('vaults = ["dev"]', 'vaults = ["dev", "camping"]');
  assert.throws(() => parsePolicy(bad), /not mirrored/);
});

test("an empty consumer is a mistake, not a revocation", () => {
  assert.throws(() => parsePolicy(GOOD.replace('vaults = ["dev"]', "vaults = []")), /remove the table/);
});

test("an empty mirror is refused", () => {
  assert.throws(() => parsePolicy('mirrored = []\n'), /missing or empty/);
});

test("a line the parser does not understand raises instead of being skipped", () => {
  // A policy file that silently ignores half its contents is how a consumer ends up
  // reading vaults nobody granted it.
  assert.throws(() => parsePolicy(GOOD + "\nsomething_else = 3\n"), /unparseable|unexpected/);
});

test("an unexpected table is refused", () => {
  assert.throws(() => parsePolicy(GOOD + "\n[wat.x]\nvaults = [\"dev\"]\n"), /only \[consumers/);
});

// ── the two copies must agree ───────────────────────────────────────────────

test("wrangler's ALARM_VAULTS matches vaults.toml", () => {
  // The load-bearing test. If this fails, the deployed Worker is retrieving from a
  // different set of vaults than the policy says, and nothing else would notice.
  const policy = loadPolicy();
  const wrangler = readFileSync(join(HERE, "..", "wrangler.toml"), "utf8");
  const m = /^ALARM_VAULTS\s*=\s*"([^"]*)"/m.exec(wrangler);
  assert.ok(m, "wrangler.toml must declare ALARM_VAULTS");
  assert.deepEqual(
    m[1].split(",").map((s) => s.trim()).filter(Boolean),
    policy.consumers.alarms.vaults,
  );
});

test("the shipped policy still points every consumer at mirrored vaults", () => {
  const p = loadPolicy();
  for (const [name, spec] of Object.entries(p.consumers)) {
    for (const v of spec.vaults) assert.ok(p.mirrored.includes(v), `${name} reads unmirrored ${v}`);
  }
});

// ── how the Worker reads it ─────────────────────────────────────────────────

test("one vault becomes a plain filter, several become a list", () => {
  assert.equal(alarmVaults({ ALARM_VAULTS: "dev" }), "dev");
  assert.deepEqual(alarmVaults({ ALARM_VAULTS: "dev, home" }), ["dev", "home"]);
});

test("an unset var means unfiltered, not empty", () => {
  // Degrading to "search everything" is the old behaviour and merely dilutes. Degrading
  // to "search nothing" would strip the summariser of context entirely, which is worse
  // than the problem this filter solves.
  assert.equal(alarmVaults({}), null);
  assert.equal(alarmVaults({ ALARM_VAULTS: "  " }), null);
});
