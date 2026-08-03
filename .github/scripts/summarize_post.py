#!/usr/bin/env python3
"""Hand the finished summary back to ops-summarizer, which posts and acks.

The Worker owns the Discord identity and the queue coordinates; this script owns
nothing but the text the model produced. That split is why the workflow needs no
webhook and no bus token.

  summarize_post.py            post the summary and ack the job
  summarize_post.py --failed   tell the queue this run failed

Stdlib only, and every network call is best-effort in the sense that it reports
rather than masks: a non-2xx exits non-zero so the run goes red, because a summary
that was produced and never delivered should not look like a success.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def post(url: str, token: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            # Cloudflare rejects urllib's default UA with error 1010 (blocked
            # browser signature). Observed on the first real run — the model had
            # already produced the summary, so the cost of this was a wasted
            # generation, not just a retry.
            "user-agent": "rgs-summarize/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def claude_output() -> tuple[str, dict]:
    """Read `claude -p --output-format json`.

    Tolerant by design: the CLI's envelope has changed shape before, and a summary
    that exists should not be lost to a key rename. Falls back to the raw file.
    """
    try:
        raw = open("/tmp/out.json").read()
    except OSError:
        return "", {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip(), {}

    text = d.get("result") or d.get("text") or d.get("content") or ""
    if isinstance(text, list):  # content-block form
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    usage = d.get("usage") or {}
    return str(text).strip(), {
        "inputTokens": usage.get("input_tokens"),
        "outputTokens": usage.get("output_tokens"),
        "turns": d.get("num_turns") or 1,
        # The CLI reports total turns, not tool calls; absent is honest.
        "toolCalls": d.get("num_tool_uses") or 0,
    }


def main() -> int:
    failed = "--failed" in sys.argv
    payload = json.loads(os.environ.get("PAYLOAD") or "{}")
    base = (os.environ.get("SUMMARIZER_URL") or "").rstrip("/")
    token = os.environ.get("HANDLER_TOKEN") or ""
    topic, job_id = payload.get("topic"), payload.get("jobId")

    if not base or not token:
        print("SUMMARIZER_URL or HANDLER_TOKEN unset", file=sys.stderr)
        return 2

    if failed:
        status, body = post(
            f"{base}/fail",
            token,
            {"topic": topic, "jobId": job_id, "reason": "workflow failed or was cancelled"},
        )
        print(f"fail-ack -> {status} {body}")
        return 0  # never mask the original failure with this one

    text, usage = claude_output()
    if not text:
        print("claude produced no text", file=sys.stderr)
        return 1

    data = payload.get("data") or {}
    ctx = data.get("vaultContext") or []
    status, body = post(
        f"{base}/post",
        token,
        {
            "topic": topic,
            "jobId": job_id,
            "data": {k: v for k, v in data.items() if k != "vaultContext"},
            "text": text,
            "telemetry": {
                "model": "haiku (max plan)",
                **usage,
                "maxTurns": 3,
                "ragHits": len(ctx),
                "hits": [
                    {"vault": m.get("vault"), "title": m.get("title"), "score": m.get("score", 0)}
                    for m in ctx
                ],
                "llmMs": int(os.environ.get("LLM_MS") or 0) or None,
            },
        },
    )
    print(f"post -> {status} {body}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
