// rag — dev-vault retrieval over Vectorize.
//
// The vault index lives on the mini (vecserve, nomic-embed-text-v1.5, 1922 dev
// chunks) and is reachable only on localhost there. Calling back through the
// tunnel would re-introduce a dependency on the mini — the host whose disk
// wedges — into a lane that exists precisely to survive that. So the dev vault is
// mirrored into Vectorize and embedded with Workers AI, and this lane never
// touches the mini.
//
// That means a SECOND embedding of the same corpus, with a different model. The
// two are not interchangeable and must not be mixed: vectors embedded with nomic
// cannot be searched with bge. Keeping them separate is the point — vecserve
// stays the mini's local tool, this is the cloud lane's copy.

// 768 dimensions, matching the index created with --dimensions=768.
export const EMBED_MODEL = "@cf/baai/bge-base-en-v1.5";

const MAX_CONTEXT_CHARS = 6000;

/** Embed a batch of strings. Workers AI returns one vector per input. */
export async function embed(env, texts) {
  const out = await env.AI.run(EMBED_MODEL, { text: texts });
  return out.data;
}

/**
 * Retrieve vault passages relevant to a query.
 *
 * Returns [] rather than throwing when retrieval fails. A summary without vault
 * context is degraded but useful; failing the whole job because the index was
 * unreachable would turn a retrieval blip into dead-lettered work.
 */
export async function retrieve(env, query, topK = 5, vault = null) {
  if (!env.VECTORIZE || !env.AI) return [];
  try {
    const [vector] = await embed(env, [query]);
    // Server-side metadata filter, which is why `vault` carries a metadata index.
    // Filtering after the fact would silently return fewer than topK results, and
    // a narrow vault would come back nearly empty for no visible reason.
    const opts = { topK, returnMetadata: "all" };
    if (vault) opts.filter = { vault };
    const res = await env.VECTORIZE.query(vector, opts);
    return (res.matches || []).map((m) => ({
      score: m.score,
      vault: m.metadata?.vault || "",
      path: m.metadata?.path || "",
      title: m.metadata?.title || "",
      text: m.metadata?.text || "",
    }));
  } catch {
    return [];
  }
}

/**
 * Format matches as prompt context, budgeted by characters.
 *
 * Truncating here rather than trusting topK matters: vault notes vary from a line
 * to several thousand words, so five matches is not a bounded amount of text.
 */
export function asContext(matches) {
  if (!matches.length) return "";
  const parts = [];
  let used = 0;
  for (const m of matches) {
    const block = `### ${m.vault ? m.vault + "/" : ""}${m.title || m.path}\n${m.text}`.trim();
    if (used + block.length > MAX_CONTEXT_CHARS) break;
    parts.push(block);
    used += block.length;
  }
  if (!parts.length) return "";
  return (
    "Relevant notes from the dev vault (the operator's own documentation — " +
    "prefer these over general knowledge when they conflict):\n\n" +
    parts.join("\n\n")
  );
}

/**
 * Upsert vault chunks. Called by the admin /ingest route.
 * Batched because Workers AI and Vectorize both cap per-call sizes, and because a
 * failed 500-chunk call is far more expensive to diagnose than a failed 50.
 */
export async function ingest(env, chunks, batchSize = 50) {
  let upserted = 0;
  for (let i = 0; i < chunks.length; i += batchSize) {
    const batch = chunks.slice(i, i + batchSize);
    const vectors = await embed(
      env,
      batch.map((c) => c.text),
    );
    await env.VECTORIZE.upsert(
      batch.map((c, j) => ({
        id: c.id,
        values: vectors[j],
        metadata: { vault: c.vault, path: c.path, title: c.title, text: c.text.slice(0, 2000) },
      })),
    );
    upserted += batch.length;
  }
  return upserted;
}
