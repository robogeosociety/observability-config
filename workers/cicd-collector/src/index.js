// cicd-collector — org-wide CI/CD telemetry as a Workers cron, writing to
// Workers Analytics Engine (WS5 of CICD-everything rgs#167; issue #156).
//
// The Workers port of the parked #149 launchd collector
// (grafana/cicd-collector/collector.py, branch head f9ab362): every GitHub
// Actions pipeline in the org, discovered dynamically each poll — a new repo
// or workflow file shows up within one poll cycle, no config edit. Plus the
// red-CI alert path: with the mini's TIG stack retiring, THIS is the alerting
// lane — a workflow_run failure on a default branch posts a compact #dev
// alert within one 5-minute tick.
//
// Three datasets (one per #149 measurement shape — see README for columns):
//   cicd_workflow_runs     — one row per completed run, write-once.
//   cicd_workflow_inventory— one row per workflow file per hourly beat (the
//                            pipeline map, including never-run pipelines).
//   cicd_collector_polls   — one heartbeat row per beat: repos scanned, runs
//                            written, API calls, errors, rate-limit remaining.
//
// Design deltas from #149 (InfluxDB → Analytics Engine):
//   • AE is append-only and stamps rows at write time — the "identical
//     tags+timestamp overwrites in place" idempotency trick doesn't exist.
//     The overlap window stays (reliability), and a KV seen-set
//     (`run_id:attempt`) makes writes once-only instead.
//   • The true completion time lands in the completed_at double (double5) —
//     query on that, not the row timestamp (backfilled rows share write time).
//   • Repo discovery is GET /installation/repositories (the github-heartbeat
//     pattern): one call, app-visible non-archived repos, includes
//     default_branch (which the alert path needs anyway).
//   • The workflow tag on 5-min ticks is run.name (fallback: file basename);
//     the file-level name map costs one call per repo, so it rides the hourly
//     inventory beat only.
//   • Re-runs land as their own row (attempt bump ⇒ new `run_id:attempt` key),
//     same as #149 — attempt history is kept; latest attempt is the verdict.
//
// A third beat (`vitals`, src/vitals.js — observability-config#161) rides the
// same lane: it reads the mini's host_vitals dataset back through the Analytics
// Engine SQL API and alerts on disk / memory / vector-silence. It lives here
// rather than in the host-vitals ingest Worker because THIS is where the alert
// plumbing already is — the KV alert-once store, the Discord bot client, the
// #dev channel resolution. One lane, one dedupe store.

import { runVitals } from "./vitals.js";

const OVERLAP_MIN = 30; // window overlap — #149's reliability margin
const BACKFILL_DAYS = 7; // first poll (fresh KV) backfills this much
const PER_PAGE = 100;
const MAX_RUN_PAGES = 5; // per repo per poll — bounds a runaway backfill
const SEEN_CAP = 4000; // write-once run keys kept, oldest dropped
const ALERTED_CAP = 500; // alert-once run keys kept, oldest dropped
const ALERT_MAX_LINES = 10; // cap per message so an org-wide red day stays readable
const STATE_KEY = "state";

// One Worker, three beats — dispatched on the cron expression (they must match
// wrangler.toml's `crons` verbatim; anything unrecognised falls back to the poll).
const POLL_CRON = "*/5 * * * *";
const INVENTORY_CRON = "7 * * * *";
const VITALS_CRON = "3-58/5 * * * *"; // +3 off the poll tick — see wrangler.toml

const enc = new TextEncoder();

// ── GitHub App auth (the deploy-gate / github-heartbeat pattern) ─────────────

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function appJwt(env) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(enc.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const payload = b64url(enc.encode(JSON.stringify({ iat: now - 30, exp: now + 540, iss: env.GH_APP_ID })));
  const pem = env.GH_APP_PRIVATE_KEY_PKCS8.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const der = Uint8Array.from(atob(pem), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey(
    "pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(`${header}.${payload}`));
  return `${header}.${payload}.${b64url(sig)}`;
}

async function ghRaw(token, method, path) {
  return fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "user-agent": "rgs-cicd-collector",
    },
  });
}

/** GET a GitHub path; counts the call and tracks the rate-limit header on
 *  `stats`. Throws on non-OK — callers decide blast radius (per-repo failures
 *  are counted, not fatal; discovery failures abort the beat). */
async function gh(stats, token, path) {
  stats.api_calls += 1;
  const res = await ghRaw(token, "GET", path);
  const rem = res.headers.get("x-ratelimit-remaining");
  if (rem !== null) stats.rate_remaining = Number(rem);
  if (!res.ok) throw new Error(`gh GET ${path} → ${res.status}`);
  return res.json();
}

async function orgInstallationToken(env, stats) {
  const jwt = await appJwt(env);
  stats.api_calls += 2;
  const inst = await ghRaw(jwt, "GET", `/orgs/${env.GH_ORG}/installation`);
  if (!inst.ok) throw new Error(`org installation lookup ${inst.status}`);
  const { id } = await inst.json();
  const tok = await fetch(`https://api.github.com/app/installations/${id}/access_tokens`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${jwt}`,
      accept: "application/vnd.github+json",
      "user-agent": "rgs-cicd-collector",
    },
  });
  if (!tok.ok) throw new Error(`installation token ${tok.status}`);
  return (await tok.json()).token;
}

/** Non-archived repos the app installation can see (single page: the org is
 *  well under 100 repos; the heartbeat Worker makes the same assumption). */
async function listRepos(stats, token) {
  const data = await gh(stats, token, "/installation/repositories?per_page=100");
  return (data.repositories || []).filter((r) => !r.archived);
}

// ── Discord (bot-token REST, the deploy-gate pattern) ────────────────────────

let channelCache = null; // { name, id } — survives warm isolates

async function discord(env, method, path, body) {
  const res = await fetch(`https://discord.com/api/v10${path}`, {
    method,
    headers: {
      authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`discord ${method} ${path} ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.status === 204 ? null : res.json();
}

async function alertChannelId(env) {
  if (channelCache?.name === env.ALERT_CHANNEL) return channelCache.id;
  const guilds = await discord(env, "GET", "/users/@me/guilds");
  for (const g of guilds) {
    const chans = await discord(env, "GET", `/guilds/${g.id}/channels`);
    const hit = chans.find((c) => c.type === 0 && c.name === env.ALERT_CHANNEL);
    if (hit) {
      channelCache = { name: env.ALERT_CHANNEL, id: hit.id };
      return hit.id;
    }
  }
  throw new Error(`channel #${env.ALERT_CHANNEL} not found in any guild the bot is in`);
}

// ── State (poll cursor + write-once + alert-once gates, one KV doc) ──────────
//
// The vitals beat keeps its own gate under `doc.vitals` (see vitals.js); it is
// carried through untouched here, so the two beats can write the same doc
// without stepping on each other.

async function loadState(env) {
  let doc = {};
  try {
    doc = (await env.STATE.get(STATE_KEY, "json")) || {};
  } catch {
    doc = {};
  }
  const seen = Array.isArray(doc.seen) ? doc.seen : [];
  const alerted = Array.isArray(doc.alerted) ? doc.alerted : [];
  return {
    doc,
    seen,
    alerted,
    seenSet: new Set(seen),
    alertedSet: new Set(alerted),
    markSeen(k) { if (!this.seenSet.has(k)) { this.seen.push(k); this.seenSet.add(k); } },
    markAlerted(k) { if (!this.alertedSet.has(k)) { this.alerted.push(k); this.alertedSet.add(k); } },
    async save() {
      this.doc.seen = this.seen.slice(-SEEN_CAP);
      this.doc.alerted = this.alerted.slice(-ALERTED_CAP);
      await env.STATE.put(STATE_KEY, JSON.stringify(this.doc));
    },
  };
}

/** Poll window start: last poll minus overlap; a fresh KV backfills. */
function windowStart(doc, now) {
  const floor = now - BACKFILL_DAYS * 86_400_000;
  const last = Date.parse(doc.last_poll || "") || null;
  if (!last) return new Date(floor);
  return new Date(Math.max(last - OVERLAP_MIN * 60_000, floor));
}

// ── Row mappers (#149's pure mappers, AE-shaped) ─────────────────────────────

const epochS = (v) => (v ? Math.floor(Date.parse(v) / 1000) : null);

/** One completed run → one cicd_workflow_runs row (see README for columns). */
function writeRunRow(env, repoName, run) {
  const finished = epochS(run.updated_at); // completion time, per #149
  const started = epochS(run.run_started_at) ?? finished;
  const created = epochS(run.created_at) ?? started;
  const workflow = run.name || (run.path || "unknown").split("/").pop();
  env.RUNS.writeDataPoint({
    indexes: [repoName],
    blobs: [repoName, workflow, run.head_branch || "", run.event || "", run.conclusion],
    doubles: [
      run.conclusion === "success" ? 1 : 0, // ok
      Math.max(0, finished - started), // duration_s
      Math.max(0, started - created), // queue_s
      run.run_attempt || 1, // run_attempt
      finished, // completed_at — query on this, not the row timestamp
      run.id, // run_id — parity spot-checks against `gh run list`
    ],
  });
}

/** One heartbeat row per beat → cicd_collector_polls. The doubles are named for
 *  the poll beat; the vitals beat reuses the same shape (one heartbeat dataset
 *  for the Worker) with `repos` = signals evaluated and `runs_seen` = signals
 *  breaching. The column map is spelled out in the README. */
function writePollRow(env, beat, outcome, stats, t0) {
  env.POLLS.writeDataPoint({
    indexes: [beat],
    blobs: [beat, outcome],
    doubles: [
      stats.repos, stats.runs_seen, stats.runs_written, stats.alerts_sent,
      stats.errors, stats.api_calls, Date.now() - t0, stats.rate_remaining,
    ],
  });
}

// ── The poll beat (every 5 minutes) ──────────────────────────────────────────

/** Completed runs in `repo` since `sinceIso`, paginated with a hard page cap. */
async function listCompletedRuns(stats, token, repoFull, sinceIso) {
  const runs = [];
  const created = encodeURIComponent(`>=${sinceIso}`);
  for (let page = 1; page <= MAX_RUN_PAGES; page++) {
    const data = await gh(
      stats, token,
      `/repos/${repoFull}/actions/runs?status=completed&created=${created}&per_page=${PER_PAGE}&page=${page}`,
    );
    const batch = data.workflow_runs || [];
    runs.push(...batch);
    if (batch.length < PER_PAGE) break;
  }
  return runs;
}

/** Post one message to the alert channel. The single Discord egress for this
 *  Worker — the red-CI path and the vitals beat share the channel resolution,
 *  the bot client, and this one place to silence the lane. */
async function sendAlert(env, content) {
  const channel = await alertChannelId(env);
  await discord(env, "POST", `/channels/${channel}/messages`, { content });
}

async function postAlerts(env, failures) {
  const lines = failures.slice(0, ALERT_MAX_LINES).map(({ repoName, branch, run }) =>
    `🔴 **${repoName}** ${branch} — ${run.name || "workflow"} ` +
    `[#${run.run_number}](${run.html_url})${(run.run_attempt || 1) > 1 ? ` (attempt ${run.run_attempt})` : ""}`);
  if (failures.length > ALERT_MAX_LINES) lines.push(`… and ${failures.length - ALERT_MAX_LINES} more`);
  await sendAlert(env, `**CI red on a default branch**\n${lines.join("\n")}`);
}

async function poll(env) {
  const t0 = Date.now();
  const stats = { repos: 0, runs_seen: 0, runs_written: 0, alerts_sent: 0,
                  errors: 0, api_calls: 0, rate_remaining: -1 };
  try {
    const token = await orgInstallationToken(env, stats);
    const state = await loadState(env);
    const now = new Date();
    const sinceIso = windowStart(state.doc, now.getTime()).toISOString().replace(/\.\d{3}Z$/, "Z");
    const repos = await listRepos(stats, token);

    const failures = []; // red default-branch runs not yet alerted
    for (const repo of repos) {
      stats.repos += 1;
      try {
        const runs = await listCompletedRuns(stats, token, repo.full_name, sinceIso);
        for (const run of runs) {
          if (!run.conclusion) continue;
          stats.runs_seen += 1;
          const key = `${run.id}:${run.run_attempt || 1}`;
          // Alert gate first, independent of the write gate — a failed post
          // stays eligible for the whole overlap window and retries next tick.
          if (run.conclusion === "failure" &&
              run.head_branch === (repo.default_branch || "main") &&
              !state.alertedSet.has(key)) {
            failures.push({ repoName: repo.name, branch: repo.default_branch || "main", run, key });
          }
          if (state.seenSet.has(key)) continue;
          writeRunRow(env, repo.name, run);
          state.markSeen(key);
          stats.runs_written += 1;
        }
      } catch (err) {
        // One flaky repo must not blank the whole org's poll (#149's rule).
        stats.errors += 1;
        console.warn(`warning: ${repo.full_name}: ${err}`);
      }
    }

    // Cursor + write-once gate advance together, after the AE writes queued —
    // so a crashed poll re-covers its window and dedupe absorbs the overlap.
    state.doc.last_poll = now.toISOString();
    await state.save();

    if (failures.length) {
      console.log(`alerting ${failures.length} red default-branch run(s) to #${env.ALERT_CHANNEL}`);
      await postAlerts(env, failures);
      stats.alerts_sent = failures.length;
      for (const f of failures) state.markAlerted(f.key);
      await state.save();
    }

    writePollRow(env, "poll", "ok", stats, t0);
    console.log(`poll ok: ${JSON.stringify(stats)} (since ${sinceIso})`);
  } catch (err) {
    stats.errors += 1;
    writePollRow(env, "poll", "error", stats, t0);
    console.error(`poll failed: ${err}`);
    throw err; // surface the failure to the cron dashboard too
  }
}

// ── The inventory beat (hourly) ──────────────────────────────────────────────

async function inventory(env) {
  const t0 = Date.now();
  const stats = { repos: 0, runs_seen: 0, runs_written: 0, alerts_sent: 0,
                  errors: 0, api_calls: 0, rate_remaining: -1 };
  try {
    const token = await orgInstallationToken(env, stats);
    const repos = await listRepos(stats, token);
    for (const repo of repos) {
      stats.repos += 1;
      try {
        const data = await gh(stats, token, `/repos/${repo.full_name}/actions/workflows?per_page=100`);
        for (const wf of data.workflows || []) {
          env.INVENTORY.writeDataPoint({
            indexes: [repo.name],
            blobs: [repo.name, wf.name || wf.path, wf.state || "", wf.path || ""],
            doubles: [1, wf.id], // present, workflow_id
          });
          stats.runs_written += 1; // rows written this beat
        }
      } catch (err) {
        stats.errors += 1;
        console.warn(`warning: ${repo.full_name}: ${err}`);
      }
    }
    writePollRow(env, "inventory", "ok", stats, t0);
    console.log(`inventory ok: ${JSON.stringify(stats)}`);
  } catch (err) {
    stats.errors += 1;
    writePollRow(env, "inventory", "error", stats, t0);
    console.error(`inventory failed: ${err}`);
    throw err;
  }
}

// ── The beats ────────────────────────────────────────────────────────────────

/** What the vitals beat needs from this module: the shared KV state doc, the
 *  shared Discord egress, and the shared heartbeat row. Injected rather than
 *  imported so vitals.js has no dependency on the GitHub plumbing (and so the
 *  whole beat is drivable from `node --test`). */
const vitalsDeps = { loadState, sendAlert, heartbeat: writePollRow };

export default {
  async scheduled(controller, env, ctx) {
    const beat =
      controller.cron === INVENTORY_CRON ? inventory(env)
      : controller.cron === VITALS_CRON ? runVitals(env, vitalsDeps)
      : poll(env); // POLL_CRON, and the safe default for an unrecognised trigger
    ctx.waitUntil(beat);
  },

  // No inbound surface — the Worker is cron-driven. (Local test:
  // `wrangler dev --test-scheduled`, then GET /__scheduled.)
  async fetch() {
    return new Response(
      `cicd-collector: cron-driven (${POLL_CRON} poll+alerts, ${VITALS_CRON} host vitals, ` +
      `${INVENTORY_CRON} inventory); see workers/cicd-collector/README.md\n`,
    );
  },
};
