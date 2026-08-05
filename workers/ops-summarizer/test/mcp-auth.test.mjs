// /mcp takes a search-only bearer, and it must not become a way in to the rest.
//
// The clients are channel bots running with --dangerously-skip-permissions. The
// shared HANDLER_TOKEN unlocks /post (posts to the #ops webhook), /ingest (rewrites
// the retrieval index), /summarize and /fail. A bot asking "what does the vault say
// about X" needs none of that. One credential for every caller is tidy right up
// until the tidiest caller is a language model with a shell.
//
// These assert the boundary in both directions: MCP_TOKEN opens /mcp and nothing
// else, and removing it changes nothing for the existing callers.

import { test } from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.js";

const ENV = { HANDLER_TOKEN: "full-token", MCP_TOKEN: "search-token" };

const call = (path, token, env = ENV, method = "POST") =>
  worker.fetch(
    new Request(`https://w.example${path}`, {
      method,
      headers: token
        ? { authorization: `Bearer ${token}`, "content-type": "application/json" }
        : { "content-type": "application/json" },
      body: method === "POST" ? JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) : undefined,
    }),
    env,
    { waitUntil() {}, passThroughOnException() {} },
  );

test("the search-only bearer opens /mcp", async () => {
  const res = await call("/mcp", "search-token");
  assert.notEqual(res.status, 401);
});

test("the search-only bearer opens NOTHING else", async () => {
  // The whole point. If any of these stops returning 401, a channel bot has just
  // gained the ability to post to #ops or rewrite the index.
  for (const path of ["/post", "/ingest", "/summarize", "/fail"]) {
    const res = await call(path, "search-token");
    assert.equal(res.status, 401, `${path} must reject the search-only bearer`);
  }
});

test("the full bearer still opens /mcp", async () => {
  const res = await call("/mcp", "full-token");
  assert.notEqual(res.status, 401);
});

test("a wrong bearer opens nothing", async () => {
  assert.equal((await call("/mcp", "nope")).status, 401);
  assert.equal((await call("/ingest", "nope")).status, 401);
});

test("no bearer opens nothing", async () => {
  assert.equal((await call("/mcp", null)).status, 401);
});

test("with MCP_TOKEN unset the route behaves exactly as before", async () => {
  // Additive by construction: an unconfigured Worker must not start accepting a
  // token it was never given, and must keep accepting the one it has.
  const env = { HANDLER_TOKEN: "full-token" };
  assert.equal((await call("/mcp", "search-token", env)).status, 401);
  assert.notEqual((await call("/mcp", "full-token", env)).status, 401);
});

test("an empty MCP_TOKEN does not make the empty string a valid bearer", async () => {
  const env = { HANDLER_TOKEN: "full-token", MCP_TOKEN: "" };
  assert.equal((await call("/mcp", "", env)).status, 401);
});
