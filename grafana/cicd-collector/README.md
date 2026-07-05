# cicd-collector — GitHub Actions pipelines → InfluxDB `cicd`

Feeds the **CI/CD Pipelines** dashboard (`dev/cicd-pipelines.json`). Every poll
(5 min, launchd `com.tommy.cicd-collector`) it:

1. **Discovers** every non-archived repo in `GH_ORG` (`/orgs/{org}/repos`,
   paginated) plus `GH_EXTRA_REPOS` — so a new repo or workflow file appears on
   the dashboard within one cycle, with **zero config edits**. That discovery is
   the point: the dashboard's repo/workflow variables and pipeline-catalog panel
   are all driven by what this collector finds, not by a hand-kept list.
2. Writes a **`workflow_inventory`** point per workflow file (name, state,
   path) — the pipeline map, including pipelines that have never run.
3. Writes a **`workflow_run`** point per *completed* run in the poll window
   (tags `org/repo/workflow/branch/event/conclusion`; fields `ok`,
   `duration_s`, `queue_s`, `run_attempt`, `run_id`), timestamped at
   completion. Re-polling the same run overwrites the identical point, so the
   window overlaps (`OVERLAP_MIN`) instead of tracking per-run cursors; the
   first ever poll backfills `BACKFILL_DAYS` (default 30).
4. Writes a **`collector_poll`** heartbeat (repos/workflows seen, runs written,
   API calls, errors, GitHub rate-limit remaining) for self-monitoring.

API budget: `1 + 2×repos` calls per poll (repo list, then workflows + runs per
repo) — ~25 calls/5 min for the org today, far under the 5 000/h authenticated
limit. The heartbeat's `rate_remaining` field tracks the real headroom.

## Setup (maintainer, on the mini)

1. Create the bucket + a scoped write token (one-time):
   `docker exec influxdb influx bucket create -n cicd -o home` and an
   all-access-free token with write on `cicd` only.
2. `cp .env.example .env` (chmod 600) and fill `GH_TOKEN` (fine-grained PAT,
   **Metadata: read + Actions: read** on all org repos) and `INFLUX_TOKEN`.
3. `./deploy.sh` — copies `collector.py` + `.env` to
   `~/.local/share/cicd-collector/` (launchd can't read `/Volumes`, the usual
   TCC rule) and (re)loads the launchd job.

Smoke-test anywhere with outbound HTTPS: `python3 collector.py --dry-run`
(prints line protocol, writes nothing). Log:
`~/Library/Logs/cicd-collector.log`; cursor: `state.json` beside the deployed
copy (advances only after a successful write).

## Tests

`test_collector.py` is hermetic (pure-function mapping/window/pagination
shapes) and runs in CI via `grafana/run-tests.sh unit`.
