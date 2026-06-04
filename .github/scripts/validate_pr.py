#!/usr/bin/env python3
"""
validate_pr.py — readability + iPad-mini length validator for PR descriptions.

Enforces the "Newspaper / Information-Pyramid" PR framework (see PR_FRAMEWORK.md):
a single descriptive panel at a comfortable density. Default budget is 2 iPad-mini
pages; very complex *code* changes may use up to 4 (the caller passes --max-pages).

Usage:
    python3 validate_pr.py path/to/pr-body.md [--max-pages N]
    cat pr-body.md | python3 validate_pr.py - [--max-pages N]
    PR_NEWSPAPER_MAX_PAGES=4 python3 validate_pr.py body.md   # env also works

Exit code 0 = PASS (possibly with warnings); 1 = FAIL (over budget / hard errors).
"""

import math
import os
import re
import sys

# --- Rendered-height model (iPad mini portrait, GitHub markdown column) ----------
PAGE_PX = 1000          # usable content height per "page"
COL_CHARS = 80          # approx characters per line in the content column
LINE_PX = 24            # body line height
H_PX = {1: 64, 2: 48, 3: 38, 4: 32, 5: 32, 6: 32}
PARA_MARGIN = 16
IMG_PX = 320            # assumed rendered height per image
MERMAID_PX = 360        # assumed rendered height per diagram
TABLE_ROW_PX = 36
CODE_LINE_PX = 20
CODE_PAD = 32
WALL_OF_TEXT_LINES = 7  # a single paragraph taller than this warns

MERMAID_KEYWORDS = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "stateDiagram-v2", "erDiagram", "journey", "gantt", "pie", "mindmap",
    "timeline", "gitGraph", "quadrantChart", "requirementDiagram", "C4Context",
    "sankey-beta", "xychart-beta", "block-beta",
)

errors, warnings = [], []


def wrapped_lines(text):
    text = text.strip()
    if not text:
        return 0
    return max(1, math.ceil(len(text) / COL_CHARS))


def parse(md):
    """Return (blocks, h1s, headings) where blocks are (kind, px, meta)."""
    lines = md.split("\n")
    blocks, h1s, headings = [], [], []
    i, n = 0, len(lines)
    para = []

    def flush_para():
        if para:
            text = " ".join(p.strip() for p in para)
            nlines = wrapped_lines(text)
            blocks.append(("prose", nlines * LINE_PX + PARA_MARGIN,
                           {"lines": nlines, "text": text}))
            para.clear()

    while i < n:
        line = lines[i]
        fence = re.match(r"^\s*```(\w*)", line)
        if fence:
            flush_para()
            lang = fence.group(1).lower()
            body, i = [], i + 1
            while i < n and not re.match(r"^\s*```", lines[i]):
                body.append(lines[i]); i += 1
            i += 1  # closing fence
            if lang == "mermaid":
                first = next((b.strip() for b in body if b.strip()), "")
                ok = first.startswith(MERMAID_KEYWORDS)
                blocks.append(("mermaid", MERMAID_PX, {"first": first, "ok": ok}))
            else:
                long_line = max((len(b) for b in body), default=0)
                blocks.append(("code", len(body) * CODE_LINE_PX + CODE_PAD,
                               {"long": long_line, "lang": lang}))
            continue

        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            flush_para()
            level = len(h.group(1))
            headings.append((level, h.group(2).strip(), len(blocks)))
            if level == 1:
                h1s.append(len(blocks))
            blocks.append(("heading", H_PX[level], {"level": level,
                                                     "text": h.group(2).strip()}))
            i += 1
            continue

        # table: a line with >=2 pipes followed by a |---| separator
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:\-|]+\|", lines[i + 1]):
            flush_para()
            rows = 0
            while i < n and "|" in lines[i]:
                rows += 1; i += 1
            blocks.append(("table", rows * TABLE_ROW_PX + 8, {"rows": rows}))
            continue

        # standalone image (markdown or html)
        md_img = re.search(r"!\[(.*?)\]\((.*?)\)", line)
        html_img = re.search(r"<img\b([^>]*)>", line, re.I)
        if md_img or html_img:
            flush_para()
            if md_img:
                alt = md_img.group(1).strip()
                blocks.append(("image", IMG_PX,
                               {"alt": alt, "width": True, "html": False}))
            else:
                attrs = html_img.group(1)
                alt_m = re.search(r'alt\s*=\s*"([^"]*)"', attrs, re.I)
                alt = alt_m.group(1).strip() if alt_m else ""
                width = bool(re.search(r'width\s*=', attrs, re.I))
                blocks.append(("image", IMG_PX,
                               {"alt": alt, "width": width, "html": True}))
            i += 1
            continue

        if line.strip() == "":
            flush_para()
            i += 1
            continue

        # blockquotes / list items / plain text all accrue as prose lines
        if re.match(r"^\s*([>\-*+]|\d+\.)\s", line):
            flush_para()
            content = re.sub(r"^\s*([>\-*+]|\d+\.)\s", "", line)
            nlines = wrapped_lines(content)
            blocks.append(("prose", nlines * LINE_PX + 4,
                           {"lines": nlines, "text": line.strip()}))
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks, h1s, headings


def has_panel(md):
    # The panel/lede may be an explicit TL;DR, or a Wired-style "dek" (standfirst): an
    # emphasized blockquote that isn't a GitHub [!ALERT] masthead.
    if re.search(r"^\s*>\s*\*\*TL;DR\*\*", md, re.M | re.I):
        return True
    if re.search(r"^\s*>.*tl;dr", md, re.M | re.I):
        return True
    for m in re.finditer(r"^\s*>\s*(?!\[!)(.+)$", md, re.M):
        if re.search(r"(\*\*|__|_[^_]|\*[^*])", m.group(1)):  # bold or italic = a dek
            return True
    return False


def has_masthead(md):
    return bool(re.search(r"^\s*>\s*\[!(NOTE|TIP|IMPORTANT)\]", md, re.M))


def main():
    # Args: a file (or '-' for stdin), optional --max-pages N (env fallback, default 2).
    max_pages = int(os.environ.get("PR_NEWSPAPER_MAX_PAGES", "2"))
    src = None
    argv, i = sys.argv[1:], 0
    while i < len(argv):
        if argv[i] == "--max-pages" and i + 1 < len(argv):
            max_pages = int(argv[i + 1]); i += 2; continue
        src = argv[i]; i += 1
    if not src:
        print("usage: validate_pr.py <file|-> [--max-pages N]"); return 2
    md = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()

    blocks, h1s, headings = parse(md)

    # --- structural checks ---
    if len(h1s) == 0:
        errors.append("No `#` headline — the panel needs a single H1 outcome line.")
    elif len(h1s) > 1:
        errors.append(f"{len(h1s)} H1 headings — a PR panel has exactly one headline.")
    elif h1s[0] != 0:
        # A single short "kicker"/rubric line above the headline is magazine style, OK.
        kicker_ok = (h1s[0] == 1 and blocks[0][0] == "prose"
                     and len(blocks[0][2].get("text", "")) <= 48)
        if not kicker_ok:
            warnings.append("H1 should lead (a single short kicker line above it is fine).")

    if not has_panel(md):
        errors.append("No `> **TL;DR**` panel — add the single descriptive lede "
                      "blockquote under the headline.")
    if not has_masthead(md):
        warnings.append("No `> [!NOTE]` masthead strip (area · type · risk · closes #).")

    # heading-skip check
    prev = 0
    for level, text, _ in headings:
        if prev and level > prev + 1:
            errors.append(f"Heading skip: jumped to H{level} ('{text}') after H{prev}.")
        prev = level

    # media / readability checks
    n_img = n_mermaid = 0
    for kind, _px, meta in blocks:
        if kind == "image":
            n_img += 1
            if not meta["alt"]:
                errors.append("Image without alt text — every image needs alt text.")
            if meta["html"] and not meta["width"]:
                warnings.append("<img> without width= may overflow the narrow column; "
                                "add width=\"…\".")
        elif kind == "mermaid":
            n_mermaid += 1
            if not meta["ok"]:
                errors.append(f"Mermaid block doesn't start with a known diagram type "
                              f"(got '{meta['first'][:30]}').")
            elif re.match(r"(flowchart|graph)\s+(LR|RL)\b", meta["first"]):
                warnings.append("Horizontal mermaid (LR/RL) overflows the narrow iPad-mini "
                                "column — orient it vertically (`flowchart TD`).")
        elif kind == "code" and meta["long"] > COL_CHARS + 20:
            warnings.append(f"Code line {meta['long']} chars wide — risks horizontal "
                            f"scroll on iPad mini.")
        elif kind == "prose" and meta["lines"] > WALL_OF_TEXT_LINES:
            warnings.append(f"Wall of text ({meta['lines']} lines): break it up — "
                            f"\"{meta['text'][:40]}…\"")

    if n_mermaid == 0 and not re.search(r"no\s+flow\s+change", md, re.I):
        warnings.append("No mermaid diagram and no 'no flow change' note — model new "
                        "flows as diagrams (preferred).")
    if n_img == 0:
        warnings.append("No images/wireframes embedded — include all available visuals.")

    # --- length budget (tiered iPad-mini pages; default 2, complex code change up to 4) ---
    total_px = sum(px for _k, px, _m in blocks)
    prose_px = sum(px for k, px, _m in blocks if k in ("prose", "heading"))
    pages = max(1, math.ceil(total_px / PAGE_PX))

    if pages > max_pages:
        errors.append(
            f"~{pages} pages (~{total_px}px) exceeds the {max_pages}-page budget. "
            f"Trim to fit; the 4-page tier is only for very complex *code* changes "
            f"(pure text/docs churn doesn't qualify).")
    elif pages == max_pages and max_pages == 2:
        warnings.append(f"At the {max_pages}-page default — fine, but keep it dense, "
                        f"not padded.")

    # --- report ---
    print("PR description readability report")
    print("=" * 40)
    print(f"  budget           : {max_pages} iPad-mini page(s)")
    print(f"  estimated height : ~{total_px}px  (~{pages} page(s))")
    print(f"  prose-only       : ~{prose_px}px")
    print(f"  images           : {n_img}")
    print(f"  mermaid diagrams : {n_mermaid}")
    print(f"  headings         : {len(headings)}")
    print()
    for w in warnings:
        print(f"  ⚠  {w}")
    for e in errors:
        print(f"  ✗  {e}")
    print()
    if errors:
        print(f"FAIL — {len(errors)} error(s), {len(warnings)} warning(s). "
              f"Rebuild the description, don't append.")
        return 1
    print(f"PASS — {pages}/{max_pages} page(s), {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
