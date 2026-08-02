// Transport-level tests for the MCP endpoint. Pure — retrieval is injected, so
// none of this needs Vectorize, Workers AI, or the network.
//
// These matter more than they look: an MCP client that gets a malformed
// initialize or a JSON-RPC error where it expected a result does not degrade
// gracefully, it drops the server. The failure then looks like "the vault tool
// isn't there" with nothing explaining why.
import assert from "node:assert/strict";
import { test } from "node:test";
import { handleMcp, __test } from "../src/mcp.js";

const { PROTOCOL_VERSION } = __test;

const fakeRetrieve = async (_env, query, k) =>
  query === "nothing"
    ? []
    : Array.from({ length: k }, (_, i) => ({
        score: 0.9 - i * 0.1,
        path: `Wiki/note-${i}.md`,
        title: `note ${i}`,
        text: `body ${i}`,
      }));

const deps = { retrieve: fakeRetrieve };
const env = {};

function post(body, headers = {}) {
  return new Request("https://w.dev/mcp", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

const call = async (body, headers) => {
  const res = await handleMcp(post(body, headers), env, deps);
  return { res, json: res.status === 202 ? null : await res.json() };
};

test("initialize returns a protocol version and tools capability", async () => {
  const { json } = await call({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  assert.equal(json.id, 1);
  assert.equal(json.result.protocolVersion, PROTOCOL_VERSION);
  assert.ok(json.result.capabilities.tools, "must advertise tools capability");
  assert.equal(json.result.serverInfo.name, "dev-vault");
});

test("initialize echoes a supported version the client asked for", async () => {
  const { json } = await call({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-03-26" },
  });
  assert.equal(json.result.protocolVersion, "2025-03-26");
});

test("tools/list advertises search_vault with a required query", async () => {
  const { json } = await call({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  const tool = json.result.tools.find((t) => t.name === "search_vault");
  assert.ok(tool);
  assert.deepEqual(tool.inputSchema.required, ["query"]);
  // The description is what makes a model reach for this instead of guessing, so
  // it must actually say the vault outranks general knowledge.
  assert.match(tool.description, /outranks general knowledge/i);
});

test("tools/call returns passages as text content", async () => {
  const { json } = await call({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "search_vault", arguments: { query: "wedge", k: 2 } },
  });
  assert.equal(json.result.content[0].type, "text");
  assert.match(json.result.content[0].text, /note 0/);
  assert.match(json.result.content[0].text, /note 1/);
  assert.ok(!json.error);
});

test("k is clamped, not trusted", async () => {
  const { json } = await call({
    jsonrpc: "2.0",
    id: 4,
    method: "tools/call",
    params: { name: "search_vault", arguments: { query: "x", k: 9999 } },
  });
  // 20 is the cap; an unclamped k would let one call pull the whole index into a
  // prompt.
  assert.equal((json.result.content[0].text.match(/^## /gm) || []).length, 20);
});

test("no matches is a result, not an error", async () => {
  // An error would make the model retry or apologise; "nothing matched" is a
  // legitimate answer it should just report.
  const { json } = await call({
    jsonrpc: "2.0",
    id: 5,
    method: "tools/call",
    params: { name: "search_vault", arguments: { query: "nothing" } },
  });
  assert.ok(!json.error);
  assert.match(json.result.content[0].text, /No vault passages matched/);
});

test("a missing query is a JSON-RPC invalid-params error", async () => {
  const { json } = await call({
    jsonrpc: "2.0",
    id: 6,
    method: "tools/call",
    params: { name: "search_vault", arguments: {} },
  });
  assert.equal(json.error.code, -32602);
});

test("an unknown tool errors rather than silently searching", async () => {
  const { json } = await call({
    jsonrpc: "2.0",
    id: 7,
    method: "tools/call",
    params: { name: "rm_rf", arguments: {} },
  });
  assert.equal(json.error.code, -32602);
});

test("unknown methods return method-not-found", async () => {
  const { json } = await call({ jsonrpc: "2.0", id: 8, method: "resources/list" });
  assert.equal(json.error.code, -32601);
});

test("notifications get 202 and no body", async () => {
  const { res, json } = await call({ jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal(res.status, 202);
  assert.equal(json, null);
});

test("a batch returns an array of only the answerable messages", async () => {
  const { json } = await call([
    { jsonrpc: "2.0", id: 1, method: "ping" },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list" },
  ]);
  assert.ok(Array.isArray(json));
  assert.equal(json.length, 2, "the notification must not produce a reply");
});

test("GET is 405 — no SSE stream is offered", async () => {
  const res = await handleMcp(new Request("https://w.dev/mcp"), env, deps);
  assert.equal(res.status, 405);
});

test("a foreign Origin is rejected (DNS rebinding)", async () => {
  const res = await handleMcp(
    post({ jsonrpc: "2.0", id: 1, method: "ping" }, { origin: "https://evil.example" }),
    { MCP_ALLOWED_ORIGINS: "https://ok.example" },
    deps,
  );
  assert.equal(res.status, 403);
});

test("no Origin is allowed — MCP clients are not browsers", async () => {
  const { json } = await call({ jsonrpc: "2.0", id: 1, method: "ping" });
  assert.deepEqual(json.result, {});
});

test("an unsupported protocol version is a 400", async () => {
  const res = await handleMcp(
    post({ jsonrpc: "2.0", id: 1, method: "ping" }, { "mcp-protocol-version": "1999-01-01" }),
    env,
    deps,
  );
  assert.equal(res.status, 400);
});

test("a retrieval fault becomes a JSON-RPC error, not a transport failure", async () => {
  const boom = {
    retrieve: async () => {
      throw new Error("vectorize down");
    },
  };
  const res = await handleMcp(
    post({ jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "search_vault", arguments: { query: "x" } } }),
    env,
    boom,
  );
  const json = await res.json();
  assert.equal(json.error.code, -32603);
  assert.equal(res.status, 200, "a well-formed error still returns 200 at the transport");
});

test("malformed JSON is a parse error", async () => {
  const res = await handleMcp(
    new Request("https://w.dev/mcp", { method: "POST", body: "{not json" }),
    env,
    deps,
  );
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error.code, -32700);
});
