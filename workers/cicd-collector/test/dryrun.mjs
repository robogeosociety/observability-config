#!/usr/bin/env node
// Dry-run harness for the vitals beat — the thing that stands in for "verify
// against real data" until the Analytics Engine read token exists.
//
// With no credentials it prints the exact SQL the beat will send, so the queries
// can be reviewed (and pasted into any AE SQL client) without deploying anything.
// With credentials it runs them, prints the rows, and shows the verdict and the
// Discord message the beat WOULD post — it never posts, never touches KV, and
// never writes a data point.
//
//   node test/dryrun.mjs                      # print the SQL only
//   CF_ACCOUNT_ID=… CF_AE_READ_TOKEN=… \
//     node test/dryrun.mjs                    # …and run it, read-only
//
// Any VITALS_* threshold var is honoured, so a threshold can be trialled against
// live data before it is committed to wrangler.toml, e.g.
//   VITALS_DISK_ALERT_RATIO=0.5 node test/dryrun.mjs
//
// Read the token out of its store; never paste it on the command line:
//   CF_AE_READ_TOKEN="$(ssh tommydoerr@tommys-mac-mini.local \
//     'cd /Volumes/dev/cloudflare-tfvend && make -s output T=analytics_read')" …

import {
  aeQuery,
  config,
  evaluate,
  gate,
  message,
  sqlDisk,
  sqlFreshness,
  sqlMemory,
} from "../src/vitals.js";

const env = process.env;
const cfg = config(env);
const queries = {
  disk: sqlDisk(cfg.diskWindowMin),
  memory: sqlMemory(cfg.memWindowMin),
  freshness: sqlFreshness(cfg.freshLookbackMin),
};

console.log("── thresholds ───────────────────────────────────────────────");
console.log(JSON.stringify(cfg, null, 2));

for (const [name, sql] of Object.entries(queries)) {
  console.log(`\n── ${name} ${"─".repeat(60 - name.length)}\n`);
  console.log(sql);
}

if (!env.CF_AE_READ_TOKEN || !env.CF_ACCOUNT_ID) {
  console.log(
    "\n── not run ──────────────────────────────────────────────────\n" +
    "Set CF_ACCOUNT_ID and CF_AE_READ_TOKEN to execute these read-only.\n" +
    "The token is cloudflare-tfvend's `analytics_read` (Account Analytics Read).",
  );
  process.exit(0);
}

const rows = {};
for (const [name, sql] of Object.entries(queries)) {
  rows[name] = await aeQuery(env, sql);
  console.log(`\n── ${name}: ${rows[name].length} row(s) ──`);
  console.table(rows[name]);
}

const observations = evaluate(rows, cfg, Date.now() / 1000);
console.log("\n── verdict ──────────────────────────────────────────────────");
for (const o of observations) console.log(`  ${o.state.padEnd(8)} ${o.key}  ${o.note || o.text || o.clearText || ""}`);

// Gate against an empty state — i.e. "what would a first beat say".
const { alerts, clears } = gate(observations, {}, Date.now());
const content = message(alerts, clears);
console.log("\n── would post to #dev ───────────────────────────────────────");
console.log(content ?? "(nothing — all quiet)");
