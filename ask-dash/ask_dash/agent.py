"""Claude tool-use loop — used by the Discord bot and the scripted CLI.

(The MCP front-end does NOT use this: there, the caller's Claude CLI session is
the model and just calls the tools directly.)
"""

from __future__ import annotations

import anthropic

from . import config, tools

SYSTEM = """You answer questions about Tommy's home observability stack — Grafana
dashboards backed by InfluxDB (org "home") on tommys-mac-mini. You have
READ-ONLY tools; you cannot change anything.

How to work:
- Discover before you query: use list_buckets / list_measurements / list_fields /
  list_tag_keys / list_tag_values to learn the schema instead of guessing.
- Always bound Flux with a range() (e.g. range(start: -7d)); never scan all time.
- To answer "how does the dashboard show X", use search_dashboards then
  get_dashboard_queries and mirror the panel's own query.
- Answer concisely with concrete numbers and units, and name the bucket/measurement
  you used. If an entity isn't found, say so and list what IS available — never
  invent a value."""


def ask(question: str, max_steps: int = 8) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    tool_defs = tools.anthropic_tools()
    # Prompt-cache the (static) tool schemas — they're identical every call.
    tool_defs[-1]["cache_control"] = {"type": "ephemeral"}
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]

    messages: list[dict] = [{"role": "user", "content": question}]
    for _ in range(max_steps):
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            tools=tool_defs,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for b in resp.content:
            if b.type == "tool_use":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": tools.dispatch(b.name, b.input),
                })
        messages.append({"role": "user", "content": results})

    return "(Stopped after the step limit without a final answer.)"
