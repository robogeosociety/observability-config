---
type: proposal
implemented_by: []
tracking: 0
---

# Proposal — does a hosted Qwen2.5-Coder-32B change our inference calculus?

**Assessment, 2026-08-02.** Triggered by today's Vectorize migration: the `obsidian-vaults`
index (~5,300 chunks across all five vaults) now lives in Cloudflare, embedded with Workers AI
`@cf/baai/bge-base-en-v1.5`. Workers AI is therefore a live, working binding in this fleet for
the first time — which raises the question of whether `@cf/qwen/qwen2.5-coder-32b-instruct`,
Cloudflare's hosted 32B code model, should replace anything currently running on Claude. Short
answer: **no, not right now** — every real generation workload in this fleet either already
runs on subscription-metered `claude -p` (where a paid hosted alternative is a pure cost add)
or hasn't shipped a live metered call yet to compare against. This is a "keep watching, don't
adopt" outcome, not an advocacy document.

## 1. The facts, cited

All numbers below are from the live Cloudflare docs, fetched today (2026-08-02) — not from
training-data memory, since this model and Workers AI pricing are recent.

| Property | Value | Source |
| --- | --- | --- |
| Model ID | `@cf/qwen/qwen2.5-coder-32b-instruct` | [model page](https://developers.cloudflare.com/workers-ai/models/qwen2.5-coder-32b-instruct/) |
| Context window | 32,768 tokens | model page |
| Pricing | $0.66 / M input tokens, $1.00 / M output tokens | model page |
| Neuron equivalent | 60,000 neurons / M input tokens, 90,909 neurons / M output tokens | model page |
| Neuron unit price | $0.011 / 1,000 neurons | [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| Free allocation | 10,000 neurons/day, **shared across all Workers AI usage** on the account (resets 00:00 UTC) — not per-model | pricing page |
| Beta status | **Not** beta-tagged in the model catalog (unlike e.g. `phi-2`, `mistral-7b-instruct-v0.2`) — filed under Text Generation, generally available | [models catalog](https://developers.cloudflare.com/workers-ai/models/) |
| Streaming | Supported (`stream: true`, server-sent events) | model page |
| Rate limit | **Not listed with a model-specific override** in the limits table (unlike e.g. `qwen1.5-14b-chat-awq` at 150 rpm) → falls back to the **default Text Generation limit of 300 requests/minute** | [platform limits](https://developers.cloudflare.com/workers-ai/platform/limits/) |
| JSON mode / structured output | **Not supported** — the JSON Mode doc lists specific Llama/Hermes/DeepSeek models and qwen2.5-coder-32b-instruct is not among them | [JSON mode](https://developers.cloudflare.com/workers-ai/features/json-mode/) |
| Function/tool calling | **Unconfirmed.** The model exposes an OpenAI-compatible `/v1/chat/completions` endpoint with a `tool_calls` output field in the response schema, but Cloudflare's function-calling guide names only `@hf/nousresearch/hermes-2-pro-mistral-7b` as a tested/recommended tool-calling model and does not list qwen2.5-coder-32b-instruct. Treat as **not validated** until tested directly. | [function calling](https://developers.cloudflare.com/workers-ai/features/function-calling/) |
| Binding | `[ai]` binding in `wrangler.toml`/`.jsonc` → `env.AI.run(model, options)` | [bindings](https://developers.cloudflare.com/workers-ai/configuration/bindings/) |
| Fine-tuning | LoRA supported | model page |
| Batch API | **None found for Workers AI** (unlike the Anthropic API's Batches endpoint at 50% cost) | pricing page (no mention) |
| Committed-use / volume discounts | **None found** | pricing page (no mention) |

Two things worth flagging as decision-relevant on their own: there is **no JSON mode**, which
matters if any candidate workload wants structured output rather than prose, and the **10,000
neuron/day free tier is a single account-wide bucket** — it's already being drawn down by the
embedding calls that back the `obsidian-vaults` Vectorize index, so a new LLM workload doesn't
get its own separate allowance.

## 2. Candidate workloads in this fleet, workload by workload

I read the actual generation call sites rather than guessing. Three repos have Claude
generation paths; only one of the three candidate lanes is billed per token today.

### `robogeosociety/tommybot` — already retired, and already rejected this exact option

The 2026-07-04 decision to retire local-mini generation is **complete**, not aspirational.
`docs/models.md` and `docs/mini-cutover.md` confirm: the mini's Discord bot runs retrieval
locally (FTS/sqlite-vec/geo) and delegates generation to `TOMMYBOT_BACKEND=claude` — a headless
`claude -p` call, **subscription-authenticated, not per-token billed**. Model artifacts were
deleted at cutover; the rollback path is documented but unused. The Air still runs local MLX
(Qwen3-8B/14B) for its own heavier bots, unaffected by any of this.

More directly: `docs/always-on-mlx-lane.md` (2026-07-27, still open/unscheduled) already
evaluated **"Option D: Hosted open-weights — Qwen via an inference API"** as one of five
candidates for exactly the gap a hosted qwen2.5-coder would fill, and rejected it in writing:
*"Stops being local — the point of the slim lane is that it runs on Tommy's own metal."* A
Cloudflare-hosted qwen2.5-coder-32b does not change that reasoning — it is still someone else's
serverless platform, not local metal, and the always-on-MLX proposal's actual candidates (B:
dedicated MLX host, C: Air-as-server) remain the live options if that lane is ever revived.

**Recommendation: no action.** The retirement holds; this doesn't reopen it.

### `robogeosociety/discobots` — subscription `claude -p`, not metered

Three call sites use an LLM, and all three go through `claude -p` at Haiku, subscription-auth,
not API-key-billed:

- **`hooks/claude-session-summary/session_summary.py`** — condenses a Claude Code transcript
  into a #dev digest. `subprocess.run([find_claude(), "-p", "--model", MODEL], ...)`,
  `MODEL = os.environ.get("DEV_SUMMARY_MODEL", "claude-haiku-4-5-20251001")`.
- **`ops/card_enrich.py`** — writes a one-line summary for a captured "card." Same pattern:
  `subprocess.run(["claude", "-p", "--model", MODEL, ...])`.
- **`ops/dev_checkin.py`** — template-rendered by design, with only an optional `claude -p`
  narrative pass.

None of these has a marginal per-call cost today — `claude -p` runs against the Claude Code
subscription already paid for, on the Air and mini. Swapping any of them for a **metered**
Workers AI call would introduce cost where none currently exists, for tasks (transcript
summarization, card annotation) that are prose-shaped, not code-shaped — not what
qwen2.5-coder is specialized for anyway. `ops/github_discord.py` (the other GitHub→Discord
poster in this repo) has **no LLM call at all** — it's a plain webhook formatter — so there's no
overlap with the ops-summarizer lane below to worry about.

**Recommendation: no action.** There is no cost to save and no capability gap to fill.

### `robogeosociety/observability-config` — `workers/ops-summarizer` (PR #174, #175, both open/draft)

This is the one surface where a **metered** Claude API call exists — or will, once it's turned
on. Read `src/llm.js`, `src/index.js`, and `wrangler.toml` directly (from PR #175's branch,
since neither PR is merged to main yet):

- Two lanes, one shared turn-limited `complete()` helper: `fleet.github.notification` (GitHub
  notification → 2–3 sentence summary) and `fleet.ops.alarm.repeated` (a repeating #ops alarm →
  grounded explanation, RAG context pulled from the new `obsidian-vaults` Vectorize index).
- Model is pinned: `HAIKU = "claude-haiku-4-5-20251001"`, called via the plain Anthropic
  Messages API (`https://api.anthropic.com/v1/messages`) with `x-api-key: env.ANTHROPIC_API_KEY`
  — because, as the Worker's own README says, **"Workers AI does not host Claude."** This is the
  only reason this Worker needs an Anthropic key at all rather than just its `[ai]` binding.
- **It is not live yet.** PR #175's own description says `/health` currently reports
  `{"anthropicKey":false,"vectorize":true,"ai":true,"maxTurns":3}` — RAG works, but the
  Anthropic key hasn't been confirmed usable, so summaries are not yet flowing. `MAX_TURNS=3`
  is a safety ceiling on a loop that is single-turn by construction today (no tool calls).

**This is the one place a genuine comparison applies**, because it's the only lane paying
per-token for an LLM call rather than drawing on a subscription. But it's also the one place
with **zero real production volume to measure against** (see §4).

**Recommendation: worth a narrow, low-stakes experiment later — not now.** Once the
`ANTHROPIC_API_KEY` wiring lands and the lane is actually live for a few weeks, it would be
cheap to try swapping `fleet.github.notification` specifically (not the alarm lane, which
needs grounded, careful RAG reasoning) to `env.AI.run('@cf/qwen/qwen2.5-coder-32b-instruct', …)`
as an A/B, since it's the lower-stakes of the two summaries and the Worker already has an `[ai]`
binding wired for embeddings. Do this only after there's a baseline of real Haiku costs and
latencies to compare against — trying it blind teaches nothing.

## 3. Honest comparison — where hosted qwen wins, loses, or is redundant

| Dimension | Claude Haiku (Anthropic API, in the Worker) | `claude -p` (subscription, on Air/mini) | Hosted qwen2.5-coder-32b |
| --- | --- | --- | --- |
| Marginal cost per call | Metered, $1/$5 per M tokens (in/out) | **$0 marginal** — already-paid subscription | Metered, $0.66/$1.00 per M tokens |
| Where it wins | Nowhere on cost per se; wins on quality/groundedness for alarm reasoning | Wins everywhere it's used today — no reason to touch it | Wins only if it *displaces the Worker's Anthropic spend* at meaningful volume, and only for code-shaped output |
| Where it loses | N/A (incumbent) | N/A (incumbent, free) | Loses on JSON mode (unsupported), on confirmed tool-calling (unvalidated), on general prose quality vs. Haiku for the alarm-explanation lane specifically |
| Where it's redundant | — | — | **Everywhere qwen would touch `claude -p` call sites** — introduces cost with no capability the subscription lane lacks |

The honest framing: **hosted qwen only competes with the one Anthropic-API-billed surface in
this fleet**, and that surface doesn't have a cost problem yet because it isn't live. It cannot
compete with `claude -p` on cost (subscription beats any metered rate at $0 marginal), and the
discobots/tommybot lanes never call a metered API at all.

## 4. Cost modeling — grounded in real volume, or an honest admission there isn't any

I looked for actual usage numbers rather than inventing them:

- **`fleet.github.notification` / `fleet.ops.alarm.repeated` volume: no data exists.** The
  Worker isn't live (`anthropicKey: false`), so there is no request count, no token count, no
  latency sample to cite. `fleet-bus` (the Durable-Objects queue behind these topics, PR #174)
  is itself "deployed and exercised" but "the mini is not yet publishing to it" per that PR's
  own description — so even the queue's producer side isn't wired up.
- **Proxy signals, labeled as inference, not measurement:** the retired Valkey bus had "one key
  and zero clients" before this migration, and the supervisor's `fleet.supervisor.tick`
  heartbeat publishes every 300s — but that's a liveness ping, not a summarization trigger.
  GitHub notification volume across this fleet's repos and repeated-alarm volume in #ops are
  both plausibly **low-rate** (the task's own framing agrees), but I found no counter, log, or
  dashboard that quantifies "how many times a day" either lane would actually fire. Estimating a
  specific dollar figure from nothing would be inventing a number the evidence doesn't support.
- **What the pricing delta would look like *if* volume existed:** on equal token counts, Claude
  Haiku runs $1.00/$5.00 per M tokens (in/out) against qwen2.5-coder's $0.66/$1.00 — roughly
  **3–5x cheaper on output tokens**, which dominates cost for short summaries. At the volumes
  implied by "low-rate, event-driven alarms plus GitHub notifications across a handful of
  repos," this is very unlikely to add up to a meaningful dollar amount either way — almost
  certainly single-digit dollars a month on the Haiku side, meaning the switching cost (losing
  JSON mode, adding an unvalidated model, adding a second inference dependency) is not obviously
  worth chasing single-digit savings.

**I could not determine:** actual request volume for either ops-summarizer lane, actual
GitHub-notification frequency across the org's repos, or actual repeated-alarm frequency in
#ops. All three would need to be measured after the Worker goes live, not before.

## 5. Risks and unknowns

- **Function calling is unvalidated for this specific model.** If a future workload needs tool
  use, don't assume qwen2.5-coder-32b supports it cleanly just because the OpenAI-compatible
  endpoint's schema includes a `tool_calls` field — test it first.
- **No JSON mode** rules out any workload wanting constrained/structured output without a
  hand-rolled parsing layer.
- **Cold starts and P95 latency are unmeasured.** Cloudflare doesn't publish per-model latency
  SLAs on the pages checked, and this fleet has no production traffic on the model to sample.
- **Rate limit is the generic 300 rpm Text-Generation default**, not a model-specific override —
  fine for anything this fleet currently does, but worth re-checking if usage ever scales up.
- **Shared free-tier neuron budget.** The 10,000 neurons/day free allocation is already being
  spent by the `obsidian-vaults` embedding pipeline; a new LLM lane doesn't get its own bucket,
  so cost modeling for any future qwen workload must account for embeddings + generation
  together, not generation alone.
- **Platform newness.** Workers AI as a product is less mature than the Anthropic API this fleet
  already depends on for `claude -p` and the Haiku calls; no beta tag on this specific model, but
  the broader product surface (pricing page, limits page) reads as actively evolving — no
  committed-use pricing, no Batches-equivalent, sparser per-model documentation than Anthropic's.
- **What I could not determine:** whether Cloudflare offers any SLA/uptime commitment for
  Workers AI text generation specifically (the beta-status page for this returned a 404 and I
  did not find an equivalent elsewhere in the docs checked).

## 6. Recommendation

**Do not adopt `@cf/qwen/qwen2.5-coder-32b-instruct` anywhere in this fleet right now.** Two of
three candidate surfaces (tommybot, discobots) run on subscription `claude -p` where a metered
model is strictly worse — it adds real cost against a $0 marginal baseline. The third
(`ops-summarizer`'s two lanes) is the only place a metered comparison is even coherent, and it
isn't live yet, so there's no volume to justify a switch and no baseline to switch away from.
tommybot's own `always-on-mlx-lane.md` proposal already considered and rejected "hosted Qwen"
as an option for a different reason (it isn't local metal) that still applies.

The one thing worth doing later, cheaply: once `ops-summarizer` has a few weeks of real Haiku
traffic, A/B `fleet.github.notification` (not the alarm-reasoning lane) against
`@cf/qwen/qwen2.5-coder-32b-instruct` using the `[ai]` binding already present in that Worker,
and decide from measured cost/quality — not from this document's estimates.

## Open questions

- Does anyone plan to give `ops-summarizer` a genuinely code-shaped workload (e.g. summarizing
  a diff, drafting a commit message) where qwen2.5-coder's specialization would actually matter,
  versus the current prose-summary lanes where it's just "a cheaper LLM"?
- Should `ANTHROPIC_API_KEY` get wired into the Worker (per the still-open probe in PR #175)
  before or independent of this assessment? It's the actual blocker on getting real volume data.
- Is there appetite to revisit `always-on-mlx-lane.md`'s Option B (dedicated MLX host) instead,
  which keeps the local-metal property that's been the standing objection to hosted alternatives?
