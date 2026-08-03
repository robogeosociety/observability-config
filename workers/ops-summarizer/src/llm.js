// llm — the Anthropic call, with a hard turn ceiling.
//
// TURN LIMITS. Both summarisers share this module so the ceiling is enforced in
// one place rather than trusted to each caller. Today the loop is single-turn by
// construction — RAG context is fetched up front and passed in the prompt, so
// there are no tool calls to iterate on — but the guard is written as a loop
// bound anyway, because the failure it prevents is the one that only appears
// later: someone adds a tool, the model starts a call/response cycle, and an
// unbounded loop burns a token quota that the queue then has to park around.
//
// MAX_TURNS is therefore a budget, not a formality. Hitting it is a bug, and it
// returns a partial result rather than throwing so the queue records an ack with
// a truncation note instead of dead-lettering work that mostly succeeded.

export const DEFAULT_MAX_TURNS = 3;
export const DEFAULT_MAX_TOKENS = 700;

// Haiku: cheap and fast, which is what a summary of a repeated alarm wants. The
// id is pinned rather than aliased so a model rotation is a deliberate commit.
export const HAIKU = "claude-haiku-4-5-20251001";

/** A quota rejection the queue must PARK on, not retry. */
export class QuotaError extends Error {
  constructor(message, retryAfterMs) {
    super(message);
    this.name = "QuotaError";
    this.retryAfterMs = retryAfterMs;
  }
}

/**
 * One bounded exchange with the Anthropic Messages API.
 *
 * @returns {Promise<{text: string, turns: number, toolCalls: number, truncated: boolean,
 *                     usage: object, llmMs: number}>}
 * @throws {QuotaError} on 429 or a quota-shaped 400, so the caller can surface a
 *         park rather than burning the item's retry budget.
 */
export async function complete(env, { system, user, maxTurns = DEFAULT_MAX_TURNS, maxTokens = DEFAULT_MAX_TOKENS }) {
  if (!env.ANTHROPIC_API_KEY) {
    throw new Error("ANTHROPIC_API_KEY is not set");
  }

  const messages = [{ role: "user", content: user }];
  const startedAt = Date.now();
  let turns = 0;
  let toolCalls = 0;
  let text = "";
  let usage = {};

  while (turns < maxTurns) {
    turns += 1;

    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({ model: HAIKU, max_tokens: maxTokens, system, messages }),
    });

    if (res.status === 429) {
      const ra = Number(res.headers.get("retry-after"));
      throw new QuotaError("anthropic 429", Number.isFinite(ra) && ra > 0 ? ra * 1000 : undefined);
    }

    if (!res.ok) {
      const body = await res.text();
      // Credit exhaustion arrives as a 400 with a typed error, not a 429 — treated
      // as a park because retrying an out-of-credit account is pure waste.
      if (/credit balance|quota|rate_limit/i.test(body)) {
        throw new QuotaError(`anthropic ${res.status}: ${body.slice(0, 200)}`);
      }
      const err = new Error(`anthropic ${res.status}: ${body.slice(0, 300)}`);
      err.status = res.status;
      throw err;
    }

    const data = await res.json();
    // Usage is per-response, so accumulate across turns rather than overwriting —
    // otherwise a multi-turn call reports only its last leg and understates cost.
    const u = data.usage || {};
    usage = {
      input_tokens: (usage.input_tokens || 0) + (u.input_tokens || 0),
      output_tokens: (usage.output_tokens || 0) + (u.output_tokens || 0),
    };
    toolCalls += (data.content || []).filter((b) => b.type === "tool_use").length;
    text += (data.content || [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");

    // Single-turn today: nothing asks for another round. When tools land, the
    // continuation goes here and the loop bound above starts earning its keep.
    if (data.stop_reason !== "tool_use") {
      return { text, turns, toolCalls, truncated: false, usage, llmMs: Date.now() - startedAt };
    }
    messages.push({ role: "assistant", content: data.content });
  }

  // Ceiling hit. Partial, flagged, not thrown — see the header.
  return { text, turns, toolCalls, truncated: true, usage, llmMs: Date.now() - startedAt };
}
