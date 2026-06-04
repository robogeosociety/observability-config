<!--
  This repo's PRs follow the "newspaper / information-pyramid" framework:
  one self-contained front page that reads top-to-bottom on an iPad-mini portrait
  display (1–2 pages; up to 4 for very complex *code* changes). Most newsworthy
  facts first, supporting detail below, fine print last.

  Full rules + voice: ~/.claude/pr-framework/PR_FRAMEWORK.md
  CI runs .github/scripts/validate_pr.py on this body (readability + page budget) —
  no model in the loop. An agent that opens/updates this PR regenerates the body
  from the *full* diff; it is rebuilt from scratch, never appended to.

  Fill the panel below and DELETE any section that is genuinely empty (don't leave
  empty headers, and don't pad to fill the budget).
-->

`<AREA>` — **<RUBRIC>**

# <Evocative but accurate headline — a real title, not "Update X">

> _<Dek: 1–2 italic sentences setting the stakes. Stands alone as the lede.>_

> [!NOTE]
> **<area>** · feat · risk: low · closes #000

<Narrative lede: a short paragraph that hooks the reader and states the problem / why.>

## <Punchy subhead for the core idea>
- <Grouped, scannable bullets, one idea each.>

> _"<Pull quote — the single line that captures the point.>"_

## How it flows
```mermaid
flowchart TD
  A[Start] --> B{New gate}
  B -->|ok| C[Result]
  B -->|blocked| D[Error]
```
<!-- Omit only if no flow changed — and say so explicitly. Orient TD (top-down), not LR. -->

## Screens & wireframes
<!-- Every available image/wireframe/mockup, width-bounded, with alt text.
     Private repo: link committed figures by blob URL (raw URLs don't render). -->

## Verification
- <Commands run / tests / manual checks, with outcomes.>

## Risk & rollout
- <Migrations, feature flags, rollback plan, follow-ups.>
