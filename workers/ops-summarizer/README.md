# ops-summarizer

The handler behind fleet-bus's `work` topics, plus the dev vault as an MCP tool.

Two summary lanes:

| Topic | What it does |
| --- | --- |
| `fleet.github.notification` | a GitHub notification → a short summary |
| `fleet.ops.alarm.repeated` | an alarm that keeps firing → what is going on, grounded in the vault |

## Retrieval: Vectorize, not the mini's vecserve

The vault index lives on the mini (`vecserve`, `nomic-embed-text-v1.5`) and is
localhost-only there. Reaching it through the tunnel would put this lane back on
the host whose disk wedges — the dependency the whole effort has been removing.

So the dev vault is mirrored into Vectorize and embedded with Workers AI. That is
deliberately a **second embedding of the same corpus**: vectors embedded with
nomic cannot be searched with bge, and mixing them returns noise silently.
vecserve stays the mini's local tool; this is the cloud lane's copy.

**Only the `dev` vault is mirrored.** vecserve also indexes camping, gear, home
and travel — those have no Cloudflare equivalent.

### Re-ingesting

```sh
HANDLER_TOKEN=… node scripts/ingest-vault.mjs [vaultDir] [workerUrl]
```

The Worker embeds and upserts, so the script needs only the handler bearer and no
Cloudflare credential lands on disk. Ids are `sha1(path)[:24]#<chunk>` — Vectorize
caps ids at 64 bytes, which vault paths exceed, and hashing keeps re-ingest an
upsert rather than a duplicate.

## MCP: the vault for every Claude surface

`POST /mcp` speaks MCP over Streamable HTTP. One server reaches **all** of them —
headless `claude -p` discobot jobs, the `@obsidian` and dev channel bots, and
interactive sessions — because MCP config is per project/user, not per session.
That is the argument for putting it here rather than building three integrations.

Implemented by hand: for a stateless tools-only server this is a small JSON-RPC
surface (`initialize`, `tools/list`, `tools/call`), and every sibling Worker in
this repo is dependency-free. Pulling in an SDK to expose one search function
would be the larger commitment.

Stateless on purpose — the spec makes `Mcp-Session-Id` optional and each search is
independent, so there is no session table, no eviction story, and no 404-restart
dance for clients.

### Client config

Already installed at `/Users/tommy/dev/.mcp.json` (Air) and `/Volumes/dev/.mcp.json`
(mini), mode `600` because it carries a bearer:

```json
{
  "mcpServers": {
    "dev-vault": {
      "type": "http",
      "url": "https://ops-summarizer.tommy-b-doerr.workers.dev/mcp",
      "headers": { "Authorization": "Bearer $HANDLER_TOKEN" }
    }
  }
}
```

No tunnel is involved. The tunnel carries Cloudflare→mini; this is the mini (as
client) making an ordinary outbound HTTPS call. Cloudflare account auth is not
involved either — wrangler's OAuth is deploy-time only.

Rotating `HANDLER_TOKEN` means updating both files and fleet-bus's copy.

## Turn limits

Enforced once in `src/llm.js` so both lanes share a ceiling rather than trusting
each caller. Single-turn by construction today — RAG context is fetched up front,
so there are no tool calls to iterate on — but written as a loop bound, because
the failure it prevents only appears later: someone adds a tool, a call/response
cycle starts, and an unbounded loop burns the quota the queue then has to park
around.

Hitting the ceiling returns **partial text with a truncation flag** rather than
throwing, so mostly-successful work is not dead-lettered. Every posted summary
footers `turns/maxTurns` and whether it truncated.

## Quota: park, don't dead-letter

This is the fleet-bus handler contract and the part worth getting right.

| Response | Verdict | Why |
| --- | --- | --- |
| 429 | **park** | honours `Retry-After` |
| 400 `credit balance too low` | **park** | credit exhaustion arrives as a 400, *not* a 429 |
| 400 other | terminal | our bug; will not fix itself |
| 5xx | transient | the queue's backoff |

Getting the second row wrong is expensive in exactly the situation it exists for:
treating it as terminal dead-letters a healthy backlog.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | unauthenticated; reports capability, incl. whether the key is set |
| POST | `/mcp` | MCP Streamable HTTP |
| POST | `/summarize` | fleet-bus handler |
| POST | `/ingest` | admin — push vault chunks |
| GET | `/search?q=&k=` | retrieval without the model, for debugging |

All except `/health` need `Authorization: Bearer $HANDLER_TOKEN`.

## Tests

```sh
node --test "test/*.test.mjs"
```

29 tests, no dependencies. Transport behaviour is covered with retrieval injected,
so none of it needs Vectorize, Workers AI or the network. The MCP tests matter
more than they look: a client that gets a malformed `initialize` does not degrade
gracefully, it drops the server — and the failure then looks like "the vault tool
isn't there" with nothing explaining why.
