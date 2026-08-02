// Push the dev vault into Vectorize via the Worker's /ingest route.
//
// The Worker does the embedding and the upsert, so every Cloudflare credential
// stays a binding and this script needs only the handler bearer. That is the
// reason for the round trip: a local script with an API token would be faster and
// would put a second copy of those credentials on disk.
//
// Usage:
//   HANDLER_TOKEN=… node scripts/ingest-vault.mjs [vaultDir] [workerUrl]
import { readFile, readdir } from "node:fs/promises";
import { join, relative, extname } from "node:path";
import { createHash } from "node:crypto";

// Vectorize caps ids at 64 bytes and vault paths run longer than that
// ("Wiki/Infrastructure/…"), so the id is a hash of the path plus the chunk
// index. Stable across runs, which is what makes re-ingest an upsert rather than
// a duplicate. The readable path stays in metadata.
const idFor = (rel, i) => `${createHash("sha1").update(rel).digest("hex").slice(0, 24)}#${i}`;

const VAULT = process.argv[2] || `${process.env.HOME}/obsidian/dev`;
// The vault name tags every chunk so one index can serve all five and a caller can
// narrow to one. Derived from the directory name rather than passed separately, so
// it cannot drift from what was actually read.
const VAULT_NAME = process.env.VAULT_NAME || VAULT.replace(/\/+$/, "").split("/").pop();
const URL_ = process.argv[3] || "https://ops-summarizer.tommy-b-doerr.workers.dev";
const TOKEN = process.env.HANDLER_TOKEN;
if (!TOKEN) {
  console.error("HANDLER_TOKEN is required");
  process.exit(2);
}

// Chunking is paragraph-aware with a character budget rather than a fixed window:
// vault notes are prose with headings, and splitting mid-sentence produces matches
// that read as noise when they land in a prompt.
const MAX_CHARS = 1200;
const MIN_CHARS = 80;

function chunk(text) {
  const out = [];
  let buf = "";
  for (const para of text.split(/\n{2,}/)) {
    const p = para.trim();
    if (!p) continue;
    if (buf.length + p.length + 2 > MAX_CHARS && buf.length >= MIN_CHARS) {
      out.push(buf.trim());
      buf = "";
    }
    buf += (buf ? "\n\n" : "") + p;
  }
  if (buf.trim().length >= MIN_CHARS) out.push(buf.trim());
  return out;
}

async function* walk(dir) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    // Skip Obsidian's own state and any dotdir — indexing those buries real notes.
    if (e.name.startsWith(".")) continue;
    const full = join(dir, e.name);
    if (e.isDirectory()) yield* walk(full);
    else if (extname(e.name) === ".md") yield full;
  }
}

const chunks = [];
for await (const file of walk(VAULT)) {
  const rel = relative(VAULT, file);
  const raw = await readFile(file, "utf8");
  // Strip YAML front matter — it is metadata, and embedding it makes every note
  // look similar to every other note.
  const body = raw.replace(/^---\n[\s\S]*?\n---\n/, "");
  const title = rel.replace(/\.md$/, "");
  chunk(body).forEach((text, i) => {
    chunks.push({ id: idFor(`${VAULT_NAME}/${rel}`, i), vault: VAULT_NAME, path: rel, title, text });
  });
}

console.log(`${chunks.length} chunks from ${VAULT} (vault=${VAULT_NAME})`);

// Batched over the wire as well as inside the Worker: a single request carrying
// every chunk would exceed the Worker request limit and time out the embedder.
const WIRE_BATCH = 100;
let done = 0;
for (let i = 0; i < chunks.length; i += WIRE_BATCH) {
  const batch = chunks.slice(i, i + WIRE_BATCH);
  // Retry transients rather than abandoning the run. A freshly rotated
  // HANDLER_TOKEN propagates across edge instances eventually, not atomically, so
  // a batch can hit one that still has the old value and 401 — which is not an
  // auth failure in any useful sense, and dying on it leaves a half-ingested
  // vault that looks complete until a query comes back thin.
  let res;
  for (let attempt = 1; attempt <= 6; attempt++) {
    res = await fetch(`${URL_}/ingest`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify({ chunks: batch }),
    });
    if (res.ok) break;
    if (![401, 429, 500, 502, 503, 504].includes(res.status)) break;
    await new Promise((r) => setTimeout(r, 2000 * attempt));
  }
  if (!res.ok) {
    console.error(`\nbatch at ${i} failed after retries: ${res.status} ${await res.text()}`);
    process.exit(1);
  }
  done += (await res.json()).upserted ?? batch.length;
  process.stdout.write(`\r  upserted ${done}/${chunks.length}`);
}
console.log("\ndone");
