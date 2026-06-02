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
- **Log dashboard changes.** Append one line to `grafana/changelog.jsonl` for any
  dashboard add/remove/move/update (`{ts, pr, summary, changes:[{dashboard,
  action}]}`, oldest-first); `tests/test_changelog.py` validates it.
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
