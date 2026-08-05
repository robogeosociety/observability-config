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

export function chunk(text) {
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
    chunk(body).forEach((text, i) => {
      chunks.push({ id: idFor(`${vaultName}/${rel}`, i), vault: vaultName, path: rel, title, text });
    });
  }
  return chunks;
}
