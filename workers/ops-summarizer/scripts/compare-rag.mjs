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

/** Wall time around a call, so the comparison carries latency as well as overlap. */
async function timed(fn) {
  const t0 = performance.now();
  try {
    return { rows: await fn(), ms: performance.now() - t0, err: null };
  } catch (e) {
    return { rows: [], ms: performance.now() - t0, err: e.message };
  }
}

const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
};

/**
 * The verdict, and what it deliberately does NOT gate on.
 *
 * Overlap is REPORTED, never enforced. The two stores use different embedding models
 * over different chunkings, so they legitimately disagree — an overlap threshold would
 * fail forever and teach us to ignore the check, which is the failure mode this repo
 * already has an issue open about (obsidian-automations#336).
 *
 * What IS a regression is a store that stops answering. A query that returned notes
 * last week and returns nothing today means an index went stale, an ingest broke, or a
 * backend is down — all real, all actionable, none of them a matter of taste.
 */
export function verdict(rows) {
  const silent = rows.filter((r) => !r.err && (r.vectorize === 0 || r.vecserve === 0));
  const errored = rows.filter((r) => r.err);
  const overlaps = rows.filter((r) => !r.err).map((r) => r.jaccard);
  return {
    queries: rows.length,
    errored: errored.length,
    silent: silent.length,
    meanOverlap: overlaps.length ? overlaps.reduce((a, b) => a + b, 0) / overlaps.length : null,
    vectorizeMs: median(rows.map((r) => r.vectorizeMs).filter((x) => x != null)),
    vecserveMs: median(rows.map((r) => r.vecserveMs).filter((x) => x != null)),
    ok: errored.length === 0 && silent.length === 0,
  };
}

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
    const v = await timed(() => queryVectorize(q, vault));
    const l = await timed(() => queryVecserve(q, vault));
    const err = [v.err && `vectorize: ${v.err}`, l.err && `vecserve: ${l.err}`].filter(Boolean).join("; ") || null;
    rows.push({ vault, q, err, vectorize: v.rows.length, vecserve: l.rows.length,
      vectorizeMs: Math.round(v.ms), vecserveMs: Math.round(l.ms), ...overlap(v.rows, l.rows),
      topVectorize: v.rows[0]?.title || null, topVecserve: l.rows[0]?.title || null });
  }

  const sum = verdict(rows);

  if (process.argv.includes("--json")) {
    console.log(JSON.stringify({ summary: sum, rows }, null, 2));
    if (!sum.ok) process.exitCode = 1;
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
  console.log(
    `\n${sum.queries} queries · mean overlap ${(sum.meanOverlap ?? 0).toFixed(2)} · ` +
      `median latency vectorize ${sum.vectorizeMs}ms / vecserve ${sum.vecserveMs}ms`,
  );
  if (!sum.ok) {
    console.log(`REGRESSION: ${sum.errored} errored, ${sum.silent} answered by only one store`);
    process.exitCode = 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
