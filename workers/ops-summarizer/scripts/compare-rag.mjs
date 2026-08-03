// Compare the two retrieval stores on the same queries.
//
// They are NOT expected to agree exactly, and a test that demanded they did would
// be wrong. Different embedding models (nomic-embed-text-v1.5 on the mini,
// bge-base-en-v1.5 in Workers AI) and different chunkers produce different
// passages and incomparable score scales. Comparing raw scores across the two is
// meaningless.
//
// What IS comparable, and what this measures, is whether they surface the same
// SOURCE NOTES — because that is what a caller actually consumes. Overlap is
// computed on note paths, not chunks and not scores.
//
// Usage:
//   HANDLER_TOKEN=… node scripts/compare-rag.mjs [--json]
// Requires the mini reachable for the vecserve half; the Vectorize half needs
// only the Worker.
const WORKER = process.env.WORKER_URL || "https://ops-summarizer.tommy-b-doerr.workers.dev";
const VECSERVE = process.env.VECSERVE_URL || "http://127.0.0.1:8899";
const TOKEN = process.env.HANDLER_TOKEN;
const K = 5;

// Deliberately spread across vaults, and phrased as a person would ask rather
// than as keywords — keyword queries flatter lexical search and tell you nothing
// about semantic retrieval.
export const QUERIES = [
  { vault: "dev", q: "why does /Volumes/dev stop answering readdir and how do I recover it" },
  { vault: "dev", q: "how does the fleet message bus deliver telemetry" },
  { vault: "dev", q: "what replaced the TIG observability stack" },
  { vault: "camping", q: "which campsites have good shade in summer" },
  { vault: "camping", q: "what do I need to check before a trip" },
  { vault: "gear", q: "which sleeping bag is rated for cold weather" },
  { vault: "home", q: "how is the home network set up" },
  { vault: "travel", q: "what trips are being planned" },
];

// Join on `title`, which both stores set to the vault-relative path minus `.md`.
// vecserve has no `path` field at all, and chunk ids are not comparable across
// two different chunkers — the note is the only shared unit of meaning.
const notePaths = (rows) => new Set(rows.map((r) => r.title).filter(Boolean));

async function queryVectorize(q, vault) {
  const u = new URL(`${WORKER}/search`);
  u.searchParams.set("q", q);
  u.searchParams.set("k", String(K));
  u.searchParams.set("vault", vault);
  const res = await fetch(u, { headers: { authorization: `Bearer ${TOKEN}` } });
  if (!res.ok) throw new Error(`vectorize ${res.status}`);
  return (await res.json()).matches || [];
}

async function queryVecserve(q, vault) {
  const res = await fetch(`${VECSERVE}/search`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ vault, query: q, k: K }),
  });
  if (!res.ok) throw new Error(`vecserve ${res.status}`);
  const body = await res.json();
  const rows = body.results || body.matches || [];
  return rows.map((r) => ({ title: r.title || "", score: r.score }));
}

export function overlap(a, b) {
  const A = notePaths(a);
  const B = notePaths(b);
  if (!A.size && !B.size) return { jaccard: 1, shared: 0, onlyA: 0, onlyB: 0 };
  const shared = [...A].filter((x) => B.has(x)).length;
  const union = new Set([...A, ...B]).size;
  return { jaccard: union ? shared / union : 0, shared, onlyA: A.size - shared, onlyB: B.size - shared };
}

async function main() {
  const rows = [];
  for (const { vault, q } of QUERIES) {
    let vec = [];
    let old = [];
    let err = null;
    try {
      vec = await queryVectorize(q, vault);
    } catch (e) {
      err = `vectorize: ${e.message}`;
    }
    try {
      old = await queryVecserve(q, vault);
    } catch (e) {
      err = (err ? err + "; " : "") + `vecserve: ${e.message}`;
    }
    rows.push({ vault, q, err, vectorize: vec.length, vecserve: old.length, ...overlap(vec, old),
      topVectorize: vec[0]?.title || null, topVecserve: old[0]?.title || null });
  }

  if (process.argv.includes("--json")) {
    console.log(JSON.stringify(rows, null, 2));
    return;
  }

  console.log(`${"vault".padEnd(8)} ${"hits(v2/v1)".padEnd(12)} ${"shared".padEnd(7)} query`);
  for (const r of rows) {
    const hits = `${r.vectorize}/${r.vecserve}`.padEnd(12);
    console.log(`${r.vault.padEnd(8)} ${hits} ${String(r.shared).padEnd(7)} ${r.q.slice(0, 52)}`);
    if (r.err) console.log(`         ! ${r.err}`);
    if (r.topVectorize || r.topVecserve) {
      console.log(`         vectorize: ${r.topVectorize || "-"}`);
      console.log(`         vecserve : ${r.topVecserve || "-"}`);
    }
  }
  const answered = rows.filter((r) => r.vectorize > 0).length;
  console.log(`\nvectorize answered ${answered}/${rows.length} queries`);
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
