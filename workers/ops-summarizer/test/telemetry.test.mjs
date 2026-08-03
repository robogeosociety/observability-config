// Tests for the telemetry footer. Pure string rendering — no runtime, no network.
import assert from "node:assert/strict";
import { test } from "node:test";
import { byNote, compact, detailLine, ms, render, summaryLine } from "../src/telemetry.js";

const base = {
  model: "haiku-4-5",
  inputTokens: 12400,
  outputTokens: 380,
  turns: 1,
  maxTurns: 3,
  toolCalls: 0,
  truncated: false,
  ragHits: 5,
  hits: [
    { vault: "dev", title: "Wiki/Infrastructure/Volume Wedges", score: 0.783 },
    { vault: "dev", title: "Tasks/GitHub/infra/25", score: 0.78 },
  ],
  ragMs: 143,
  llmMs: 1120,
  totalMs: 1290,
};

test("token counts match the existing bot's formatting", () => {
  assert.equal(compact(12400), "12.4k");
  assert.equal(compact(380), "380");
  assert.equal(compact(89300), "89.3k");
  assert.equal(compact(null), "?");
});

test("durations switch units at a second", () => {
  assert.equal(ms(143), "143ms");
  assert.equal(ms(1290), "1.3s");
  assert.equal(ms(null), "?");
});

test("the visible line is subtext and carries the four headline numbers", () => {
  const l = summaryLine(base);
  assert.ok(l.startsWith("-# "), "must be Discord subtext");
  assert.match(l, /haiku-4-5/);
  assert.match(l, /12\.4k in \/ 380 out/);
  // 2, not 5: `base` has five chunk hits but only two distinct notes.
  assert.match(l, /2 rag/);
  assert.match(l, /1\.3s/);
});

test("a single turn with no tools stays off the visible line", () => {
  // Printing `1 turn · 0 tools` on every message is noise that trains people to
  // stop reading the line at all.
  const l = summaryLine(base);
  assert.doesNotMatch(l, /turns/);
  assert.doesNotMatch(l, /tools/);
});

test("turns and tool calls appear once they are interesting", () => {
  const l = summaryLine({ ...base, turns: 3, toolCalls: 2 });
  assert.match(l, /3 turns/);
  assert.match(l, /2 tools/);
});

test("hitting the ceiling is flagged where it cannot be missed", () => {
  assert.match(summaryLine({ ...base, truncated: true }), /turn ceiling/);
});

test("the detail line is a spoiler so it expands on demand", () => {
  const d = detailLine(base);
  assert.ok(d.startsWith("-# ||") && d.endsWith("||"), "must be a subtext spoiler");
});

test("a newline would close the spoiler, so the detail stays single-line", () => {
  // Discord ends `||…||` at a line break; multi-line detail silently leaks.
  assert.doesNotMatch(detailLine(base), /\n/);
});

test("detail names the grounding notes and their scores", () => {
  const d = detailLine(base);
  assert.match(d, /dev\/Wiki\/Infrastructure\/Volume Wedges \(0\.78\)/);
  assert.match(d, /retrieval 143ms/);
  assert.match(d, /model 1\.1s/);
  assert.match(d, /1\/3 turns/);
});

test("an ungrounded answer says so rather than omitting it", () => {
  // Silence would read as "not measured"; ungrounded is a materially different
  // thing from grounded and the reader should be able to tell.
  const d = detailLine({ ...base, hits: [], ragHits: 0 });
  assert.match(d, /no vault context/);
});

test("render produces exactly the two lines", () => {
  const lines = render(base).split("\n");
  assert.equal(lines.length, 2);
  assert.ok(lines[0].startsWith("-# ") && !lines[0].includes("||"));
  assert.ok(lines[1].startsWith("-# ||"));
});

test("chunk hits collapse to distinct notes, best score first", () => {
  // Retrieval returns chunks and several usually come from one note. Counting
  // them raw overstates the grounding, and listing a title four times with
  // near-identical scores is noise in a glanceable line.
  const notes = byNote([
    { vault: "dev", title: "Volume Wedges", score: 0.77 },
    { vault: "dev", title: "Volume Wedges", score: 0.79 },
    { vault: "dev", title: "Volume Wedges", score: 0.78 },
    { vault: "dev", title: "infra/25", score: 0.85 },
  ]);
  assert.equal(notes.length, 2);
  assert.equal(notes[0].title, "infra/25", "highest score leads");
  assert.equal(notes[1].score, 0.79, "keeps the best score, not the last");
  assert.equal(notes[1].chunks, 3);
});

test("a repeated note is shown once with a chunk count", () => {
  const d = detailLine({
    ...base,
    hits: [
      { vault: "dev", title: "Volume Wedges", score: 0.79 },
      { vault: "dev", title: "Volume Wedges", score: 0.77 },
    ],
  });
  assert.match(d, /Volume Wedges \(0\.79, ×2\)/);
  assert.equal((d.match(/Volume Wedges/g) || []).length, 1, "must not repeat the title");
});

test("the same note across different vaults stays distinct", () => {
  assert.equal(
    byNote([
      { vault: "dev", title: "Automations", score: 0.7 },
      { vault: "home", title: "Automations", score: 0.6 },
    ]).length,
    2,
  );
});
