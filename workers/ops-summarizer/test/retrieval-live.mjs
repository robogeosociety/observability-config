// Live retrieval regression, run against the deployed Worker.
//
// NOT in the `*.test.mjs` glob on purpose: this needs the network and a token, so
// CI's unit lane must not pick it up. Same split as cicd-collector's dryrun.mjs.
//
//   HANDLER_TOKEN=… node test/retrieval-live.mjs
//
// WHAT THIS DOES NOT ASSERT: that Vectorize agrees with vecserve. It cannot and
// should not. The two use different embedding models (nomic-embed-text-v1.5 vs
// bge-base-en-v1.5) over different chunkings, so their scores are on
// incomparable scales and their top-1 legitimately differs. A test demanding
// parity would fail forever and teach us to ignore it.
//
// Measured 2026-08-02 across 8 queries, overlap of source notes in top-5:
//
//   dev     "readdir / recover"        4/5 shared
//   dev     "message bus telemetry"    1/5
//   dev     "what replaced TIG"        0/5
//   camping "shade in summer"          0/5
//   camping "check before a trip"      0/5
//   gear    "cold weather sleeping bag" 4/5
//   home    "home network setup"       3/5
//   travel  "trips being planned"      2/5
//
// Both stores answered every query. So this is not a coverage regression — it is
// a genuinely different retriever with comparable reach and different ranking.
//
// WHAT IT DOES ASSERT is what survives vecserve's removal: every vault is
// populated and queryable, the vault filter actually filters, and a handful of
// anchors whose correct answer is known independently of either store still come
// back. Those are the properties a cutover could actually break.
import assert from "node:assert/strict";

const WORKER = process.env.WORKER_URL || "https://ops-summarizer.tommy-b-doerr.workers.dev";
const TOKEN = process.env.HANDLER_TOKEN;
if (!TOKEN) {
  console.error("HANDLER_TOKEN is required");
  process.exit(2);
}

const VAULTS = ["dev", "camping", "gear", "home", "travel"];

// Anchors: query → a note that MUST appear in the top 5. Chosen because the right
// answer is obvious to a human reading the vault, not because a store returned it.
const ANCHORS = [
  {
    vault: "dev",
    q: "why does /Volumes/dev stop answering readdir and how do I recover it",
    expect: "Wiki/Infrastructure/Volume Wedges",
  },
  {
    vault: "dev",
    q: "OrbStack virtiofs shares the whole macOS root",
    expect: "Wiki/Infrastructure/Volume Wedges",
  },
];

async function search(q, vault, k = 5) {
  const u = new URL(`${WORKER}/search`);
  u.searchParams.set("q", q);
  u.searchParams.set("k", String(k));
  if (vault) u.searchParams.set("vault", vault);
  const res = await fetch(u, { headers: { authorization: `Bearer ${TOKEN}` } });
  assert.equal(res.ok, true, `search failed: ${res.status}`);
  return (await res.json()).matches || [];
}

let failures = 0;
const check = async (name, fn) => {
  try {
    await fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failures += 1;
    console.log(`  FAIL  ${name}\n        ${e.message}`);
  }
};

console.log("live retrieval regression\n");

for (const vault of VAULTS) {
  await check(`${vault}: populated and queryable`, async () => {
    // A broad query every vault should answer somehow. An empty result here means
    // the vault was never ingested, or its chunks lost their `vault` tag — the
    // exact silent failure a cutover would ship.
    const m = await search("notes", vault, 3);
    assert.ok(m.length > 0, `no results for vault ${vault}`);
  });

  await check(`${vault}: filter returns only that vault`, async () => {
    const m = await search("the", vault, 5);
    const strays = m.filter((r) => r.vault && r.vault !== vault).map((r) => r.vault);
    assert.deepEqual(strays, [], `leaked from ${[...new Set(strays)].join(",")}`);
  });
}

for (const a of ANCHORS) {
  await check(`anchor: ${a.q.slice(0, 44)}…`, async () => {
    const titles = (await search(a.q, a.vault, 5)).map((r) => r.title);
    assert.ok(
      titles.includes(a.expect),
      `expected "${a.expect}" in top-5, got:\n          ${titles.join("\n          ")}`,
    );
  });
}

await check("unfiltered search spans more than one vault", async () => {
  // Proves the default really is all-vaults; if the filter were stuck on, this is
  // where it would show.
  const m = await search("notes about planning", null, 20);
  const vaults = new Set(m.map((r) => r.vault).filter(Boolean));
  assert.ok(vaults.size > 1, `only saw: ${[...vaults].join(",") || "none"}`);
});

await check("an unknown vault yields nothing rather than erroring", async () => {
  assert.deepEqual(await search("anything", "no-such-vault", 5), []);
});

console.log(`\n${failures ? `${failures} FAILED` : "all passed"}`);
process.exit(failures ? 1 : 0);
