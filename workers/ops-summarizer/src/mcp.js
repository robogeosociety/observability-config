// mcp — the dev vault as an MCP tool, over Streamable HTTP.
//
// WHY THIS EXISTS. The Vectorize index already backs the summariser's retrieval,
// but that only helps the summariser. Every other Claude surface in the fleet —
// headless `claude -p` discobot jobs, the @obsidian and dev channel bots,
// interactive sessions — has no access to the vault at all. They all speak MCP,
// and MCP servers are configured per project/user rather than per session, so ONE
// server here reaches all of them with no per-bot wiring. That is the whole
// argument for putting it on this Worker rather than building three integrations.
//
// IMPLEMENTED BY HAND, NO SDK. Streamable HTTP for a stateless, tools-only server
// is a small JSON-RPC surface — initialize, tools/list, tools/call — and the
// sibling Workers in this repo are all dependency-free. Pulling in the Agents SDK
// to expose one search function would be the larger commitment.
//
// STATELESS ON PURPOSE. The spec makes `Mcp-Session-Id` optional, and retrieval
// has no session state worth keeping: each search is independent. Skipping
// sessions means no eviction story, no session table, and no 404-restart dance
// for clients.

const PROTOCOL_VERSION = "2025-06-18";
// Clients that send no MCP-Protocol-Version header are, per spec, assumed to be
// on this older revision. Accepted so we do not reject well-behaved older clients.
const SUPPORTED_VERSIONS = new Set([PROTOCOL_VERSION, "2025-03-26", "2024-11-05"]);

const SERVER_INFO = { name: "dev-vault", version: "1.0.0" };

const TOOLS = [
  {
    name: "search_vault",
    description:
      "Semantic search over Tommy's Obsidian vaults. `dev` holds infrastructure " +
      "runbooks, incident write-ups and fleet architecture (the Mac mini, Cloudflare " +
      "Workers, the discobots, the message bus); `camping` holds trip and campsite " +
      "notes; `gear` equipment; `home` household; `travel` trip planning. Use this " +
      "before answering questions about any of them: the vaults are the operator's " +
      "own record and outrank general knowledge. Searches all vaults unless one is " +
      "named. Returns excerpts, not whole notes.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "What to look for. Natural language works better than keywords.",
        },
        k: {
          type: "integer",
          description: "How many passages to return (1-20, default 5).",
          minimum: 1,
          maximum: 20,
        },
        vault: {
          type: "string",
          description:
            "Restrict to one vault. Omit to search all — usually right, since the " +
            "answer may not be in the vault you expect.",
          enum: ["dev", "camping", "gear", "home", "travel"],
        },
      },
      required: ["query"],
    },
  },
];

const rpcResult = (id, result) => ({ jsonrpc: "2.0", id, result });
const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id, error: { code, message } });

const jsonResponse = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

/**
 * DNS-rebinding protection, which the transport spec requires. A browser page can
 * reach a Worker, so this is not only a localhost concern: without it, any origin
 * a signed-in user visits could drive this server.
 *
 * MCP clients are not browsers and send no Origin, so absent is allowed; a
 * *present* Origin must be one we know.
 */
function originAllowed(req, env) {
  const origin = req.headers.get("origin");
  if (!origin) return true;
  const allowed = (env.MCP_ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return allowed.includes(origin);
}

function versionAllowed(req) {
  const v = req.headers.get("mcp-protocol-version");
  return !v || SUPPORTED_VERSIONS.has(v);
}

/** Dispatch one JSON-RPC message. Returns null for notifications (no reply). */
async function dispatch(msg, env, deps) {
  const { id, method, params } = msg;

  // Notifications carry no id and MUST NOT be answered with a result.
  const isNotification = id === undefined || id === null;

  if (method === "initialize") {
    // Echo the client's version when we support it, else answer with ours and let
    // it decide — that is the negotiation the spec describes.
    const asked = params?.protocolVersion;
    return rpcResult(id, {
      protocolVersion: SUPPORTED_VERSIONS.has(asked) ? asked : PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: SERVER_INFO,
    });
  }

  if (method === "notifications/initialized" || method === "notifications/cancelled") {
    return null;
  }

  if (method === "ping") return rpcResult(id, {});

  if (method === "tools/list") {
    return rpcResult(id, { tools: TOOLS });
  }

  if (method === "tools/call") {
    const name = params?.name;
    if (name !== "search_vault") {
      return rpcError(id, -32602, `unknown tool: ${name}`);
    }
    const query = params?.arguments?.query;
    if (typeof query !== "string" || !query.trim()) {
      return rpcError(id, -32602, "query is required");
    }
    const k = Math.min(Math.max(1, Number(params?.arguments?.k) || 5), 20);
    const vault = params?.arguments?.vault || null;

    const matches = await deps.retrieve(env, query, k, vault);

    if (!matches.length) {
      // A tool result, not an error: "nothing matched" is a legitimate answer and
      // an error would make the model retry or apologise instead of moving on.
      return rpcResult(id, {
        content: [{ type: "text", text: `No vault passages matched "${query}".` }],
      });
    }

    const text = matches
      .map(
        (m) =>
          `## ${m.title || m.path}\n_score ${m.score.toFixed(3)} · ${m.vault ? m.vault + " · " : ""}${m.path}_\n\n${m.text}`,
      )
      .join("\n\n---\n\n");

    return rpcResult(id, { content: [{ type: "text", text }] });
  }

  if (isNotification) return null;
  return rpcError(id, -32601, `method not found: ${method}`);
}

/**
 * Handle a request to the MCP endpoint.
 * @param {Request} req
 * @param {object} env
 * @param {{retrieve: Function}} deps injected so the transport can be tested
 *        without Vectorize or Workers AI bindings
 */
export async function handleMcp(req, env, deps) {
  if (!originAllowed(req, env)) return jsonResponse({ error: "origin not allowed" }, 403);
  if (!versionAllowed(req)) {
    // The spec is explicit that an unsupported version is a 400, not a negotiation.
    return jsonResponse({ error: "unsupported MCP-Protocol-Version" }, 400);
  }

  // No server-initiated stream, so the spec's prescribed answer to GET is 405.
  if (req.method === "GET") {
    return new Response("this server does not offer an SSE stream", { status: 405 });
  }

  // Stateless: there is no session to delete.
  if (req.method === "DELETE") return new Response(null, { status: 405 });

  if (req.method !== "POST") return new Response(null, { status: 405 });

  let body;
  try {
    body = await req.json();
  } catch {
    return jsonResponse(rpcError(null, -32700, "parse error"), 400);
  }

  // A batch is an array; a single message is an object. Both are legal JSON-RPC.
  const batch = Array.isArray(body) ? body : [body];
  const replies = [];
  for (const msg of batch) {
    try {
      const reply = await dispatch(msg, env, deps);
      if (reply) replies.push(reply);
    } catch (e) {
      // An internal fault must still be a well-formed JSON-RPC error, or the
      // client sees a transport failure and drops the whole connection.
      if (msg?.id !== undefined && msg?.id !== null) {
        replies.push(rpcError(msg.id, -32603, `internal error: ${e}`));
      }
    }
  }

  // Everything was a notification or response → 202 with no body, per spec.
  if (!replies.length) return new Response(null, { status: 202 });

  return jsonResponse(Array.isArray(body) ? replies : replies[0]);
}

export const __test = { dispatch, TOOLS, PROTOCOL_VERSION, SUPPORTED_VERSIONS };
