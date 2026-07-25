// vitals — threshold alerting on the host-vitals lane (observability-config#161).
//
// The mini's Vector agent (supervisor ops/vector/vector.toml) pushes host_metrics
// into the `host_vitals` Analytics Engine dataset via the host-vitals Worker. That
// Worker is deliberately push-only — no crons, no KV, no outbound tokens — so the
// *alerting* half lives here, in the Worker that already IS the alert lane: the
// red-CI gate's KV alert-once store, the Discord bot-token client, and the
// `#dev` channel resolution are all right here, and one lane means one dedupe
// store and one place a signal can be silenced. (Putting a cron in host-vitals
// would hand the ingest endpoint a Discord bot token and a KV binding it has no
// other use for, and split alert state across two Workers.)
//
// Three signals, all read back through the Analytics Engine SQL API:
//
//   disk    — filesystem_used_ratio per mount, window-averaged, > VITALS_DISK_ALERT_RATIO
//   memory  — memory_available_bytes, window-averaged, < VITALS_MEM_ALERT_BYTES
//             (the 8 GB box is the whole reason this lane exists — the InfluxDB
//             OOM loop of 2026-07-19 is what it is watching for)
//   silent  — newest source timestamp for the host older than VITALS_SILENT_SEC.
//             This is the heartbeat that replaces the retired TIG stack-watchdog's
//             implicit liveness: no rows means the agent, the box, or the ingest
//             Worker is down, and the other two signals have gone blind.
//
// Column map (workers/host-vitals/README.md — positional, do not guess):
//   host_vitals  blob1=host  blob2=metric name  blob3=tags "k=v,k=v" sorted
//                blob4=collector  double1=value  double2=source epoch seconds
//
// Freshness reads double2 (SOURCE time) because AE stamps rows at WRITE time: a
// Vector that buffered for an hour and then flushed would look perfectly healthy
// on the row timestamp. Everything else windows on the row `timestamp`, which is
// what the dataset is indexed on and is within ~2 min of source time in the
// steady state (60 s scrape + 60 s sink batch).

const AE_SQL = (acc) =>
  `https://api.cloudflare.com/client/v4/accounts/${acc}/analytics_engine/sql`;

// ── config ───────────────────────────────────────────────────────────────────

const num = (v, dflt) => {
  // An unset or blank var must fall back, not become 0 — Number("") is 0, and a
  // silently-zeroed VITALS_SILENT_SEC would alert on every single beat forever.
  if (v === undefined || v === null || String(v).trim() === "") return dflt;
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
};

/** Thresholds are env vars, all of them, because we have well under a day of
 *  baseline: every default below is a conservative guess to be re-cut after a
 *  week of real data (see README "Thresholds"). Retuning is a wrangler.toml
 *  edit, not a code change. */
export function config(env = {}) {
  const alertBytes = num(env.VITALS_MEM_ALERT_BYTES, 500e6);
  return {
    host: env.VITALS_HOST || "tommys-mac-mini.local",
    mounts: String(env.VITALS_MOUNTS || "/,/Volumes/dev")
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean),
    diskAlertRatio: num(env.VITALS_DISK_ALERT_RATIO, 0.9),
    diskClearRatio: num(env.VITALS_DISK_CLEAR_RATIO, 0.87),
    diskWindowMin: num(env.VITALS_DISK_WINDOW_MIN, 10),
    memAlertBytes: alertBytes,
    memClearBytes: num(env.VITALS_MEM_CLEAR_BYTES, alertBytes * 1.5),
    memWindowMin: num(env.VITALS_MEM_WINDOW_MIN, 10),
    memMinSamples: num(env.VITALS_MEM_MIN_SAMPLES, 5),
    silentSec: num(env.VITALS_SILENT_SEC, 600),
    freshLookbackMin: num(env.VITALS_FRESH_LOOKBACK_MIN, 60),
  };
}

// ── the queries ──────────────────────────────────────────────────────────────
//
// `sum(double1 * _sample_interval) / sum(_sample_interval)` — not avg(double1).
// Analytics Engine samples under load and hands back a per-row weight; the
// weighted mean is the correct average at any sampling rate and collapses to the
// plain mean at 1:1 (which is where this dataset sits today). Same reason
// `sum(_sample_interval)` — not count() — is the row count.

/** Per-mount disk fullness over the window. Grouped on the whole blob3 tag
 *  string; the mountpoint is picked out client-side (mountpointOf) rather than
 *  with SQL string surgery — the AE SQL dialect's string functions are a subset
 *  and this keeps the parsing testable. */
export function sqlDisk(windowMin) {
  return [
    "SELECT blob1 AS host,",
    "       blob3 AS tags,",
    "       sum(double1 * _sample_interval) / sum(_sample_interval) AS used_ratio,",
    "       sum(_sample_interval) AS samples",
    "FROM host_vitals",
    "WHERE blob2 = 'filesystem_used_ratio'",
    `  AND timestamp > now() - INTERVAL '${windowMin}' MINUTE`,
    "GROUP BY host, tags",
    "FORMAT JSON",
  ].join("\n");
}

/** Available memory over the window, plus swap-in-use as context on the alert
 *  line. One query, two metrics — `IN` is not in the documented AE SQL subset,
 *  so this ORs the two names. */
export function sqlMemory(windowMin) {
  return [
    "SELECT blob1 AS host,",
    "       blob2 AS metric,",
    "       sum(double1 * _sample_interval) / sum(_sample_interval) AS avg_value,",
    "       min(double1) AS min_value,",
    "       max(double1) AS max_value,",
    "       sum(_sample_interval) AS samples",
    "FROM host_vitals",
    "WHERE (blob2 = 'memory_available_bytes' OR blob2 = 'memory_swap_used_bytes')",
    `  AND timestamp > now() - INTERVAL '${windowMin}' MINUTE`,
    "GROUP BY host, metric",
    "FORMAT JSON",
  ].join("\n");
}

/** Newest SOURCE timestamp per host — the liveness probe. A host missing from
 *  this result entirely has written nothing in the lookback and is treated as
 *  silent (that is the dead-agent case, not a data gap). */
export function sqlFreshness(lookbackMin) {
  return [
    "SELECT blob1 AS host,",
    "       max(double2) AS newest_source_ts,",
    "       sum(_sample_interval) AS samples",
    "FROM host_vitals",
    `WHERE timestamp > now() - INTERVAL '${lookbackMin}' MINUTE`,
    "GROUP BY host",
    "FORMAT JSON",
  ].join("\n");
}

// ── row helpers ──────────────────────────────────────────────────────────────

/** Pull `mountpoint=` out of a collapsed blob3 tag string, e.g.
 *  "device=disk3s5,filesystem=apfs,mountpoint=/Volumes/dev" -> "/Volumes/dev".
 *  Values never contain a comma (host_metrics tags are device names, filesystem
 *  types and paths), so a plain split is safe. */
export function mountpointOf(tags) {
  for (const part of String(tags || "").split(",")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i) === "mountpoint") return part.slice(i + 1);
  }
  return null;
}

/** Case-insensitive host match. The README writes the mini as
 *  `tommys-mac-mini.local`, but the box's actual gethostname() is
 *  `Tommys-Mac-mini.local` — and blob1 is whatever Vector's host tag says.
 *  Comparing case-insensitively means the alert lane cannot be silently blinded
 *  by hostname casing (a silent signal that never fires is worse than useless). */
const sameHost = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();

// ClickHouse's JSON format renders UInt64 (sum(_sample_interval)) as a *string*.
// Everything numeric goes through Number() rather than trusting the JSON type.
const n = (v) => Number(v);

// ── formatting ───────────────────────────────────────────────────────────────

const pct = (r) => `${(r * 100).toFixed(1)}%`;

export function bytes(b) {
  if (!Number.isFinite(b)) return "?";
  return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;
}

export function duration(sec) {
  if (!Number.isFinite(sec)) return "?";
  const s = Math.max(0, Math.round(sec));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// ── evaluation (pure) ────────────────────────────────────────────────────────
//
// Every signal lands in one of three states:
//   "breach"  — over the alert threshold
//   "clear"   — back under the *clear* threshold (deliberately tighter than the
//               alert threshold: the gap is hysteresis, so a value hovering on
//               the line cannot alternate alert/recovery every 5 minutes)
//   "unknown" — inside the hysteresis band, or not enough data to judge.
//               "unknown" never alerts and never clears. Missing data must not
//               manufacture an all-clear.
//
// The one exception is `silent`, where absent data IS the signal.

/** @returns {{key:string,state:"breach"|"clear"|"unknown",text?:string,clearText?:string,note?:string}[]} */
export function evaluate({ disk = [], memory = [], freshness = [] }, cfg, nowSec) {
  const out = [];

  // ── disk, per configured mount ──
  const byMount = new Map();
  for (const row of disk) {
    if (!sameHost(row.host, cfg.host)) continue;
    const mp = mountpointOf(row.tags);
    if (mp) byMount.set(mp, row);
  }
  for (const mount of cfg.mounts) {
    const key = `disk:${mount}`;
    const row = byMount.get(mount);
    if (!row || n(row.samples) < 1) {
      out.push({ key, state: "unknown", note: `${mount}: no samples` });
      continue;
    }
    const ratio = n(row.used_ratio);
    if (!Number.isFinite(ratio)) {
      out.push({ key, state: "unknown", note: `${mount}: non-numeric ratio` });
    } else if (ratio > cfg.diskAlertRatio) {
      out.push({
        key,
        state: "breach",
        text:
          `🟠 **disk** \`${mount}\` at **${pct(ratio)}** used ` +
          `(${cfg.diskWindowMin}-min avg, threshold ${pct(cfg.diskAlertRatio)})`,
      });
    } else if (ratio < cfg.diskClearRatio) {
      out.push({
        key,
        state: "clear",
        clearText: `✅ **disk** \`${mount}\` back to ${pct(ratio)} used`,
      });
    } else {
      out.push({ key, state: "unknown", note: `${mount}: ${pct(ratio)} in hysteresis band` });
    }
  }

  // ── memory ──
  const mem = memory.find(
    (r) => sameHost(r.host, cfg.host) && r.metric === "memory_available_bytes",
  );
  const swap = memory.find(
    (r) => sameHost(r.host, cfg.host) && r.metric === "memory_swap_used_bytes",
  );
  // Swap is context on the alert line, never a trigger on its own: on macOS a
  // few hundred MB of swap in use is normal and says nothing by itself.
  const swapNote = swap && Number.isFinite(n(swap.avg_value))
    ? ` — swap in use ${bytes(n(swap.avg_value))}`
    : "";
  if (!mem || n(mem.samples) < cfg.memMinSamples) {
    out.push({
      key: "memory",
      state: "unknown",
      note: `memory: ${mem ? n(mem.samples) : 0} samples < ${cfg.memMinSamples}`,
    });
  } else {
    const avg = n(mem.avg_value);
    if (!Number.isFinite(avg)) {
      out.push({ key: "memory", state: "unknown", note: "memory: non-numeric avg" });
    } else if (avg < cfg.memAlertBytes) {
      out.push({
        key: "memory",
        state: "breach",
        text:
          `🟠 **memory** only **${bytes(avg)}** available ` +
          `(${cfg.memWindowMin}-min avg, threshold ${bytes(cfg.memAlertBytes)}; ` +
          `low-water ${bytes(n(mem.min_value))})${swapNote}`,
      });
    } else if (avg > cfg.memClearBytes) {
      out.push({
        key: "memory",
        state: "clear",
        clearText: `✅ **memory** back to ${bytes(avg)} available`,
      });
    } else {
      out.push({ key: "memory", state: "unknown", note: `memory: ${bytes(avg)} in hysteresis band` });
    }
  }

  // ── vector-silent ──
  const fresh = freshness.find((r) => sameHost(r.host, cfg.host));
  if (!fresh) {
    out.push({
      key: "silent",
      state: "breach",
      text:
        `🔴 **vector silent** — \`${cfg.host}\` has written **no host_vitals rows** ` +
        `in the last ${cfg.freshLookbackMin} min. The agent, the box, or the ingest ` +
        `Worker is down; disk and memory alerting is blind until it is back.`,
    });
  } else {
    const age = nowSec - n(fresh.newest_source_ts);
    if (!Number.isFinite(age)) {
      out.push({ key: "silent", state: "unknown", note: "freshness: non-numeric timestamp" });
    } else if (age > cfg.silentSec) {
      out.push({
        key: "silent",
        state: "breach",
        text:
          `🔴 **vector silent** — newest \`${cfg.host}\` observation is **${duration(age)}** old ` +
          `(threshold ${duration(cfg.silentSec)}). Disk and memory alerting is blind until it is back.`,
      });
    } else if (age <= cfg.silentSec / 2) {
      out.push({
        key: "silent",
        state: "clear",
        clearText: `✅ **vector** reporting again — newest observation ${duration(age)} old`,
      });
    } else {
      out.push({ key: "silent", state: "unknown", note: `freshness: ${duration(age)} in hysteresis band` });
    }
  }

  return out;
}

// ── the alert-once gate (pure) ───────────────────────────────────────────────
//
// Same shape as the red-CI gate, keyed per (signal, mount, day):
//   alerted[key] = "YYYY-MM-DD"  — the last day this signal was announced
//   active[key]  = "<iso>"       — when the current breach episode started
//
// A breach announces at most once per UTC day. Recovery is cheap here because
// `active` already has to exist to make "once per episode" meaningful: when a
// signal that is currently active goes clear, it posts one ✅ line and the
// episode ends. `alerted` is deliberately NOT reset by recovery — a mount that
// crosses 90%, is cleaned up, and fills again the same day stays quiet until
// tomorrow. That is the alert-once contract; a flapping disk should produce a
// human decision, not a stream of messages.

const dayKey = (ms) => new Date(ms).toISOString().slice(0, 10);

export function gate(observations, prev = {}, nowMs = Date.now()) {
  const today = dayKey(nowMs);
  const yesterday = dayKey(nowMs - 86_400_000);
  const alerted = { ...(prev.alerted || {}) };
  const active = { ...(prev.active || {}) };
  const alerts = [];
  const clears = [];

  for (const obs of observations) {
    if (obs.state === "breach") {
      if (alerted[obs.key] !== today) {
        alerts.push(obs.text);
        alerted[obs.key] = today;
      }
      // Track the episode either way, so a same-day re-breach still gets a
      // recovery line when it ends.
      if (!active[obs.key]) active[obs.key] = new Date(nowMs).toISOString();
    } else if (obs.state === "clear") {
      if (active[obs.key]) {
        clears.push(obs.clearText);
        delete active[obs.key];
      }
    }
    // "unknown" holds everything exactly as it is.
  }

  // Keep the doc bounded: a day key is only ever compared against today, so
  // anything older than yesterday can never suppress a future alert.
  for (const k of Object.keys(alerted)) {
    if (alerted[k] !== today && alerted[k] !== yesterday) delete alerted[k];
  }

  return { alerts, clears, next: { alerted, active } };
}

/** The Discord payload for one beat, or null when there is nothing to say. */
export function message(alerts, clears) {
  if (!alerts.length && !clears.length) return null;
  const lines = [...alerts, ...clears];
  const header = alerts.length ? "**Host vitals**" : "**Host vitals — recovered**";
  return `${header}\n${lines.join("\n")}`;
}

// ── the AE SQL client ────────────────────────────────────────────────────────

/** POST one statement to the Analytics Engine SQL API and return its rows.
 *  Needs an "Account Analytics Read" token (cloudflare-tfvend `analytics_read`)
 *  in CF_AE_READ_TOKEN — writing AE needs no credential, reading it does. */
export async function aeQuery(env, sql, fetchImpl = fetch) {
  if (!env.CF_AE_READ_TOKEN) throw new Error("CF_AE_READ_TOKEN is not set");
  if (!env.CF_ACCOUNT_ID) throw new Error("CF_ACCOUNT_ID is not set");
  const res = await fetchImpl(AE_SQL(env.CF_ACCOUNT_ID), {
    method: "POST",
    headers: { authorization: `Bearer ${env.CF_AE_READ_TOKEN}` },
    body: sql,
  });
  if (!res.ok) {
    throw new Error(`AE SQL ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
  const doc = await res.json();
  return Array.isArray(doc?.data) ? doc.data : [];
}

// ── the beat ─────────────────────────────────────────────────────────────────

/** The vitals beat. `deps` is injected by index.js (loadState / sendAlert /
 *  heartbeat) so this module stays free of the GitHub + Discord plumbing and
 *  the whole beat is drivable from a test. */
export async function runVitals(env, deps, nowMs = Date.now()) {
  const t0 = Date.now();
  const cfg = config(env);
  const stats = {
    repos: 0, runs_seen: 0, runs_written: 0, alerts_sent: 0,
    errors: 0, api_calls: 0, rate_remaining: -1,
  };
  try {
    const [disk, memory, freshness] = await Promise.all([
      aeQuery(env, sqlDisk(cfg.diskWindowMin)),
      aeQuery(env, sqlMemory(cfg.memWindowMin)),
      aeQuery(env, sqlFreshness(cfg.freshLookbackMin)),
    ]);
    stats.api_calls = 3;

    const observations = evaluate({ disk, memory, freshness }, cfg, nowMs / 1000);
    stats.repos = observations.length; // signals evaluated (see README: beat column map)
    stats.runs_seen = observations.filter((o) => o.state === "breach").length;

    const state = await deps.loadState(env);
    const { alerts, clears, next } = gate(observations, state.doc.vitals, nowMs);

    const content = message(alerts, clears);
    if (content) {
      // Post BEFORE persisting the gate, exactly like the red-CI path: a failed
      // Discord post must leave the signal eligible on the next beat rather than
      // burning the once-per-day announcement on a message nobody received.
      await deps.sendAlert(env, content);
      stats.alerts_sent = alerts.length + clears.length;
    }
    state.doc.vitals = next;
    await state.save();

    deps.heartbeat(env, "vitals", "ok", stats, t0);
    console.log(
      `vitals ok: ${stats.runs_seen} breaching of ${stats.repos} signals, ` +
      `${alerts.length} alert(s) + ${clears.length} recovery line(s) — ` +
      observations.map((o) => `${o.key}=${o.state}`).join(" "),
    );
  } catch (err) {
    stats.errors += 1;
    deps.heartbeat(env, "vitals", "error", stats, t0);
    console.error(`vitals failed: ${err}`);
    throw err; // surface to the cron dashboard, same as the other beats
  }
}
