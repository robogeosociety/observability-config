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
// SPOILERS. `||text||` renders as a click-to-reveal blackout, and it composes with
// `-#`, so `-# ||…||` is small grey text that stays hidden until clicked. That is
// as close to expand-on-demand as Discord offers — there is no native collapsible
// block. Two constraints worth knowing, both verified rather than assumed:
//
//   * Spoilers work in message CONTENT and in embed DESCRIPTIONS. They do NOT
//     render in an embed footer, which is plain text — the footer would show the
//     literal pipes. So telemetry moved out of the footer and into the body.
//   * A newline inside `||…||` ends the spoiler. Multi-line detail therefore has
//     to be one line with separators, not a code block.
//
// NOT in an embed field either: fields are laid out in columns and a long
// telemetry string wraps badly next to a summary.

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

/**
 * The always-visible line.
 *
 * Ordered by what someone scanning #ops actually wants: which model, what it
 * cost, how much grounding it had, how long it took. Turns and tool calls appear
 * ONLY when they are interesting — a `1 turn · 0 tools` on every message is noise
 * that trains people to stop reading the line.
 */
export function summaryLine(t) {
  const bits = [
    t.model,
    `${compact(t.inputTokens)} in / ${compact(t.outputTokens)} out`,
    // Distinct notes, not raw chunks — see byNote().
    `${byNote(t.hits).length || t.ragHits} rag`,
    ms(t.totalMs),
  ];
  if (t.turns > 1) bits.splice(2, 0, `${t.turns} turns`);
  if (t.toolCalls > 0) bits.splice(t.turns > 1 ? 3 : 2, 0, `${t.toolCalls} tools`);
  if (t.truncated) bits.push("⚠ turn ceiling");
  return `-# ${bits.join(" · ")}`;
}

/**
 * The spoilered detail line — the part worth clicking for when a summary looks
 * wrong: which notes grounded it and where the time went.
 *
 * Single line by necessity (a newline closes the spoiler), so separators do the
 * work of layout.
 */
export function detailLine(t) {
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

  parts.push(`${t.turns}/${t.maxTurns} turns, ${t.toolCalls} tool calls`);
  if (t.truncated) parts.push("hit the turn ceiling — output is partial");

  return `-# ||${parts.join(" · ")}||`;
}

/** Both lines, ready to append to a message body. */
export function render(t) {
  return `${summaryLine(t)}\n${detailLine(t)}`;
}
