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
| `worker.sh` | drains the queue, deploys `main` from the internal clone, **verifies Grafana health, rolls back on failure**. Runs under launchd. |
| `install.sh` | clones the repo to the internal disk and loads the launchd job. |
| `com.tommy.observability-coordinator.plist` | launchd template (2-min interval). |
| `enable-merge-queue.sh` | one-shot: enable the GitHub merge queue + required `hermetic` check on `main`. |

## Runtime layout (internal disk — launchd can't touch `/Volumes`)

```
~/.observability/coordinator/
  repo/        # git clone of main; the worker hard-resets it each run (never hand-edit)
  queue/       processing/  done/  failed/
  LOCK/        # mkdir mutex (present = held; owner file = "<pid> <epoch>")
  env          # optional: INFLUX_OPS_TOKEN etc. for the heartbeat (chmod 600)
~/.observability/grafana/provisioning      # what Grafana mounts (rsync target)
~/.observability/grafana/provisioning.prev # rollback snapshot
```

The worker deploys **`main` as cloned internally**, not the `/Volumes` working tree —
so a scheduled deploy only ever ships merged, reviewed config. To preview an
*uncommitted* local edit, run `../grafana/deploy-provisioning.sh` (it takes the same
mutex, so it can't race the scheduler).

## Setup (run once, after this lands on `main`)

```sh
./install.sh                       # internal clone + launchd job
./enable-merge-queue.sh            # dry-run: review the ruleset
./enable-merge-queue.sh --apply    # enable merge queue on main (governance change)
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

## Tests

`test_coordination.py` is hermetic (a tmp `COORD_HOME`, `COORD_DEPLOY=0` to skip
git/docker): it exercises queue atomicity, the mutex's mutual exclusion + stale reclaim,
and the worker's queue→done lifecycle. Runs in the unit tier / CI.
