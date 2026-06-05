# AGENTS.md — conventions for agentic changes to observability-config

This repo is the **live configuration** for the home observability stack
(Grafana + InfluxDB + Telegraf on `tommys-mac-mini`). If you are an AI agent
making a change here, follow the conventions below — they are not style
preferences; they prevent outages, silent data loss, and reverted work.

`CLAUDE.md` is the architecture + gotchas reference. **This file is the rules for
*changing* things and opening PRs.** Read both.

## Your job: open a PR. That's it.

You **edit files and open a pull request — and stop there.** A human reviews and
merges; the repo's automation applies the change. Specifically, you do **NOT**:

- **merge** PRs (`gh pr merge`) — a human merges;
- **restart, recreate, or `docker`** anything (Grafana, InfluxDB, the renderer);
- run any **`deploy.sh` / `deploy-provisioning.sh`**, or otherwise sync to the
  internal-disk deploy targets;
- **mutate the live stack** directly — no Grafana API writes/deletes, no `influx`
  writes, no `launchctl load/unload`, no editing `~/.observability/...` or
  `~/.local/share/...`.

**The repo applies changes itself.** Once a human merges to `main`, the launchd
**coordinator** (`com.tommy.observability-coordinator`, every ~120s) deploys it:
`git reset --hard origin/main` → rsync provisioning to the internal disk →
restart Grafana → health-check with automatic rollback. You don't deploy; you
don't restart; you don't preview against the live stack. **Your verification is
the hermetic test suite, not a running deploy.**

## How to make a change

1. **Branch off `main`; one PR per change.** Never push stack changes straight to
   `main` — the coordinator deploys whatever lands there within ~2 minutes.
2. Edit the **repo source** (never the internal-disk copies, never the Grafana UI
   — `allowUiUpdates: false` makes provisioned dashboards read-only there anyway).
3. **Run the hermetic tests** — they gate CI and are your verification:
   `grafana/run-tests.sh unit` (or pytest the relevant tier; see
   `grafana/TESTING.md`). They must pass. The integration/e2e tiers need the live
   stack and are the maintainer's to run — don't try to stand the stack up.
4. **Open the PR.** Body = what changed, why, and how you verified (which tests).
   Keep secrets out of the diff. Then stop — do not merge, deploy, or restart.

   **PR descriptions follow the "newspaper" framework** — one self-contained front
   page (kicker → headline → dek → masthead → why → what → mermaid flow → screens →
   verification → risk) that reads top-to-bottom on an iPad-mini portrait display
   (1–2 pages; up to 4 for very complex *code* changes). Rebuild it from the **full**
   diff, never append. Skeleton: `.github/pull_request_template.md`; full rules:
   <https://github.com/tommyroar/.github/blob/main/PR_FRAMEWORK.md>. The
   `pr-newspaper` workflow validates the body in CI (readability + page budget).

## Conventions your change must follow

These exist because the maintainer/coordinator — not you — applies the change, so
the files must be correct as-merged:

- **Store-as-code.** Dashboards, datasources, alerting, and Telegraf are
  file-provisioned. Edit the files; the UI is read-only.
- **Dashboard ↔ index parity.** Edit JSON in `grafana/provisioning/dashboards/`
  (under the right domain subdir — `infra/ ops/ campsites/ transit/ weather/ dev/`,
  one Grafana folder per subdir) and update `grafana/dashboards.index.yaml` in the
  *same* PR — `tests/test_dashboard_index.py` enforces title / folder-relative
  `file` / datasource parity and fails on orphans.
- **The changelog is derived from git — don't hand-maintain a file.** There is no
  `changelog.jsonl` to append to (it was a shared-tail conflict funnel). Git history
  *is* the append-only, ordered, conflict-free log; just write a **clear commit
  subject** describing the dashboard change. Render the human view on demand with
  `python3 grafana/render-changelog.py` (groups commits touching
  `provisioning/dashboards/**` by date, newest first).
- **Declare data provenance.** Every dashboard entry carries a `source:` block
  (repo / paths / produces / cadence / status) so drift is detectable. Add one for
  any new dashboard; mark usage/event-driven buckets `monitor: skip` (idle ≠
  broken). The drift-sentinel and collector-freshness alerts read it.
- **Alerting is code.** Rules/contacts/policies live in
  `grafana/provisioning/alerting/`, routed to the `discord` contact point. A new
  rule should set `execErrState: OK` (and usually `noDataState: OK`) unless it is
  the primary InfluxDB-down detector — so an InfluxDB outage pages **once**, not
  once per rule.
- **Secrets** live in per-dir `.env` (gitignored, `chmod 600`). Commit only
  `.env.example`. Never put a real token, webhook, or password in a tracked file
  or a PR diff.

## Dashboard validation & regression path

A dashboard can pass JSON validation and still render **No data** — a query bound
to a fixed window the picker never covers, a variable defaulting to a dataless
value, an unsupported datasource function. We catch that class in layers; when you
add or change a dashboard, your change is verified against them (the first layer
gates CI):

1. **Static guards — hermetic, run in CI** (`grafana/tests/test_dashboards_static.py`,
   the nodata guards repo-wide as of #67). Time-axis (`timeseries`/`trend`) panels
   must follow the time picker (ClickHouse `$timeFilter`/`$timeSeries`, Flux
   `v.timeRangeStart`) and never hardcode their own window; ClickHouse targets using
   the time macros must declare `dateTimeColDataType`; a single-value query variable
   feeding an `== "${var}"` filter must be data-scoped (so it can't default to a
   dataless edge value). **Parametrized over the dashboard dir glob**, so *any* new
   dashboard is covered automatically — no test edit needed. A legitimately
   fixed-window time-axis panel goes in the `TIME_AXIS_EXCEPTIONS` allowlist; encode
   the exception, don't weaken the rule.
2. **Integration — live stack, maintainer-run** (`test_dashboards_integration.py`,
   repo-wide as of #67; marked `integration`, self-skips if the stack is down).
   Resolves each dashboard's variables to Grafana's defaults, interpolates, and runs
   every panel over the default range **and** `now-24h`, failing any that returns No
   data. It's **health-aware**: a query error on a *healthy* datasource is a failure
   (catches unsupported SQL like AE's missing `uniqExactIf`/`uniq`, or a Flux type
   conflict), not a skip. A legitimately-empty panel (event-driven, weather, not-yet-
   configured) goes in `tests/dashboard_coverage.yaml`, keyed by dashboard uid —
   no entry ⇒ strict. `allow_empty` excuses an *empty* result only; a real query
   error still fails.
3. **Visual — local Playwright** (`grafana/playwright/{campsites,dashboards}-visual.spec.ts`).
   Renders each panel (canvas mounted + no "No data"), loose committed baselines over
   a relative window. Driven by a `PANELS` manifest — cover a new dashboard by adding
   rows. For ad-hoc spot-checks, the Grafana MCP `get_panel_image` renders a single
   panel to PNG (the renderer sidecar).

Preferred order when something looks empty: **data layer first** (run the pytest
tiers / `/api/ds/query`), then **canvas layer** (visual baseline or
`get_panel_image`) — most "nodata" is a query/variable bug the data layer pins
down exactly. CI runs layer 1 over all dashboards on every push/PR; layers 2–3
need the live stack (`grafana/run-tests.sh integration|e2e`) and are the
maintainer's to run. Adding a panel with a new visualization? Add it to the visual
spec in the same PR.

**Attach a full-page screenshot — required.** Any PR that adds or changes a
dashboard **must** include a full-page render of the affected dashboard(s) in the
PR body. The reviewer should see the rendered result, not just JSON — this is part
of the change, not optional polish. Steps:

1. Generate it with `grafana/playwright/screenshot-dashboard.sh <uid> '<pinned-vars>'`
   — renders the whole dashboard via the renderer sidecar (kiosk mode) to
   `grafana/playwright/pr-screenshots/<uid>.png`. **Pin dashboard variables to a
   date/selection that has data** so the shot isn't a misleading empty render.
2. Commit the PNG (the `pr-screenshots/` dir is tracked).
3. Embed it in the PR body with the branch raw URL:
   `![<uid>](https://github.com/<owner>/<repo>/blob/<branch>/grafana/playwright/pr-screenshots/<uid>.png?raw=true)`.
   This **renders inline** for repo members on this private repo (verified) — no
   drag-drop or external upload needed.

## Context to write correct config (the maintainer applies it, not you)

- **`/Volumes` + launchd = TCC block.** Any launchd job that reads `/Volumes`
  fails. Host collectors run from the internal disk via a `deploy.sh` (the
  maintainer runs it on merge), or require Full Disk Access on the invoking binary
  (the InfluxDB backup's `/bin/zsh`). Don't point a new launchd job at a
  `/Volumes` path.
- **Datasource/compose changes need a container recreate** on apply, not just a
  provider reload — note it in the PR so the maintainer recreates. And **removing a
  datasource from YAML does not delete it from Grafana**; call that out so it's
  deleted explicitly.
- **A merged change is what goes live**, deployed from `origin/main`. There is no
  "preview that sticks" — correctness must be in the committed files, verified by
  tests, because the coordinator reconciles the live stack to `main`.

For deeper architecture and gotchas, see `CLAUDE.md`; coordination internals are
in `coordination/README.md` and `COORDINATION-PLAN.md`.
