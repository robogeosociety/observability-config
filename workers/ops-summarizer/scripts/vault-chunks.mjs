// The one definition of "what chunks does this vault produce, and what are their ids".
//
// Extracted from ingest-vault.mjs so the reconcile can compute the SAME ids the
// ingest wrote. This is not tidiness: the reconcile deletes every id in the index
// that this module does not produce, so if the two ever disagreed about chunking,
// the reconcile would delete live vectors and the next ingest would write them
// back — a delete/rewrite cycle that costs embeddings daily and looks like
// nothing is wrong. One definition, imported twice, cannot drift.

import { readFile, readdir } from "node:fs/promises";
import { join, relative, extname } from "node:path";
import { createHash } from "node:crypto";

// Vectorize caps ids at 64 bytes and vault paths run longer than that
// ("Wiki/Infrastructure/…"), so the id is a hash of the path plus the chunk
// index. Stable across runs, which is what makes re-ingest an upsert rather than
// a duplicate. The readable path stays in metadata.
export const idFor = (rel, i) => `${createHash("sha1").update(rel).digest("hex").slice(0, 24)}#${i}`;

// Chunking is paragraph-aware with a character budget rather than a fixed window:
// vault notes are prose with headings, and splitting mid-sentence produces matches
// that read as noise when they land in a prompt.
export const MAX_CHARS = 1200;
export const MIN_CHARS = 80;

/**
 * Split one over-budget paragraph. Prefers a line break, then a space, and only
 * hard-cuts mid-word when a paragraph contains neither — a data table or a pasted
 * blob, where there is no good boundary to find.
 */
function splitLong(p) {
  const out = [];
  let rest = p;
  while (rest.length > MAX_CHARS) {
    let cut = rest.lastIndexOf("\n", MAX_CHARS);
    if (cut < MIN_CHARS) cut = rest.lastIndexOf(" ", MAX_CHARS);
    if (cut < MIN_CHARS) cut = MAX_CHARS;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out.filter((c) => c.length > 0);
}

export function chunk(text) {
  const out = [];
  let buf = "";
  const flush = () => {
    if (buf.trim().length >= MIN_CHARS) out.push(buf.trim());
    buf = "";
  };
  for (const para of text.split(/\n{2,}/)) {
    const p = para.trim();
    if (!p) continue;

    // A single paragraph can exceed the budget on its own — a data table, a pasted
    // blob, a long list with no blank lines. Splitting only BETWEEN paragraphs let
    // those through whole: the home vault grew a 15,886-char note and every upsert
    // batch containing it failed with VECTOR_UPSERT_ERROR 40023, because chunk text
    // rides in Vectorize metadata and metadata is capped at ~10 KiB per vector.
    // The mirror had been failing on `home` every run.
    if (p.length > MAX_CHARS) {
      flush();
      for (const piece of splitLong(p)) if (piece.length >= MIN_CHARS) out.push(piece);
      continue;
    }

    if (buf.length + p.length + 2 > MAX_CHARS && buf.length >= MIN_CHARS) flush();
    buf += (buf ? "\n\n" : "") + p;
  }
  flush();
  return out;
}

export async function* walk(dir) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    // Skip Obsidian's own state and any dotdir — indexing those buries real notes.
    if (e.name.startsWith(".")) continue;
    const full = join(dir, e.name);
    if (e.isDirectory()) yield* walk(full);
    else if (extname(e.name) === ".md") yield full;
  }
}

export function vaultNameFor(dir) {
  return dir.replace(/\/+$/, "").split("/").pop();
}

/** Every chunk one vault directory produces, in ingest order. */
export async function chunksForVault(vaultDir, vaultName = vaultNameFor(vaultDir)) {
  const chunks = [];
  for await (const file of walk(vaultDir)) {
    const rel = relative(vaultDir, file);
    const raw = await readFile(file, "utf8");
    // Strip YAML front matter — it is metadata, and embedding it makes every note
    // look similar to every other note.
    const body = raw.replace(/^---\n[\s\S]*?\n---\n/, "");
    const title = rel.replace(/\.md$/, "");

    let pieces = chunk(body);

    // INVARIANT: a note with content is never invisible.
    //
    // MIN_CHARS exists to stop a stray fragment becoming its own chunk when a note is
    // being SPLIT. Applied to a whole short note it does something else entirely: the
    // note produces no chunks at all, is never upserted, and cannot be retrieved by
    // any query. Measured 2026-08-05 across the five vaults: 187 of 1,913 notes —
    // 30% of `gear`, 66% of `travel` — were absent from the index for this reason,
    // silently, because nothing counts notes that produced nothing.
    //
    // Short notes are exactly the ones worth keeping: stubs, index notes, link hubs.
    // So the floor governs splitting, not inclusion — a note that survives front
    // matter with any content at all gets one chunk, however small.
    if (pieces.length === 0 && body.trim().length > 0) pieces = [body.trim()];

    pieces.forEach((text, i) => {
      chunks.push({ id: idFor(`${vaultName}/${rel}`, i), vault: vaultName, path: rel, title, text });
    });
  }
  return chunks;
}
