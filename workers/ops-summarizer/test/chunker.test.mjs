// The chunker's output rides in Vectorize METADATA, which is capped at ~10 KiB per
// vector. Exceeding it does not degrade gracefully: the whole upsert batch is
// rejected with VECTOR_UPSERT_ERROR 40023 and every note in that batch of 100 goes
// unwritten. So this is not a formatting preference — an over-budget chunk takes 99
// innocent notes down with it.
//
// That is exactly what happened: the home vault grew a 15,886-char note
// (Pins/Pins Data.md) and the daily mirror failed on `home` every run, alarming in
// #ops, while the other four vaults mirrored fine.

import { test } from "node:test";
import assert from "node:assert/strict";
import { chunk, MAX_CHARS, MIN_CHARS } from "../scripts/vault-chunks.mjs";

// The provable ceiling. A paragraph under MIN_CHARS does not trigger a flush, so a
// short paragraph can absorb a following near-max one before the budget check bites.
// Bounded, small, and far below the metadata cap — worth stating exactly rather than
// pretending the budget is a hard limit.
const CEILING = MAX_CHARS + MIN_CHARS + 2;

const para = (n, ch = "a") => ch.repeat(n);
const words = (n) => Array.from({ length: n }, (_, i) => `word${i}`).join(" ");

test("a single over-budget paragraph is split, not passed through whole", () => {
  // Before the fix this returned one 8000-char chunk, because splitting only ever
  // happened BETWEEN paragraphs.
  const out = chunk(words(2000));
  assert.ok(out.length > 1, "should split");
  for (const c of out) assert.ok(c.length <= CEILING, `chunk of ${c.length} exceeds ${CEILING}`);
});

test("the pathological shape splits too: no newlines, no spaces", () => {
  // A pasted blob or a data table with no break to prefer. There is no good
  // boundary, so a hard cut is correct — losing the note is not.
  const out = chunk(para(16000));
  assert.ok(out.length >= 13, `expected many pieces, got ${out.length}`);
  for (const c of out) assert.ok(c.length <= CEILING, `chunk of ${c.length} exceeds ${CEILING}`);
});

test("splitting prefers a line break over a mid-word cut", () => {
  const line = para(300) + "\n";
  const out = chunk(line.repeat(10));
  // Every piece should end at a line boundary, i.e. contain no partial 300-run.
  for (const c of out) assert.ok(c.length <= CEILING);
  assert.ok(out.length > 1);
});

test("ordinary prose chunking is unchanged", () => {
  // The fix must not re-chunk normal notes: every id is path+index, so a changed
  // count rewrites ids and churns the index for no reason.
  const text = [para(300), para(300), para(300), para(300), para(300)].join("\n\n");
  const out = chunk(text);
  // 300+300+300 = 904 (with the two "\n\n" joins), then the 4th would reach 1206 and
  // trips the budget, so it flushes and 4+5 become 602. Identical before and after the
  // fix — the new branch only fires for a paragraph over MAX_CHARS on its own.
  assert.deepEqual(out.map((c) => c.length), [904, 602]);
});

test("a short tail below MIN_CHARS is still dropped", () => {
  assert.deepEqual(chunk(para(20)), []);
});

test("no chunk from a realistic mixed note comes near the metadata cap", () => {
  const note = [para(200), words(3000), para(150), para(9000, "x")].join("\n\n");
  for (const c of chunk(note)) {
    assert.ok(c.length <= CEILING, `chunk of ${c.length} exceeds ${CEILING}`);
    assert.ok(c.length * 4 < 10 * 1024, "must stay clear of the ~10 KiB metadata cap even as UTF-8");
  }
});

// ── the invisible-note invariant ────────────────────────────────────────────

test("a note shorter than MIN_CHARS still produces one chunk", async () => {
  // Measured before this fix: 187 of 1,913 notes across the five vaults produced
  // nothing and were absent from the index — 30% of `gear`, 66% of `travel`.
  // Silently, because nothing counts notes that produced nothing.
  const { chunksForVault } = await import("../scripts/vault-chunks.mjs");
  const { mkdtemp, writeFile } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");

  const dir = await mkdtemp(join(tmpdir(), "shortvault-"));
  await writeFile(join(dir, "stub.md"), "---\ntags: [x]\n---\n\nSee [[Other Note]].\n");
  const out = await chunksForVault(dir, "v");
  assert.equal(out.length, 1, "a stub note must be indexed, not dropped");
  assert.match(out[0].text, /See \[\[Other Note\]\]/);
});

test("a note that is only front matter is still skipped", async () => {
  // The floor governs splitting, not inclusion — but an empty note has nothing to
  // retrieve, and indexing it would put a titleless blank in every result set.
  const { chunksForVault } = await import("../scripts/vault-chunks.mjs");
  const { mkdtemp, writeFile } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");

  const dir = await mkdtemp(join(tmpdir(), "emptyvault-"));
  await writeFile(join(dir, "empty.md"), "---\ntags: [x]\n---\n\n\n");
  assert.deepEqual(await chunksForVault(dir, "v"), []);
});

test("chunk() itself still drops sub-MIN_CHARS fragments", () => {
  // The fix belongs at the note level. If chunk() started emitting fragments, long
  // notes would gain a trailing scrap chunk each — noise in every result set.
  assert.deepEqual(chunk(para(20)), []);
});
