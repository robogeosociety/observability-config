// What rag-compare treats as a regression — and, more importantly, what it does not.
//
// The two stores use different embedding models over different chunkings, so they
// legitimately disagree about which notes are best. A check that demanded agreement
// would fail forever and get ignored, which is the exact complaint
// obsidian-automations#336 makes about a job that has failed since July.
//
// So overlap is reported, never gated. A store that STOPS ANSWERING is the signal.

import { test } from "node:test";
import assert from "node:assert/strict";
import { verdict } from "../scripts/compare-rag.mjs";

const row = (o = {}) => ({ jaccard: 0.3, vectorize: 5, vecserve: 5, vectorizeMs: 200, vecserveMs: 40, ...o });

test("total disagreement still passes, as long as both stores answer", () => {
  // The load-bearing assertion. If this ever flips, someone has turned a taste
  // difference into an alarm and the check will be muted within a month.
  const v = verdict([row({ jaccard: 0 }), row({ jaccard: 0 })]);
  assert.equal(v.ok, true);
  assert.equal(v.meanOverlap, 0);
});

test("a store that returns nothing is a regression", () => {
  const v = verdict([row(), row({ vecserve: 0 })]);
  assert.equal(v.ok, false);
  assert.equal(v.silent, 1);
});

test("either side going silent counts, not just the local one", () => {
  assert.equal(verdict([row({ vectorize: 0 })]).ok, false);
});

test("an errored query is a regression and is not counted as silent", () => {
  const v = verdict([row({ err: "vecserve: ECONNREFUSED", vectorize: 0, vecserve: 0 })]);
  assert.equal(v.ok, false);
  assert.equal(v.errored, 1);
  assert.equal(v.silent, 0, "an error is already reported; double-counting hides the cause");
});

test("latency is reported as a median per store", () => {
  const v = verdict([row({ vecserveMs: 40, vectorizeMs: 200 }), row({ vecserveMs: 60, vectorizeMs: 300 })]);
  assert.equal(v.vecserveMs, 60);
  assert.equal(v.vectorizeMs, 300);
});

test("an errored query does not drag the overlap average down", () => {
  // Its jaccard is meaningless — nothing came back to compare.
  const v = verdict([row({ jaccard: 0.5 }), row({ err: "boom", jaccard: 0 })]);
  assert.equal(v.meanOverlap, 0.5);
});
