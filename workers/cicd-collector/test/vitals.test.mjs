// Unit tests for the vitals beat's pure core, run with `node --test` (no deps).
//
// These are the *only* verification this logic has had: the Analytics Engine
// read token (cloudflare-tfvend `analytics_read`) did not exist when this was
// written, so not one of these queries has been run against live data. What is
// tested here is everything that does not need a network: the SQL text, the
// threshold arithmetic, the hysteresis bands, and the alert-once/recovery gate.
// What is NOT tested is whether Analytics Engine accepts the SQL and returns the
// column names assumed — see test/dryrun.mjs, which does exactly that once a
// token exists.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  aeQuery,
  bytes,
  config,
  duration,
  evaluate,
  gate,
  message,
  mountpointOf,
  runVitals,
  sqlBusFreshness,
  sqlDisk,
  sqlFreshness,
  sqlMemory,
} from "../src/vitals.js";

const CFG = config({}); // the shipped defaults
const NOW_MS = Date.parse("2026-07-25T12:00:00Z");
const NOW_S = NOW_MS / 1000;

const diskRow = (mount, ratio, samples = 10) => ({
  host: "Tommys-Mac-mini.local", // deliberately the real gethostname() casing
  tags: `device=disk7s1,filesystem=apfs,mountpoint=${mount}`,
  used_ratio: ratio,
  samples: String(samples), // UInt64 comes back from AE as a JSON *string*
});

const memRow = (avg, { min = avg, samples = 10, metric = "memory_available_bytes" } = {}) => ({
  host: "tommys-mac-mini.local",
  metric,
  avg_value: avg,
  min_value: min,
  max_value: avg,
  samples: String(samples),
});

const freshRow = (ageSec) => ({
  host: "tommys-mac-mini.local",
  newest_source_ts: NOW_S - ageSec,
  samples: "600",
});

// The fleet-bus Worker's mirror rows carry its BUS_HOST constant, not Vector's
// host tag — the bus signal must not host-filter, so the fixture keeps the
// mismatched name on purpose.
const busRow = (ageSec) => ({
  host: "Tommys-Mac-mini",
  newest_source_ts: NOW_S - ageSec,
  samples: "10",
});

/** Everything healthy — the baseline every test perturbs one signal from. */
const healthy = () => ({
  disk: [diskRow("/", 0.5), diskRow("/Volumes/dev", 0.5)],
  memory: [
    memRow(4e9),
    memRow(2e9, { metric: "memory_swap_used_bytes" }),
    memRow(8e9, { metric: "memory_swap_total_bytes" }),
  ],
  freshness: [freshRow(60)],
  bus: [busRow(60)],
});

// ── config ───────────────────────────────────────────────────────────────────

test("config: defaults are the conservative shipped thresholds", () => {
  assert.equal(CFG.host, "tommys-mac-mini.local");
  assert.deepEqual(CFG.mounts, ["/", "/Volumes/dev"]);
  assert.equal(CFG.diskAlertRatio, 0.9);
  assert.equal(CFG.diskClearRatio, 0.87);
  assert.equal(CFG.memAlertBytes, 500e6);
  assert.equal(CFG.silentSec, 600);
});

test("config: every threshold is env-tunable, and clear tracks alert by default", () => {
  const c = config({
    VITALS_HOST: "other.local",
    VITALS_MOUNTS: " / , /data ,",
    VITALS_DISK_ALERT_RATIO: "0.8",
    VITALS_MEM_ALERT_BYTES: "1000000000",
    VITALS_SILENT_SEC: "300",
  });
  assert.equal(c.host, "other.local");
  assert.deepEqual(c.mounts, ["/", "/data"]); // trimmed, empties dropped
  assert.equal(c.diskAlertRatio, 0.8);
  assert.equal(c.memAlertBytes, 1e9);
  assert.equal(c.memClearBytes, 1.5e9); // derived from alert when not set
  assert.equal(c.silentSec, 300);
});

test("config: junk env values fall back to the default, they do not become NaN", () => {
  const c = config({ VITALS_DISK_ALERT_RATIO: "ninety percent", VITALS_SILENT_SEC: "" });
  assert.equal(c.diskAlertRatio, 0.9);
  assert.equal(c.silentSec, 600);
});

// ── SQL ──────────────────────────────────────────────────────────────────────

test("sqlDisk: weighted average, correct dataset/column, window interpolated", () => {
  const sql = sqlDisk(10);
  assert.match(sql, /FROM host_vitals/);
  assert.match(sql, /blob2 = 'filesystem_used_ratio'/);
  // AE samples under load: the weighted mean, never avg(double1).
  assert.match(sql, /sum\(double1 \* _sample_interval\) \/ sum\(_sample_interval\)/);
  assert.match(sql, /sum\(_sample_interval\) AS samples/);
  assert.doesNotMatch(sql, /\bcount\(/);
  assert.match(sql, /timestamp > now\(\) - INTERVAL '10' MINUTE/);
  assert.match(sql, /GROUP BY host, tags/);
  assert.match(sql, /FORMAT JSON$/);
});

test("sqlMemory: both metrics via OR (IN is not in the documented AE subset)", () => {
  const sql = sqlMemory(15);
  assert.match(sql, /blob2 = 'memory_available_bytes' OR blob2 = 'memory_swap_used_bytes'/);
  assert.doesNotMatch(sql, /\bIN\s*\(/);
  assert.match(sql, /min\(double1\) AS min_value/);
  assert.match(sql, /INTERVAL '15' MINUTE/);
});

test("sqlFreshness: reads double2 (source time), not the write-time row timestamp", () => {
  const sql = sqlFreshness(60);
  assert.match(sql, /max\(double2\) AS newest_source_ts/);
  // The window predicate may use the row timestamp (that is the indexed column),
  // but the freshness VALUE must come from double2 — a buffered-then-flushed
  // Vector looks healthy on write time and is exactly what this catches.
  assert.doesNotMatch(sql, /max\(timestamp\)/);
  assert.match(sql, /INTERVAL '60' MINUTE/);
});

// ── row parsing ──────────────────────────────────────────────────────────────

test("mountpointOf: pulls the mountpoint out of a collapsed tag string", () => {
  assert.equal(
    mountpointOf("device=disk3s5,filesystem=apfs,mountpoint=/"),
    "/",
  );
  assert.equal(
    mountpointOf("device=disk7s1,filesystem=apfs,mountpoint=/Volumes/dev"),
    "/Volumes/dev",
  );
  // Order is not guaranteed to put mountpoint last, and a path can contain '='.
  assert.equal(mountpointOf("mountpoint=/Volumes/we=ird,filesystem=apfs"), "/Volumes/we=ird");
  assert.equal(mountpointOf("cpu=0,mode=idle"), null);
  assert.equal(mountpointOf(""), null);
  assert.equal(mountpointOf(undefined), null);
  // A tag literally named `not_mountpoint` must not match by suffix.
  assert.equal(mountpointOf("not_mountpoint=/"), null);
});

// ── formatting ───────────────────────────────────────────────────────────────

test("bytes / duration render human units", () => {
  assert.equal(bytes(412e6), "412 MB");
  assert.equal(bytes(2.1e9), "2.1 GB");
  assert.equal(bytes(NaN), "?");
  assert.equal(duration(45), "45s");
  assert.equal(duration(600), "10m");
  assert.equal(duration(7200), "2.0h");
});

// ── disk ─────────────────────────────────────────────────────────────────────

test("disk: over threshold on one mount breaches only that mount", () => {
  const obs = evaluate(
    { ...healthy(), disk: [diskRow("/", 0.5), diskRow("/Volumes/dev", 0.94)] },
    CFG,
    NOW_S,
  );
  const dev = obs.find((o) => o.key === "disk:/Volumes/dev");
  const root = obs.find((o) => o.key === "disk:/");
  assert.equal(dev.state, "breach");
  assert.match(dev.text, /\*\*94\.0%\*\* used/);
  assert.match(dev.text, /\/Volumes\/dev/);
  assert.equal(root.state, "clear");
});

test("disk: the hysteresis band is neither an alert nor an all-clear", () => {
  const at = (r) =>
    evaluate({ ...healthy(), disk: [diskRow("/Volumes/dev", r)] }, CFG, NOW_S)
      .find((o) => o.key === "disk:/Volumes/dev").state;
  assert.equal(at(0.95), "breach");
  assert.equal(at(0.9), "unknown"); // exactly at the threshold — not over it
  assert.equal(at(0.88), "unknown"); // in the band: no flapping back to clear
  assert.equal(at(0.86), "clear");
});

test("disk: a mount with no rows is unknown, never a silent all-clear", () => {
  const obs = evaluate({ ...healthy(), disk: [diskRow("/", 0.5)] }, CFG, NOW_S);
  assert.equal(obs.find((o) => o.key === "disk:/Volumes/dev").state, "unknown");
});

test("disk: rows from another host are ignored", () => {
  const other = { ...diskRow("/Volumes/dev", 0.99), host: "MacBook-Air.local" };
  const obs = evaluate({ ...healthy(), disk: [other] }, CFG, NOW_S);
  assert.equal(obs.find((o) => o.key === "disk:/Volumes/dev").state, "unknown");
});

test("disk: the mini's real gethostname() casing still matches", () => {
  // blob1 carries whatever Vector's host tag says; the docs and the box disagree
  // on capitalisation, and the alert must not care.
  const obs = evaluate(
    { ...healthy(), disk: [diskRow("/Volumes/dev", 0.97)] },
    CFG,
    NOW_S,
  );
  assert.equal(obs.find((o) => o.key === "disk:/Volumes/dev").state, "breach");
});

// ── memory ───────────────────────────────────────────────────────────────────

test("memory: sustained low available memory breaches, with swap as context", () => {
  const obs = evaluate(
    {
      ...healthy(),
      memory: [
        memRow(412e6, { min: 300e6 }),
        memRow(2.1e9, { metric: "memory_swap_used_bytes" }),
      ],
    },
    CFG,
    NOW_S,
  );
  const mem = obs.find((o) => o.key === "memory");
  assert.equal(mem.state, "breach");
  assert.match(mem.text, /412 MB/);
  assert.match(mem.text, /low-water 300 MB/);
  assert.match(mem.text, /swap in use 2\.1 GB/);
});

test("memory: swap alone never breaches — it is context, not a trigger", () => {
  const obs = evaluate(
    { ...healthy(), memory: [memRow(4e9), memRow(6e9, { metric: "memory_swap_used_bytes" })] },
    CFG,
    NOW_S,
  );
  assert.equal(obs.find((o) => o.key === "memory").state, "clear");
  assert.equal(obs.filter((o) => o.key === "memory").length, 1);
});

test("memory: a thin window is unknown — 'sustained' means enough samples", () => {
  const obs = evaluate({ ...healthy(), memory: [memRow(10e6, { samples: 2 })] }, CFG, NOW_S);
  const mem = obs.find((o) => o.key === "memory");
  assert.equal(mem.state, "unknown");
  assert.match(mem.note, /2 samples < 5/);
});

test("memory: no rows at all is unknown, not clear", () => {
  const obs = evaluate({ ...healthy(), memory: [] }, CFG, NOW_S);
  assert.equal(obs.find((o) => o.key === "memory").state, "unknown");
});

// ── vector-silent ────────────────────────────────────────────────────────────

test("silent: a stale newest observation breaches with its age", () => {
  const obs = evaluate({ ...healthy(), freshness: [freshRow(14 * 60)] }, CFG, NOW_S);
  const s = obs.find((o) => o.key === "silent");
  assert.equal(s.state, "breach");
  assert.match(s.text, /\*\*14m\*\* old/);
  assert.match(s.text, /blind/); // says out loud that the other signals are dark
});

test("silent: the host missing entirely is the dead-agent case, and breaches", () => {
  const obs = evaluate({ ...healthy(), freshness: [] }, CFG, NOW_S);
  const s = obs.find((o) => o.key === "silent");
  assert.equal(s.state, "breach");
  assert.match(s.text, /no host_vitals rows/);
});

test("silent: recovery needs to be well inside the threshold, not just under it", () => {
  const at = (age) =>
    evaluate({ ...healthy(), freshness: [freshRow(age)] }, CFG, NOW_S)
      .find((o) => o.key === "silent").state;
  assert.equal(at(11 * 60), "breach");
  assert.equal(at(8 * 60), "unknown"); // under 600s but inside the band
  assert.equal(at(60), "clear"); // a live agent lands here every beat
});

// ── the alert-once gate ──────────────────────────────────────────────────────

const breach = (key, text = "boom") => ({ key, state: "breach", text });
const clear = (key, clearText = "ok") => ({ key, state: "clear", clearText });

test("gate: a breach alerts once, then stays quiet for the rest of the day", () => {
  const first = gate([breach("disk:/Volumes/dev")], {}, NOW_MS);
  assert.deepEqual(first.alerts, ["boom"]);
  assert.equal(first.next.alerted["disk:/Volumes/dev"], "2026-07-25");
  assert.ok(first.next.active["disk:/Volumes/dev"]);

  const later = gate([breach("disk:/Volumes/dev")], first.next, NOW_MS + 6 * 60_000);
  assert.deepEqual(later.alerts, []);
});

test("gate: the same signal alerts again the next day", () => {
  const day1 = gate([breach("memory")], {}, NOW_MS);
  const day2 = gate([breach("memory")], day1.next, NOW_MS + 86_400_000);
  assert.deepEqual(day2.alerts, ["boom"]);
});

test("gate: recovery posts once per episode, then goes quiet", () => {
  const fired = gate([breach("memory")], {}, NOW_MS);
  const recovered = gate([clear("memory")], fired.next, NOW_MS + 60_000);
  assert.deepEqual(recovered.clears, ["ok"]);
  assert.equal(recovered.next.active.memory, undefined);

  const still = gate([clear("memory")], recovered.next, NOW_MS + 120_000);
  assert.deepEqual(still.clears, []); // nothing to recover from
});

test("gate: a same-day re-breach stays silent but still earns its recovery line", () => {
  const fired = gate([breach("disk:/")], {}, NOW_MS);
  const recovered = gate([clear("disk:/")], fired.next, NOW_MS + 60_000);
  const again = gate([breach("disk:/")], recovered.next, NOW_MS + 120_000);
  assert.deepEqual(again.alerts, []); // alert-once per day holds
  assert.ok(again.next.active["disk:/"]); // but the episode is tracked
  const done = gate([clear("disk:/")], again.next, NOW_MS + 180_000);
  assert.deepEqual(done.clears, ["ok"]);
});

test("gate: a clear with no prior alert says nothing at all", () => {
  const g = gate([clear("memory")], {}, NOW_MS);
  assert.deepEqual(g.clears, []);
  assert.deepEqual(g.alerts, []);
});

test("gate: unknown holds state — it neither alerts nor cancels an episode", () => {
  const fired = gate([breach("silent")], {}, NOW_MS);
  const held = gate([{ key: "silent", state: "unknown" }], fired.next, NOW_MS + 60_000);
  assert.deepEqual(held.alerts, []);
  assert.deepEqual(held.clears, []);
  assert.ok(held.next.active.silent); // still firing as far as the gate knows
});

test("gate: the day map is pruned so the doc cannot grow without bound", () => {
  const stale = { alerted: { "disk:/old": "2026-01-01", memory: "2026-07-25" }, active: {} };
  const g = gate([], stale, NOW_MS);
  assert.equal(g.next.alerted["disk:/old"], undefined);
  assert.equal(g.next.alerted.memory, "2026-07-25");
});

test("gate: yesterday's key survives pruning (a breach at 23:59 must not re-fire at 00:01)", () => {
  const y = { alerted: { memory: "2026-07-24" }, active: {} };
  const g = gate([], y, NOW_MS);
  assert.equal(g.next.alerted.memory, "2026-07-24");
});

// ── the message ──────────────────────────────────────────────────────────────

test("message: nothing to say produces no post", () => {
  assert.equal(message([], []), null);
});

test("message: alerts and recoveries share one post, headed by the worse news", () => {
  assert.match(message(["a"], ["b"]), /^\*\*Host vitals\*\*\na\nb$/);
  assert.match(message([], ["b"]), /^\*\*Host vitals — recovered\*\*\nb$/);
});

// ── end to end, through evaluate → gate → message ─────────────────────────────

test("a healthy box produces no Discord traffic at all", () => {
  const obs = evaluate(healthy(), CFG, NOW_S);
  assert.deepEqual(obs.filter((o) => o.state === "breach"), []);
  const g = gate(obs, {}, NOW_MS);
  assert.equal(message(g.alerts, g.clears), null);
});

test("the full-disk story: fill, hold, clean up, hold", () => {
  const full = { ...healthy(), disk: [diskRow("/", 0.5), diskRow("/Volumes/dev", 0.93)] };
  const ok = healthy();

  let state = {};
  const beat = (rows, ms) => {
    const g = gate(evaluate(rows, CFG, ms / 1000), state, ms);
    state = g.next;
    return message(g.alerts, g.clears);
  };

  const t = NOW_MS;
  assert.match(beat(full, t), /🟠 \*\*disk\*\* `\/Volumes\/dev` at \*\*93\.0%\*\*/);
  assert.equal(beat(full, t + 5 * 60_000), null); // held — no flapping
  assert.match(beat(ok, t + 10 * 60_000), /✅ \*\*disk\*\* `\/Volumes\/dev` back to 50\.0%/);
  assert.equal(beat(ok, t + 15 * 60_000), null); // quiet once recovered
});

// ── the AE SQL client + the beat's wiring ────────────────────────────────────

const okJson = (data) => ({ ok: true, json: async () => ({ data }) });

test("aeQuery: POSTs the statement to the account's SQL endpoint as a bearer", async () => {
  let seen;
  const rows = await aeQuery(
    { CF_ACCOUNT_ID: "acc123", CF_AE_READ_TOKEN: "tok" },
    "SELECT 1",
    async (url, init) => {
      seen = { url, init };
      return okJson([{ a: 1 }]);
    },
  );
  assert.deepEqual(rows, [{ a: 1 }]);
  assert.equal(seen.url, "https://api.cloudflare.com/client/v4/accounts/acc123/analytics_engine/sql");
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers.authorization, "Bearer tok");
  assert.equal(seen.init.body, "SELECT 1"); // raw SQL body, not JSON-wrapped
});

test("aeQuery: a missing read token fails loudly rather than reading nothing", async () => {
  await assert.rejects(
    () => aeQuery({ CF_ACCOUNT_ID: "acc" }, "SELECT 1", async () => okJson([])),
    /CF_AE_READ_TOKEN/,
  );
});

test("aeQuery: an API error surfaces the status", async () => {
  await assert.rejects(
    () =>
      aeQuery({ CF_ACCOUNT_ID: "a", CF_AE_READ_TOKEN: "t" }, "SELECT 1", async () => ({
        ok: false,
        status: 403,
        text: async () => "Authentication error",
      })),
    /AE SQL 403/,
  );
});

/** Drive the whole beat with a stubbed AE and a stubbed Discord. */
function beatRig(rows) {
  const posted = [];
  const beats = [];
  let doc = {};
  const env = { CF_ACCOUNT_ID: "acc", CF_AE_READ_TOKEN: "tok" };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    const sql = init.body;
    const which = sql.includes("filesystem_used_ratio")
      ? "disk"
      : sql.includes("memory_available_bytes")
        ? "memory"
        : sql.includes("bus.fleet.supervisor.tick")
          ? "bus"
          : "freshness";
    return okJson(rows[which] ?? []);
  };
  const deps = {
    loadState: async () => ({ doc, save: async () => {} }),
    sendAlert: async (_e, content) => posted.push(content),
    heartbeat: (_e, beat, outcome, stats) => beats.push({ beat, outcome, stats }),
  };
  return {
    env,
    deps,
    posted,
    beats,
    get doc() { return doc; },
    restore: () => { globalThis.fetch = realFetch; },
  };
}

test("runVitals: healthy box → no post, one ok heartbeat", async (t) => {
  const rig = beatRig(healthy());
  t.after(rig.restore);
  await runVitals(rig.env, rig.deps, NOW_MS);
  assert.deepEqual(rig.posted, []);
  assert.equal(rig.beats.length, 1);
  assert.equal(rig.beats[0].beat, "vitals");
  assert.equal(rig.beats[0].outcome, "ok");
  assert.equal(rig.beats[0].stats.api_calls, 4);
  assert.equal(rig.beats[0].stats.runs_seen, 0); // signals breaching
  assert.equal(rig.beats[0].stats.repos, 6); // signals: 2 mounts + mem + swap + silence + bus
});

test("runVitals: a breach posts once and records the day in the shared KV doc", async (t) => {
  const rig = beatRig({ ...healthy(), memory: [memRow(100e6)] });
  t.after(rig.restore);
  await runVitals(rig.env, rig.deps, NOW_MS);
  assert.equal(rig.posted.length, 1);
  assert.match(rig.posted[0], /\*\*Host vitals\*\*/);
  assert.match(rig.posted[0], /🟠 \*\*memory\*\*/);
  assert.equal(rig.doc.vitals.alerted.memory, "2026-07-25");

  await runVitals(rig.env, rig.deps, NOW_MS + 5 * 60_000);
  assert.equal(rig.posted.length, 1); // alert-once holds across beats
});

test("runVitals: a failed Discord post does not burn the day's alert", async (t) => {
  const rig = beatRig({ ...healthy(), memory: [memRow(100e6)] });
  t.after(rig.restore);
  rig.deps.sendAlert = async () => { throw new Error("discord 503"); };
  await assert.rejects(() => runVitals(rig.env, rig.deps, NOW_MS), /discord 503/);
  // The gate was never persisted, so the next beat is still eligible to alert.
  assert.equal(rig.doc.vitals, undefined);
  assert.equal(rig.beats.at(-1).outcome, "error");
});

test("runVitals: an AE failure is an error heartbeat, not a silent all-clear", async (t) => {
  const rig = beatRig(healthy());
  t.after(rig.restore);
  globalThis.fetch = async () => ({ ok: false, status: 500, text: async () => "boom" });
  await assert.rejects(() => runVitals(rig.env, rig.deps, NOW_MS), /AE SQL 500/);
  assert.deepEqual(rig.posted, []);
  assert.equal(rig.beats.at(-1).outcome, "error");
  assert.equal(rig.beats.at(-1).stats.errors, 1);
});

// ── swap (its own trigger since 2026-07-25; baseline on the box was 88%) ──────

test("swap: ratio over the threshold breaches, with used-of-total in the text", () => {
  const v = evaluate(
    {
      ...healthy(),
      memory: [
        memRow(4e9),
        memRow(7.6e9, { metric: "memory_swap_used_bytes" }),
        memRow(8e9, { metric: "memory_swap_total_bytes" }),
      ],
    },
    CFG,
    NOW_MS / 1000,
  );
  const swap = v.find((x) => x.key === "swap");
  assert.equal(swap.state, "breach");
  assert.match(swap.text, /95\.0%/);
  assert.match(swap.text, /of 8\.0 GB/);
});

test("swap: the measured 88% baseline does NOT fire", () => {
  const v = evaluate(
    {
      ...healthy(),
      memory: [
        memRow(4e9),
        memRow(7.05e9, { metric: "memory_swap_used_bytes" }),
        memRow(8e9, { metric: "memory_swap_total_bytes" }),
      ],
    },
    CFG,
    NOW_MS / 1000,
  );
  assert.equal(v.find((x) => x.key === "swap").state, "unknown"); // hysteresis band
});

test("swap: falls back to absolute bytes when swap_total is absent", () => {
  const v = evaluate(
    { ...healthy(), memory: [memRow(4e9), memRow(7.9e9, { metric: "memory_swap_used_bytes" })] },
    CFG,
    NOW_MS / 1000,
  );
  const swap = v.find((x) => x.key === "swap");
  assert.equal(swap.state, "breach");
  assert.match(swap.text, /total unknown/);
});

// ── supervisor-bus silent (self-arming; obs-config#174 shadow) ────────────────

test("sqlBusFreshness: double2 value, bus metric+collector filter, 7-day lookback", () => {
  const sql = sqlBusFreshness(7 * 86_400);
  assert.match(sql, /FROM host_vitals/);
  assert.match(sql, /blob2 = 'bus\.fleet\.supervisor\.tick'/);
  assert.match(sql, /blob4 = 'bus'/);
  assert.match(sql, /max\(double2\) AS newest_source_ts/);
  assert.doesNotMatch(sql, /max\(timestamp\)/);
  assert.match(sql, /INTERVAL '10080' MINUTE/); // 7 days, in the dialect's MINUTE unit
  assert.doesNotMatch(sql, /blob1/); // no host filter: blob1 is BUS_HOST, not Vector's tag
});

test("bus: a fresh tick is clear", () => {
  const obs = evaluate(healthy(), CFG, NOW_S);
  assert.equal(obs.find((o) => o.key === "bus-silent").state, "clear");
});

test("bus: silence past the threshold (and inside the disarm window) breaches", () => {
  const obs = evaluate({ ...healthy(), bus: [busRow(20 * 60)] }, CFG, NOW_S);
  const b = obs.find((o) => o.key === "bus-silent");
  assert.equal(b.state, "breach");
  assert.match(b.text, /\*\*20m\*\* old/);
  assert.match(b.text, /fleet-bus Worker/);
});

test("bus: SELF-ARMING — no rows at all stays unknown, never a breach", () => {
  // Before the mini's dual-publish ever runs there is nothing to measure; an
  // unarmed lane must not alarm daily about a heartbeat that has not started.
  const obs = evaluate({ ...healthy(), bus: [] }, CFG, NOW_S);
  const b = obs.find((o) => o.key === "bus-silent");
  assert.equal(b.state, "unknown");
  assert.match(b.note, /not armed/);
});

test("bus: SELF-ARMING — a row older than the disarm window goes quiet again", () => {
  // The formally-retired-lane case: 8 days of silence disarms the signal
  // instead of alarming forever about a bus nobody publishes to.
  const obs = evaluate({ ...healthy(), bus: [busRow(8 * 86_400)] }, CFG, NOW_S);
  const b = obs.find((o) => o.key === "bus-silent");
  assert.equal(b.state, "unknown");
  assert.match(b.note, /disarm/);
});

test("bus: the hysteresis band neither alerts nor clears", () => {
  const at = (age) =>
    evaluate({ ...healthy(), bus: [busRow(age)] }, CFG, NOW_S)
      .find((o) => o.key === "bus-silent").state;
  assert.equal(at(11 * 60), "breach");
  assert.equal(at(8 * 60), "unknown"); // under 600s but inside the band
  assert.equal(at(60), "clear");
});

test("bus: thresholds are env-tunable like every other signal", () => {
  const c = config({ VITALS_BUS_SILENT_SEC: "300", VITALS_BUS_DISARM_SEC: "86400" });
  assert.equal(c.busSilentSec, 300);
  assert.equal(c.busDisarmSec, 86_400);
  assert.equal(config({}).busSilentSec, 600); // shipped default
  assert.equal(config({}).busDisarmSec, 7 * 86_400);
});
