// Push the dev vault into Vectorize via the Worker's /ingest route.
//
// The Worker does the embedding and the upsert, so every Cloudflare credential
// stays a binding and this script needs only the handler bearer. That is the
// reason for the round trip: a local script with an API token would be faster and
// would put a second copy of those credentials on disk.
//
// Usage:
//   HANDLER_TOKEN=… node scripts/ingest-vault.mjs [vaultDir] [workerUrl]
import { chunksForVault, vaultNameFor } from "./vault-chunks.mjs";

const VAULT = process.argv[2] || `${process.env.HOME}/obsidian/dev`;
// The vault name tags every chunk so one index can serve all five and a caller can
// narrow to one. Derived from the directory name rather than passed separately, so
// it cannot drift from what was actually read.
const VAULT_NAME = process.env.VAULT_NAME || vaultNameFor(VAULT);
const URL_ = process.argv[3] || "https://ops-summarizer.tommy-b-doerr.workers.dev";
const TOKEN = process.env.HANDLER_TOKEN;
if (!TOKEN) {
  console.error("HANDLER_TOKEN is required");
  process.exit(2);
}

// Chunking and id derivation live in vault-chunks.mjs, shared with the reconcile —
// see that file for why one definition matters here.
const chunks = await chunksForVault(VAULT, VAULT_NAME);

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
