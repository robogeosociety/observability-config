// The reconcile decides what to DELETE from the retrieval store, so both failure
// directions are expensive: deleting too much silently removes knowledge the RAG
// then cannot cite, and deleting too little leaves deleted notes answering
// questions as though they were current.
//
// The guards matter more than the diff. The mini's external disk has wedged
// repeatedly (readdir returning EINTR for minutes), and a failed vault read looks
// exactly like "every note in this vault was deleted" unless something refuses.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { orphans, refuseReason, expectedIds } from "../scripts/reconcile-vault.mjs";
import { idFor, chunksForVault } from "../scripts/vault-chunks.mjs";

const note = (title, paras) => `---\ntags: [x]\n---\n\n${paras.join("\n\n")}\n`;
const para = (n, ch = "a") => ch.repeat(n);

async function vault(files) {
  const dir = await mkdtemp(join(tmpdir(), "vault-"));
  for (const [rel, body] of Object.entries(files)) {
    const full = join(dir, rel);
    await mkdir(join(full, ".."), { recursive: true });
    await writeFile(full, body);
  }
  return dir;
}

// ── the diff ────────────────────────────────────────────────────────────────

test("an id the vaults no longer produce is an orphan", () => {
  const present = new Set(["a#0", "a#1", "b#0"]);
  const wanted = new Set(["a#0", "a#1"]);
  assert.deepEqual(orphans(present, wanted), ["b#0"]);
});

test("a vault that only grew deletes nothing", () => {
  const present = new Set(["a#0"]);
  const wanted = new Set(["a#0", "a#1", "b#0"]);
  assert.deepEqual(orphans(present, wanted), []);
});

test("a shrunk note orphans exactly its trailing chunks", () => {
  // The common case: an edit removes paragraphs, so the note still exists but
  // produces fewer chunks. Nothing else about it should be touched.
  const present = new Set(["n#0", "n#1", "n#2", "n#3"]);
  const wanted = new Set(["n#0", "n#1"]);
  assert.deepEqual(orphans(present, wanted), ["n#2", "n#3"]);
});

// ── the blast-radius guard ──────────────────────────────────────────────────

test("a normal day of edits passes the guard", () => {
  assert.equal(refuseReason(40, 4587, 0.10), null);
});

test("a wholesale deletion is refused, not executed", () => {
  // 4587/4587 is what an unreadable vault set produces. Refusing is the only
  // safe answer: the cost of a wrong refusal is a stale index for one day.
  const reason = refuseReason(4587, 4587, 0.10);
  assert.match(reason, /100\.0%/);
  assert.match(reason, /bad read/);
});

test("the ceiling is adjustable for a genuinely large deletion", () => {
  assert.equal(refuseReason(1000, 4587, 0.50), null);
});

test("nothing to delete is never a refusal", () => {
  assert.equal(refuseReason(0, 0, 0.10), null);
});

test("an index reporting zero vectors refuses rather than dividing by it", () => {
  assert.match(refuseReason(5, 0, 0.10), /zero vectors/);
});

// ── the read guards, against real directories ───────────────────────────────

test("a missing vault directory refuses instead of orphaning everything", async () => {
  await assert.rejects(
    () => expectedIds(["/nonexistent/vault/path"]),
    /refusing to reconcile against a partial read/,
  );
});

test("a vault that reads as empty refuses", async () => {
  // An empty read and a deleted vault are indistinguishable from here, and the
  // disk that holds these vaults is documented as stalling. Refuse.
  const dir = await vault({ "README.txt": "not markdown" });
  await assert.rejects(() => expectedIds([dir]), /produced no chunks/);
});

test("expected ids match what the ingest would write, for the same content", async () => {
  // The reconcile deletes everything not in this set, so if it computed ids even
  // slightly differently from the ingest, it would delete live vectors and the
  // next mirror would write them back — forever.
  const dir = await vault({ "Note.md": note("Note", [para(200), para(200)]) });
  const name = dir.split("/").pop();
  const { ids } = await expectedIds([dir]);
  const chunks = await chunksForVault(dir);
  assert.equal(ids.size, chunks.length);
  for (const c of chunks) assert.ok(ids.has(c.id), `${c.id} missing`);
  assert.ok(ids.has(idFor(`${name}/Note.md`, 0)));
});

test("front matter is stripped before chunking, as the ingest does", async () => {
  // Front matter is metadata; embedding it makes every note resemble every other
  // note. If the reconcile disagreed with the ingest about this, chunk COUNTS
  // would differ and the tail would be deleted every run.
  const withFm = await vault({ "a.md": `---\ntags: [t]\n---\n\n${para(300)}\n` });
  const withoutFm = await vault({ "a.md": `${para(300)}\n` });
  const a = await chunksForVault(withFm, "v");
  const b = await chunksForVault(withoutFm, "v");
  assert.deepEqual(a.map((c) => c.text), b.map((c) => c.text));
});

test("dotdirs are skipped, so .obsidian state never becomes an expected id", async () => {
  const dir = await vault({
    "real.md": para(300),
    ".obsidian/workspace.md": para(300),
  });
  const chunks = await chunksForVault(dir, "v");
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].path, "real.md");
});
