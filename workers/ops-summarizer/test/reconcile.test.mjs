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
  // The id now carries a hash of the chunk's TEXT as well as its path, which is what
  // makes the ingest incremental — so it can only be reconstructed from the chunk.
  assert.ok(ids.has(idFor(`${name}/Note.md`, 0, chunks[0].text)));
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

// ── incremental ingest: the id is the change detector ───────────────────────

test("editing a note changes its chunk id, leaving the old one an orphan", async () => {
  // This is the whole mechanism. A changed chunk derives an id the index does not
  // have (so it gets embedded), and the id it used to have is no longer produced by
  // the vault (so the reconcile deletes it). No manifest, no metadata read-back.
  const { writeFile } = await import("node:fs/promises");
  const { join } = await import("node:path");

  const dir = await vault({ "n.md": para(300) });
  const before = (await chunksForVault(dir, "v")).map((c) => c.id);
  await writeFile(join(dir, "n.md"), para(300, "b"));
  const after = (await chunksForVault(dir, "v")).map((c) => c.id);

  assert.notDeepEqual(before, after, "an edit must produce different ids");
  assert.deepEqual(orphans(new Set(before), new Set(after)), before.sort());
});

test("an untouched note keeps its id, so the ingest can skip it", async () => {
  const dir = await vault({ "n.md": para(300) });
  const first = (await chunksForVault(dir, "v")).map((c) => c.id);
  const second = (await chunksForVault(dir, "v")).map((c) => c.id);
  assert.deepEqual(first, second);
  assert.deepEqual(orphans(new Set(first), new Set(second)), [], "nothing to delete");
});

test("two notes with identical text still get distinct ids", async () => {
  // The path is hashed too. Without that, one note's chunk would satisfy the other's
  // id and the second note would never be embedded at all.
  const dir = await vault({ "a.md": para(300), "b.md": para(300) });
  const ids = (await chunksForVault(dir, "v")).map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length, "ids must be unique per note");
});

test("ids stay inside Vectorize's 64-byte cap", async () => {
  const dir = await vault({ "deeply/nested/path/to/a/note/with/a/long/name.md": para(3000) });
  for (const c of await chunksForVault(dir, "some-vault-name")) {
    assert.ok(c.id.length <= 64, `id ${c.id} is ${c.id.length} bytes`);
  }
});

// ── enumeration must survive a cursor dying mid-walk ────────────────────────

test("a dead cursor restarts the walk instead of returning a partial set", async () => {
  // Observed on the id-scheme cutover: the corpus doubled while the reconcile was
  // paging it and the API returned "cursor appears to be corrupted [code: 40052]".
  // Returning what had been collected so far would be catastrophic — every id on the
  // unread pages would look like an orphan and be deleted.
  const { idsInIndex } = await import("../scripts/reconcile-vault.mjs");
  let calls = 0;
  const fetchPage = async (cursor) => {
    calls++;
    if (!cursor) return { totalCount: 4, isTruncated: true, nextCursor: "c1", vectors: [{ id: "a" }, { id: "b" }] };
    if (calls === 2) {
      const err = new Error("A request to the Cloudflare API failed.");
      err.stderr = "List vectors cursor appears to be corrupted [code: 40052]";
      throw err;
    }
    return { totalCount: 4, isTruncated: false, vectors: [{ id: "c" }, { id: "d" }] };
  };
  const { ids } = await idsInIndex("ix", { fetchPage });
  assert.deepEqual([...ids].sort(), ["a", "b", "c", "d"], "must return the COMPLETE set");
});

test("a non-cursor failure surfaces instead of being retried away", async () => {
  const { idsInIndex } = await import("../scripts/reconcile-vault.mjs");
  const fetchPage = async () => { throw new Error("unauthorized"); };
  await assert.rejects(() => idsInIndex("ix", { fetchPage }), /unauthorized/);
});

test("a cursor that never recovers gives up rather than looping forever", async () => {
  const { idsInIndex } = await import("../scripts/reconcile-vault.mjs");
  let calls = 0;
  const fetchPage = async () => {
    calls++;
    const err = new Error("boom"); err.stderr = "cursor appears to be corrupted";
    throw err;
  };
  // The cursor text lives on err.stderr; the rethrown error keeps its own message,
  // which is what a caller sees. Assert on both so neither can drift.
  await assert.rejects(() => idsInIndex("ix", { attempts: 3, fetchPage }), /boom/);
  assert.equal(calls, 3, "bounded attempts — must not loop forever on a dead cursor");
});
