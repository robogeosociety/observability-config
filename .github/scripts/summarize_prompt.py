#!/usr/bin/env python3
"""Build the summariser prompt from a repository_dispatch payload.

Kept out of the workflow YAML on purpose. Prompt text in a `run:` block has to
survive two levels of escaping (YAML, then shell), which is how a stray colon or
quote silently truncates a prompt — and a truncated prompt produces a plausible
answer to the wrong question, which is the worst failure mode available here.

Reads the payload from $PAYLOAD, writes the prompt to stdout. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

MAX_CONTEXT_CHARS = 6000

SYSTEM = {
    "fleet.github.notification": (
        "Summarise this GitHub notification for an operator who is scanning, not reading. "
        "Two or three sentences. Lead with what changed and whether it needs them. "
        "No preamble, no restating the question."
    ),
    "fleet.ops.alarm.repeated": (
        "Explain this repeated operational alarm to the engineer who owns the host. "
        "You are given an alarm that has fired several times and notes from their own "
        "dev vault. Say what is most likely happening, cite the note that supports it "
        "by title, and give the single next check worth running. Be concrete and brief. "
        "If the vault notes do not actually explain the alarm, say so plainly rather "
        "than forcing a connection."
    ),
}


def vault_block(context: list[dict]) -> str:
    """Format pre-fetched vault passages, budgeted by characters.

    The Worker already retrieved these, so the model does not have to search. The
    budget matters because vault notes run from a line to several thousand words:
    five passages is not a bounded amount of text.
    """
    if not context:
        return ""
    parts, used = [], 0
    for m in context:
        block = f"### {m.get('vault', '')}/{m.get('title', '')}\n{m.get('text', '')}".strip()
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return ""
    return (
        "\n\nRelevant notes from the operator's own dev vault (prefer these over "
        "general knowledge when they conflict):\n\n" + "\n\n".join(parts)
    )


def main() -> int:
    payload = json.loads(os.environ.get("PAYLOAD") or "{}")
    topic = payload.get("topic")
    data = payload.get("data") or {}

    system = SYSTEM.get(topic)
    if not system:
        print(f"unknown topic: {topic}", file=sys.stderr)
        return 2

    if topic == "fleet.ops.alarm.repeated":
        body = (
            f"Alarm: {data.get('title') or data.get('topic') or 'unknown'}\n"
            f"Fired {data.get('count', 'several')} times"
            + (f" in {data['windowMin']} minutes" if data.get("windowMin") else "")
            + ".\n"
        )
        if data.get("reason"):
            body += f"Reported reason: {data['reason']}\n"
        if data.get("samples"):
            body += "Recent occurrences:\n" + "\n".join(str(s) for s in data["samples"][:5]) + "\n"
    else:
        body = f"Repository: {data.get('repo', 'unknown')}\n"
        if data.get("title"):
            body += f"Title: {data['title']}\n"
        if data.get("n"):
            body += f"Number: #{data['n']}\n"
        if data.get("body"):
            body += f"Body:\n{str(data['body'])[:4000]}\n"

    print(system + "\n\n" + body + vault_block(data.get("vaultContext") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
