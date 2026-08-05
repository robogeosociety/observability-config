// Delete vectors whose notes no longer exist — the other half of the mirror.
//
// The mirror upserts, so additions and edits land, but a note deleted from a vault
// keeps its vectors forever and stays retrievable. That is the documented gap in
// vault-vectorize-mirror.yml and the blocker on Vectorize becoming the only
// retrieval store: a RAG answer citing a note the operator deleted is worse than
// no answer, because it reads as current.
//
// WHY ENUMERATE RATHER THAN KEEP A MANIFEST
//
// The obvious design is to record what was written and diff against it next run.
// That makes the index's contents a claim about history rather than an
// observation, and a single lost manifest write hides orphans permanently with
// nothing to notice. `wrangler vectorize list-vectors` gives us the actual
// contents, so this is a true reconcile: it converges from any starting state,
// including one produced by a run that crashed halfway, and it has no state of its
// own to drift. Same reason the fleet trusts heartbeats over `launchctl list`.
//
// The Vectorize *binding* has no list method, which is why this is a script over
// the CLI and not a Worker route.
//
// Usage:
//   CLOUDFLARE_API_TOKEN=… node scripts/reconcile-vault.mjs            # report only
//   CLOUDFLARE_API_TOKEN=… node scripts/reconcile-vault.mjs --apply    # delete
//
//   --index NAME   default obsidian-vaults
//   --vaults DIR,… default ~/obsidian/{dev,camping,gear,home,travel}
//
// NOTE: --vaults must list EVERY vault in the index. Ids are content hashes and
// carry no vault tag, so there is no way to scope a reconcile to one vault: any
// vault you leave out looks entirely deleted. Narrowing it is a 94%-orphan run
// that the blast-radius guard refuses — which is the guard doing its job, not a
// bug to work around with --max-delete-fraction.
//   --max-delete-fraction F  refuse above this share of the index (default 0.10)
//
//   WRANGLER_CMD  how to invoke wrangler (default "wrangler"; CI uses "npx --yes wrangler@4")

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { stat } from "node:fs/promises";
import { chunksForVault } from "./vault-chunks.mjs";

const run = promisify(execFile);

// How to invoke wrangler. The mini's Actions runner has node but no wrangler, and
// installing one there would put a version of this lane's tooling in host state —
// the failure mode the fleet keeps rediscovering. The workflow passes
// WRANGLER_CMD="npx --yes wrangler@4", which needs nothing installed and pins the
// version in the workflow file where it is reviewable.
const WRANGLER = (process.env.WRANGLER_CMD || "wrangler").trim().split(/\s+/);

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(name);
  return i === -1 ? fallback : args[i + 1];
};
const APPLY = args.includes("--apply");
const INDEX = flag("--index", "obsidian-vaults");
const MAX_DELETE_FRACTION = Number(flag("--max-delete-fraction", "0.10"));
const VAULTS = flag("--vaults", ["dev", "camping", "gear", "home", "travel"]
  .map((v) => `${process.env.HOME}/obsidian/${v}`).join(","))
  .split(",").filter(Boolean);

const DELETE_BATCH = 500;

async function wrangler(subcommand) {
  const [bin, ...prefix] = WRANGLER;
  const { stdout } = await run(bin, [...prefix, ...subcommand], { maxBuffer: 32 * 1024 * 1024 });
  return stdout;
}

/** Every id currently in the index, paged. */
async function idsInIndex(index) {
  const ids = new Set();
  let cursor;
  let total = null;
  for (;;) {
    const cmd = ["vectorize", "list-vectors", index, "--count", "1000", "--json"];
    if (cursor) cmd.push("--cursor", cursor);
    const page = JSON.parse(await wrangler(cmd));
    total ??= page.totalCount;
    for (const v of page.vectors ?? []) ids.add(v.id);
    if (!page.isTruncated || !page.nextCursor) break;
    cursor = page.nextCursor;
  }
  return { ids, reportedTotal: total };
}

/**
 * Which ids should exist, given what the vaults hold right now.
 *
 * Throws if a vault is missing or reads as empty. That is the important guard:
 * this script deletes everything the vaults do not claim, so an unreadable vault
 * — the mini's external disk stalling is a documented, recurring event — would
 * otherwise present as "every note in that vault was deleted" and wipe its
 * vectors. Refusing to run beats a confident deletion built on a failed read.
 */
export async function expectedIds(vaultDirs) {
  const ids = new Set();
  const perVault = {};
  for (const dir of vaultDirs) {
    let isDir = false;
    try {
      isDir = (await stat(dir)).isDirectory();
    } catch {
      throw new Error(`vault directory missing: ${dir} — refusing to reconcile against a partial read`);
    }
    if (!isDir) throw new Error(`not a directory: ${dir}`);
    const chunks = await chunksForVault(dir);
    if (chunks.length === 0) {
      throw new Error(`vault produced no chunks: ${dir} — refusing (an empty read is indistinguishable from a deleted vault)`);
    }
    perVault[dir] = chunks.length;
    for (const c of chunks) ids.add(c.id);
  }
  return { ids, perVault };
}

/**
 * Ids present in the index that the vaults no longer produce.
 *
 * Pure, so the interesting cases are testable without a network: a shrunk note
 * (trailing chunk ids orphan), a deleted note (all of its ids orphan), and the
 * case that must NOT delete anything — a vault that simply grew.
 */
export function orphans(indexIds, wantedIds) {
  const out = [];
  for (const id of indexIds) if (!wantedIds.has(id)) out.push(id);
  return out.sort();
}

/** The blast-radius guard. Returns a refusal reason, or null when safe. */
export function refuseReason(orphanCount, indexSize, maxFraction) {
  if (orphanCount === 0) return null;
  if (indexSize === 0) return "the index reports zero vectors — nothing to reconcile against";
  const fraction = orphanCount / indexSize;
  if (fraction > maxFraction) {
    return `${orphanCount}/${indexSize} vectors (${(fraction * 100).toFixed(1)}%) look orphaned, over the ${(maxFraction * 100).toFixed(0)}% ceiling — `
      + "that is the shape of a bad read or a partial --vaults list, not a day of edits. "
      + "Raise --max-delete-fraction only once you have confirmed which notes actually went away.";
  }
  return null;
}

async function main() {
  // Authentication is wrangler's, not ours: CI exports CLOUDFLARE_API_TOKEN
  // (Vectorize Read + Write), a workstation is usually OAuth-logged-in already.
  // Asserting the env var here would make a perfectly authenticated dry run fail,
  // and wrangler's own error on a missing credential is clearer than ours.
  if (!process.env.CLOUDFLARE_API_TOKEN) {
    console.log("note: CLOUDFLARE_API_TOKEN unset — relying on wrangler's stored login");
  }

  const { ids: wanted, perVault } = await expectedIds(VAULTS);
  for (const [dir, n] of Object.entries(perVault)) console.log(`  ${n.toString().padStart(5)} chunks  ${dir}`);
  console.log(`${wanted.size} chunk ids expected across ${VAULTS.length} vaults`);

  const { ids: present, reportedTotal } = await idsInIndex(INDEX);
  console.log(`${present.size} ids present in ${INDEX} (index reports ${reportedTotal})`);

  const dead = orphans(present, wanted);
  const missing = [...wanted].filter((id) => !present.has(id)).length;
  console.log(`${dead.length} orphaned, ${missing} expected-but-absent (the mirror writes those; this script never does)`);

  if (dead.length === 0) {
    console.log("nothing to reconcile");
    return;
  }

  const refusal = refuseReason(dead.length, present.size, MAX_DELETE_FRACTION);
  if (refusal) {
    console.error(`REFUSING: ${refusal}`);
    process.exit(1);
  }

  if (!APPLY) {
    console.log(`dry run — would delete ${dead.length} vectors. First few: ${dead.slice(0, 5).join(", ")}`);
    console.log("re-run with --apply to delete");
    return;
  }

  for (let i = 0; i < dead.length; i += DELETE_BATCH) {
    const batch = dead.slice(i, i + DELETE_BATCH);
    await wrangler(["vectorize", "delete-vectors", INDEX, "--ids", ...batch]);
    console.log(`  deleted ${Math.min(i + batch.length, dead.length)}/${dead.length}`);
  }
  console.log("done");
}

// Importable for tests; only runs the reconcile when invoked directly.
if (import.meta.url === `file://${process.argv[1]}`) {
  await main();
}
