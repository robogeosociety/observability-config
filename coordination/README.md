# coordination/

The deploy scheduler + queue that lets projects land dashboards concurrently
without racing the one live Grafana. Design rationale and the later phases are in
[`../COORDINATION-PLAN.md`](../COORDINATION-PLAN.md); this is the operator's view of
**Phase 0** (the deploy serializer) — what's implemented here.

## What's here

| file | role |
|---|---|
| `mutex.sh` | atomic `mkdir` lock (sourced). One holder across launchd ticks + manual deploys; reclaims stale locks (dead PID + age > TTL). |
| `enqueue.sh` | `enqueue.sh deploy "reason"` — atomically drop a job in the queue. |
| `worker.sh` | drains the queue, deploys `main` from the internal clone **only if its CI is green** (`ci_gate.sh`), **verifies Grafana health, rolls back on failure**. Runs under launchd. |
| `ci_gate.sh` | `ci_gate.sh <sha>` → `deploy` / `skip:ci-failed` / `skip:ci-pending` from the `tests` workflow's conclusion on that SHA — the deploy admission gate (a red-CI merge is *withheld*, not shipped). Degrades **open** if `gh` can't answer. See [`../CICD.md`](../CICD.md). |
| `install.sh` | clones the repo to the internal disk and loads the launchd job. |
| `com.tommy.observability-coordinator.plist` | launchd template (2-min interval). |

> Merges go through normal squash PRs gated by the required `hermetic` check — **no merge
> queue** (it needs GitHub Pro or a public repo, and merge ordering is low-stakes for a solo
> repo; the deploy serializer below covers the real collision risk). Branch protection can't
> *enforce* that check on this plan, so the coordinator enforces it at **deploy** time instead:
> `worker.sh` gates on `ci_gate.sh` and won't ship a SHA whose CI isn't green ([`../CICD.md`](../CICD.md)).

## Runtime layout (internal disk — launchd can't touch `/Volumes`)

```
~/.observability/coordinator/
  repo/        # git clone of main; the worker hard-resets it each run (never hand-edit)
  queue/       processing/  done/  failed/  skipped/   # skipped = CI-gated deferrals (not failures)
  LOCK/        # mkdir mutex (present = held; owner file = "<pid> <epoch>")
  env          # optional: INFLUX_OPS_TOKEN (heartbeat) + GH_TOKEN (CI gate, read-only) — chmod 600
~/.observability/grafana/provisioning      # what Grafana mounts (rsync target)
~/.observability/grafana/provisioning.prev # rollback snapshot
```

The worker deploys **`main` as cloned internally**, not the `/Volumes` working tree —
so a scheduled deploy only ever ships merged, reviewed config. To preview an
*uncommitted* local edit, run `../grafana/deploy-provisioning.sh` (it takes the same
mutex, so it can't race the scheduler).

## Setup (run once)

```sh
./install.sh    # internal clone + launchd job (com.tommy.observability-coordinator)
```

## Trigger a deploy

```sh
./enqueue.sh deploy "ship transit dashboard tweak"
# worker picks it up within ~2 min; or run it now:
~/.observability/coordinator/repo/coordination/worker.sh
```

A CI `post-merge` step (or `deploy-provisioning.sh`) can call `enqueue.sh deploy` so
every merge to `main` auto-deploys through the serialized path.

## Observability

- Log: `~/Library/Logs/observability-coordinator.log`.
- Heartbeat: with `INFLUX_OPS_TOKEN` in `~/.observability/coordinator/env`, each run writes
  `coordinator` (`success`/`jobs`/`duration_s`) to the `ops` bucket — same pattern as the
  backup job, so it can surface on an ops dashboard.
- A non-empty `failed/` means a deploy failed health-check and rolled back; the job file +
  log have the detail. Failures are recorded, not hot-retried — the queue head never blocks.
- A non-empty `skipped/` means a deploy was **withheld by the CI gate** (main advanced to a
  SHA whose `tests` run wasn't green) — a deferral, re-evaluated each tick, not a failure. The
  heartbeat carries `gated=1i` so it's distinguishable from a health-check rollback on a dashboard.

## Tests

`test_coordination.py` is hermetic (a tmp `COORD_HOME`, `COORD_DEPLOY=0` to skip
git/docker): it exercises queue atomicity, the mutex's mutual exclusion + stale reclaim,
and the worker's queue→done lifecycle. It also drives `ci_gate.sh` with a stubbed CI
source (`COORD_CI_CHECK_CMD`) to verify the deploy / withhold / degrade-open mapping.
Runs in the unit tier / CI.
