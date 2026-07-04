# CICD.md — the pilot-light CI/CD for observability-config

This repo is the **pilot** for CI-gated continuous deployment across the fleet: a small,
always-on ignition that proves the pattern, then templates to any repo with a pull-deployer.
Two halves, built to be adopted independently.

```
┌ CI (what's tested) ─────────────────────────────────────────────────────────┐
│  Tier 1  hermetic        GitHub-hosted (test.yml)          every push / PR    │
│  Tier 2  integration     self-hosted mini runner           integration.yml    │  ← NEW
│          (live stack)    (dormant until activated)                            │
│  Tier 3  e2e/Playwright  maintainer-local (RAM)            run-tests.sh e2e    │
└──────────────────────────────────────────────────────────────────────────────┘
┌ CD (what deploys) ──────────────────────────────────────────────────────────┐
│  coordinator worker.sh — poll-deploys origin/main, now CI-GATED (ci_gate.sh)  │  ← NEW
└──────────────────────────────────────────────────────────────────────────────┘
```

## Why a pilot light and not a big-bang pipeline

The mini is tailnet-only and RAM-tight (8 GB, already shared by qwen + Grafana + InfluxDB +
the obsidian-supervisor). GitHub-hosted runners can't reach the live stack; a heavy
runner-driven build/deploy would fight the model for RAM. So the pilot keeps the **existing,
working pull-deploy coordinator** and adds only the two missing pieces — a CI gate on the
deploy, and a live-stack CI tier — each small and reversible.

---

## Part 1 — CI-gated CD (no runner, no RAM)

**Problem.** The coordinator (`coordination/worker.sh`, launchd every ~2 min) *poll-deploys*:
the moment `origin/main` advances it rsyncs provisioning → restarts Grafana → health-checks →
rolls back. It never asked whether **CI passed** on that SHA. And branch protection can't gate
merges — private repo on the free plan (`Upgrade to GitHub Pro`). So a red-CI merge went live
within one tick.

**Fix.** `coordination/ci_gate.sh <sha>` echoes `deploy` / `skip:ci-failed` / `skip:ci-pending`
from the `tests` workflow's conclusion on that exact SHA. `worker.sh` calls it before touching
the live stack:

- **green** → deploy as before.
- **failed** → withhold; leave `last-deployed-sha` unchanged; the last good config stays live.
- **pending** → defer to a later tick (CI usually resolves in a minute).
- **gate broken** (no `gh`, query error) → **degrade OPEN** (deploy) — a broken gate must never
  freeze deploys; it only ever *withholds* on an affirmative not-green signal.

A withheld SHA is filed under `~/.observability/coordinator/skipped/` (a deferral, not a
failure) and re-evaluated each tick, so it deploys the instant CI goes green and is superseded
by any newer green SHA. **Break-glass:** `COORD_REQUIRE_CI=0` skips the gate for a manual deploy.

**Auth (one setup step).** `gh` uses the host OAuth token (holy-trinity — never a minted PAT).
Under launchd the keychain may be locked, so drop a **scoped read-only** token in the coordinator
env so `gh api` resolves — the same documented unattended-token pattern as `INFLUX_OPS_TOKEN`:

```sh
# ~/.observability/coordinator/env   (chmod 600, already sourced by worker.sh)
GH_TOKEN=<fine-grained token · Actions:read + Contents:read on observability-config, read-only>
```

Without it (and with a locked keychain) the gate degrades open — deploys keep working, just
un-gated, and the log says so.

---

## Part 2 — live-stack CI (the self-hosted runner)

**Problem.** Tier-2 integration tests (`-m integration`) contact the live Grafana/InfluxDB on
the box, so they never ran in CI — only locally. A GitHub-hosted runner can't reach the tailnet
stack.

**Fix.** `.github/workflows/integration.yml` runs `grafana/run-tests.sh integration` on a
**bare-metal macOS runner on the mini** (`runs-on: [self-hosted, macos, arm64, mini]`). It's
**dormant until activated** — every job is gated on the repo variable `SELF_HOSTED_RUNNER ==
'ready'`, so merging this file never leaves jobs stuck "waiting for a runner".

### Registering the runner — one ORG-level runner, reusing PR #144's installer (do NOT write a second one)

observability-config was transferred into the **`robogeosociety` org** (2026-07-04), which
**resolves the "stand up an org first?" open question** in
[#143](https://github.com/robogeosociety/observability-config/pull/143) /
[#144](https://github.com/robogeosociety/observability-config/pull/144). #144's
`macos-runner/setup.sh` already supports **org mode** (`GH_ORG`), so the clean fit is now **one
org-level runner** — a single bare-metal host runner that serves *every* repo in the org
(observability-config, tommybot, …) — not a per-repo instance each (which would multiply idle
RAM on the 8 GB box). After #144 merges:

```sh
cd macos-runner
cp .env.example .env
# ORG-level profile — ONE runner for the whole robogeosociety org:
#   GH_ORG=robogeosociety
#   RUNNER_DIR=~/actions-runner-mini
#   RUNNER_NAME=mini-macos
#   RUNNER_LABELS=self-hosted,macos,arm64,metal,mini   # superset: `metal` for tommybot's MLX
#                                                        # gates, host access for this repo's
#                                                        # live-stack tests — integration.yml's
#                                                        # [self-hosted,macos,arm64,mini] matches
./setup.sh   # registers at the org
gh variable set SELF_HOSTED_RUNNER --repo robogeosociety/observability-config --body ready
```

> **Token — recommended improvement to #144:** its `setup.sh` mints the registration token from
> a fine-grained **PAT** (`RUNNER_PAT`). Prefer `gh api -X POST
> orgs/{org}/actions/runners/registration-token --jq .token` (host OAuth, no PAT) — proposed on
> #144. Until that lands, use a short-lived fine-grained PAT with **org** Administration /
> manage-runners; it's consumed once at registration and never stored in the runner.

**Park it** during model-heavy windows (protect the 8 GB box):
`gh variable delete SELF_HOSTED_RUNNER --repo robogeosociety/observability-config`.

### Triggers & RAM discipline

- `push: main` → post-merge live-stack smoke (pairs with the coordinator deploy).
- `pull_request` → opt-in per PR via the **`ci-integration`** label, so not every PR spends RAM.
- `workflow_dispatch` → manual.
- **e2e/Playwright stays maintainer-local** (`run-tests.sh e2e`) — Chromium is the heavy tenant;
  it is deliberately *not* on the runner.

---

## Activation order (and where #142 fits)

1. **Merge this PR.** The CI-gate self-propagates — the coordinator hard-resets its internal
   clone to `main` each tick, so the next deploy runs the gated `worker.sh`. `integration.yml`
   lands dormant. *(The first deploy after merge runs the old worker once, then the gated one.)*
2. **Add `GH_TOKEN`** to the coordinator env (Part 1) so the gate resolves under launchd.
3. **Merge [#144](https://github.com/robogeosociety/observability-config/pull/144)**, register the
   observability-config runner, `gh variable set SELF_HOSTED_RUNNER ready` (Part 2).
4. **[#142](https://github.com/robogeosociety/observability-config/pull/142) is the first gated
   deploy** — its `tests` check is already green, so once the gate is live it deploys through
   the new path: the pilot's first real payload.

## Related / coordinate

- [#144](https://github.com/robogeosociety/observability-config/pull/144) — bare-metal macOS runner
  (this reuses its installer; do not duplicate).
- [#143](https://github.com/robogeosociety/observability-config/pull/143) — Linux/OrbStack runner for
  cheap portable gates (lint/unit/build). Orthogonal: the hermetic tier already runs free on
  GitHub-hosted, so observability-config's own CI doesn't need it — but it's the right home for
  heavier portable builds if the fleet wants self-hosted Tier-1.
- `COORDINATION-PLAN.md` / `coordination/README.md` — the deploy serializer this gate rides on.
