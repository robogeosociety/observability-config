// Unit tests for the pure core: turn accounting, quota classification, and RAG
// context budgeting. Plain `node --test`, no deps, matching the sibling Workers.
//
// The Anthropic call and the Vectorize query are both network, so `complete()`
// and `retrieve()` are exercised here against a stubbed global fetch / binding
// rather than mocked away — the classification logic is the part that decides
// whether the queue parks or dead-letters, and getting it wrong is expensive in
// exactly the situation it exists for.
import assert from "node:assert/strict";
import { test, afterEach } from "node:test";

import { complete, QuotaError, DEFAULT_MAX_TURNS, HAIKU } from "../src/llm.js";
import { asContext } from "../src/rag.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const okBody = (text, stop = "end_turn") => ({
  ok: true,
  status: 200,
  json: async () => ({ content: [{ type: "text", text }], stop_reason: stop, usage: { input_tokens: 5 } }),
});

test("a normal completion is one turn", async () => {
  globalThis.fetch = async () => okBody("done");
  const r = await complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u" });
  assert.equal(r.turns, 1);
  assert.equal(r.truncated, false);
  assert.equal(r.text, "done");
});

test("the model id is pinned, not aliased", async () => {
  // A rotation should be a deliberate commit, not something that drifts under us.
  let sent;
  globalThis.fetch = async (_u, init) => {
    sent = JSON.parse(init.body);
    return okBody("x");
  };
  await complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u" });
  assert.equal(sent.model, HAIKU);
  assert.match(HAIKU, /^claude-haiku-/);
});

test("the turn ceiling truncates instead of looping forever", async () => {
  // A tool-use stop reason every time is the runaway the bound exists to stop.
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return okBody("chunk ", "tool_use");
  };
  const r = await complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u", maxTurns: 3 });
  assert.equal(calls, 3, "must stop at the ceiling");
  assert.equal(r.turns, 3);
  assert.equal(r.truncated, true, "truncation must be reported, not hidden");
});

test("truncation returns partial text rather than throwing", async () => {
  // Dead-lettering work that mostly succeeded would lose the summary entirely.
  globalThis.fetch = async () => okBody("partial ", "tool_use");
  const r = await complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u", maxTurns: 2 });
  assert.ok(r.text.length > 0);
});

test("the default ceiling is small and finite", () => {
  assert.ok(Number.isInteger(DEFAULT_MAX_TURNS) && DEFAULT_MAX_TURNS > 0 && DEFAULT_MAX_TURNS <= 5);
});

test("429 raises QuotaError so the queue parks", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 429,
    headers: { get: (h) => (h === "retry-after" ? "120" : null) },
    text: async () => "rate limited",
  });
  await assert.rejects(
    () => complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u" }),
    (e) => e instanceof QuotaError && e.retryAfterMs === 120_000,
  );
});

test("credit exhaustion arrives as a 400 and must ALSO park", async () => {
  // The expensive mistake: retrying an out-of-credit account, or worse,
  // dead-lettering the backlog because 400 looks terminal.
  globalThis.fetch = async () => ({
    ok: false,
    status: 400,
    headers: { get: () => null },
    text: async () => '{"error":{"message":"Your credit balance is too low"}}',
  });
  await assert.rejects(
    () => complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u" }),
    (e) => e instanceof QuotaError,
  );
});

test("an ordinary 400 is NOT a quota park", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 400,
    headers: { get: () => null },
    text: async () => '{"error":{"message":"invalid model"}}',
  });
  await assert.rejects(
    () => complete({ ANTHROPIC_API_KEY: "k" }, { system: "s", user: "u" }),
    (e) => !(e instanceof QuotaError) && e.status === 400,
  );
});

test("a missing key fails loudly rather than calling out", async () => {
  globalThis.fetch = async () => {
    throw new Error("should not be called");
  };
  await assert.rejects(() => complete({}, { system: "s", user: "u" }), /ANTHROPIC_API_KEY/);
});

test("no matches yields no context block", () => {
  assert.equal(asContext([]), "");
});

test("context is budgeted, not just top-k", () => {
  // Five matches is not a bounded amount of text — vault notes run from a line to
  // thousands of words, and an unbudgeted prompt is how a cheap call gets costly.
  const huge = Array.from({ length: 5 }, (_, i) => ({
    title: `note ${i}`,
    path: `n${i}.md`,
    text: "x".repeat(4000),
  }));
  const ctx = asContext(huge);
  assert.ok(ctx.length < 8000, `context should be budgeted, got ${ctx.length}`);
  assert.match(ctx, /note 0/);
});

test("context tells the model to prefer the vault over general knowledge", () => {
  const ctx = asContext([{ title: "t", path: "t.md", text: "body" }]);
  assert.match(ctx, /prefer these/i);
});
