// telemetry — what a summary cost, rendered for Discord.
//
// Modelled on the @obsidian bot's existing footer, which is a single subtext line:
//
//   -# sonnet-5 · 89.3k in / 428 out
//
// Discord's `-# ` prefix renders small grey text. That is the right default: the
// summary is the message, and the cost is a glance, not a section.
//
// This adds the numbers that line lacks — retrieval hits, latency, turns, tool
// calls — without turning the footer into a paragraph. The rule is one visible
// line, everything else behind a spoiler.
//
// Both lines are plain `-#` subtext. Spoilers (`||…||`) were tried and dropped by
// preference — they are the only expand-on-demand Discord offers, but they make
// the reader click to see whether an answer was grounded, which is exactly the
// thing worth seeing at a glance.
//
// Still not an embed: the telemetry uses `-#` subtext, which does not render in an
// embed footer (plain text) and wraps badly in a field (columns). Plain message
// content also matches the @obsidian bot's existing shape.

/**
 * Formatting options, because the same numbers suit two different places.
 *
 *   detail  — both lines (#ops, where a summary is the message and someone may
 *             need to see what grounded it) or just the first (#dev threads,
 *             where the footnote annotates a card that is already terse).
 *   prefix  — Discord subtext by default. Overridable for surfaces that do not
 *             render `-# `, such as an embed footer.
 *
 * Defaults reproduce the original two-line #ops behaviour exactly, so existing
 * callers need no change.
 */
export const DEFAULTS = { detail: true, prefix: "-# " };

/** 12345 → "12.3k". Matches the existing bot's token formatting. */
export function compact(n) {
  if (n == null || Number.isNaN(n)) return "?";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

export function ms(n) {
  if (n == null) return "?";
  return n < 1000 ? `${Math.round(n)}ms` : `${(n / 1000).toFixed(1)}s`;
}

/**
 * Collapse chunk hits to distinct notes, best score first.
 *
 * Retrieval returns CHUNKS, and several usually come from the same note — a
 * five-hit result was measured as two notes. Reporting the raw count overstates
 * how much distinct grounding there was, and listing the same title four times
 * with near-identical scores is noise in a line meant to be glanceable.
 */
export function byNote(hits = []) {
  const best = new Map();
  for (const h of hits) {
    const key = `${h.vault || ""}/${h.title || h.path || ""}`;
    const prev = best.get(key);
    if (!prev) best.set(key, { ...h, chunks: 1 });
    else {
      prev.chunks += 1;
      if (h.score > prev.score) prev.score = h.score;
    }
  }
  return [...best.values()].sort((a, b) => b.score - a.score);
}

/** "(28.5k cached)" when a meaningful share of the input was cache reads. */
export function cacheNote(t) {
  const cached = (t.cacheReadTokens || 0) + (t.cacheWriteTokens || 0);
  if (!cached || !t.inputTokens) return "";
  return ` (${compact(cached)} cached)`;
}

/**
 * The always-visible line.
 *
 * Ordered by what someone scanning #ops actually wants: which model, what it
 * cost, how much grounding it had, how long it took. Turns and tool calls appear
 * ONLY when they are interesting — a `1 turn · 0 tools` on every message is noise
 * that trains people to stop reading the line.
 */
export function summaryLine(t, opts = {}) {
  const { prefix } = { ...DEFAULTS, ...opts };
  const bits = [
    t.model,
    // Total input, with the cached share broken out. `input_tokens` alone counts
    // only uncached input — a measured run was 10 fresh against 32.6k total —
    // so a single "in" number would understate the prompt enormously and price
    // it wrongly, since the three classes bill differently.
    `${compact(t.inputTokens)} in${cacheNote(t)} / ${compact(t.outputTokens)} out`,
    // Distinct notes, not raw chunks — see byNote().
    `${byNote(t.hits).length || t.ragHits} rag`,
    ms(t.totalMs),
  ];
  if (t.turns > 1) bits.splice(2, 0, `${t.turns} turns`);
  if (t.toolCalls > 0) bits.splice(t.turns > 1 ? 3 : 2, 0, `${t.toolCalls} tools`);
  if (t.truncated) bits.push("⚠ turn ceiling");
  return `${prefix}${bits.join(" · ")}`;
}

/**
 * The second subtext line: which notes grounded the answer and where the time
 * went — the detail you want when a summary looks wrong.
 *
 * Kept to one line with separators rather than wrapped across several. Two short
 * grey lines under a summary read as a footer; five read as a second message.
 */
export function detailLine(t, opts = {}) {
  const { prefix } = { ...DEFAULTS, ...opts };
  const parts = [];

  const notes = byNote(t.hits);
  if (notes.length) {
    parts.push(
      "grounded on " +
        notes
          .map(
            (h) =>
              `${h.vault ? h.vault + "/" : ""}${h.title} (${h.score.toFixed(2)}` +
              `${h.chunks > 1 ? `, ×${h.chunks}` : ""})`,
          )
          .join(", "),
    );
  } else {
    // Worth stating rather than omitting: an ungrounded summary is a different
    // thing from a grounded one, and silence reads as "not measured".
    parts.push("no vault context — answered from the model alone");
  }

  const timings = [];
  if (t.ragMs != null) timings.push(`retrieval ${ms(t.ragMs)}`);
  if (t.llmMs != null) timings.push(`model ${ms(t.llmMs)}`);
  if (timings.length) parts.push(timings.join(", "));

  // Token classes spelled out for cost attribution — cache writes cost more than
  // fresh input, cache reads far less, and a rollup cannot separate them later.
  if (t.cacheWriteTokens || t.cacheReadTokens) {
    parts.push(
      `tokens ${compact(t.freshInputTokens)} fresh / ${compact(t.cacheWriteTokens)} cache-write / ` +
        `${compact(t.cacheReadTokens)} cache-read / ${compact(t.outputTokens)} out`,
    );
  }

  // toolCalls is null when the runtime does not report it. "0 tool calls" would
  // be a claim we cannot make.
  const tools = t.toolCalls == null ? "tool calls not reported" : `${t.toolCalls} tool calls`;
  parts.push(`${t.turns}/${t.maxTurns} turns, ${tools}`);
  if (t.truncated) parts.push("hit the turn ceiling — output is partial");

  return `${prefix}${parts.join(" · ")}`;
}

/**
 * The footnote, ready to append to a message body.
 *
 * `detail: false` gives the one-line form. That line already carries everything a
 * #dev reader asked for — model, turns, tools, tokens, rag hits, latency — so the
 * second line is additive rather than load-bearing, and omitting it keeps a thread
 * annotation from outweighing the card it annotates.
 */
export function render(t, opts = {}) {
  const o = { ...DEFAULTS, ...opts };
  return o.detail ? `${summaryLine(t, o)}\n${detailLine(t, o)}` : summaryLine(t, o);
}
