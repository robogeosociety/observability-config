# Backups

The backup system of record for the mini: every backup target emits a **heartbeat to
InfluxDB** (bucket `ops`, measurement `backup`) after each run, and Grafana both graphs
those heartbeats (the *Backups* dashboard) and **pages when one goes stale**. A backup
that stops running is an alert, never a silent gap.

Two heartbeating targets today:

| `target` | What | Schedule | Runner | Code |
| --- | --- | --- | --- | --- |
| `local` | InfluxDB daily dump → external disk (30-day retention) | 03:30 daily | LaunchAgent `dev.tommydoerr.influxdb-backup` | [`influxdb/backup.sh`](../influxdb/backup.sh) (this repo) |
| `r2-dev` | Full `/Volumes/dev` disk → Cloudflare R2, encrypted **restic** snapshots | 02:30 daily | Nomad periodic `dev-backup@backup` | `/Volumes/dev/backups/dev-backup.sh` (mini-local, not in a repo) |

(The Obsidian vault has its own backup jobs in the same Nomad `backup` namespace —
those belong to `obsidian-automations` and don't use this heartbeat contract.)

## The heartbeat contract

Line protocol, written to `ops` with the `INFLUX_OPS_TOKEN` from each job's `.env`:

```
backup,target=<local|r2-dev> success=<0|1>i,bytes=<n>i,duration_s=<n>i
```

Rules every target follows:

- **Exactly one heartbeat per run**, success or failure. An EXIT trap in each script
  guarantees a `success=0` on *any* failure exit — including a missing `.env` before it
  is sourced.
- **SIGKILL visibility** (`r2-dev` only, added 2026-07-17): traps can't catch SIGKILL,
  so the script also emits `backup_phase,target=r2-dev,phase=start value=1i` up front.
  A `start` with no matching success/failure heartbeat that night is the OOM-kill
  signature — visible post-hoc in the data even though the process died uncatchably.
- The staleness alerts (below) treat **absence of a success as the alert condition** —
  `noDataState: Alerting` — so a job that stops being scheduled at all still pages.

## The dev-disk restic backup (`target=r2-dev`)

Nightly encrypted, deduplicated, compressed **restic** snapshot of the whole 2 TB dev
disk to the `dev-backup` R2 bucket (`restic/` prefix). Replaced an rclone `sync` design
on 2026-07-17 after repeated memory-pressure kills — full incident and migration record
in [robot-geographical-society#168](https://github.com/robogeosociety/robot-geographical-society/issues/168).
Why restic survives where rclone didn't: rclone's `sync --fast-list` held a full
source+remote listing in RAM (multi-GB on this tree, fatal on the 8 GB mini), while
restic walks incrementally against a local on-disk cache — observed RSS stays
~450 MB. First snapshot: 397,891 files / 35.4 GiB processed, 14.3 GiB stored after
dedup+compression, 4m22s.

| Piece | Where / value |
| --- | --- |
| Script | `/Volumes/dev/backups/dev-backup.sh` (rclone predecessor kept as `dev-backup.sh.bak-rclone-20260717`) |
| Nomad job | `dev-backup`, namespace `backup` — periodic `30 2 * * *` America/Los_Angeles, `prohibit_overlap`, `raw_exec` → `/bin/zsh` (nomad agent + `/bin/zsh` hold Full Disk Access for `/Volumes`) |
| Repo | `s3:${R2_ENDPOINT}/dev-backup/restic` — R2 S3 key pair vended by `cloudflare-tfvend` (resource `dev_backup_r2`) |
| Encryption | `RESTIC_PASSWORD` in `/Volumes/dev/backups/.env` (chmod 600). **It exists nowhere else — losing it loses the backup.** |
| Cache | `/Volumes/dev/backups/.restic-cache` — deliberately on the external disk; the internal disk is space-tight |
| Excludes | `data/wikipedia` (re-downloadable mirror), `node_modules`, `.venv`, `target`, `dist`, `.next`, `__pycache__`, `.pytest_cache`, `*.pyc`, `.DS_Store`, volume metadata dirs. `.git` **is** kept (protects unpushed work). |
| Retention | `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6` nightly; `--prune` Sundays only (it's the expensive op). Best-effort *after* the success heartbeat — a retention hiccup never marks a good backup failed. |
| Success ping | Green embed to Discord (`DISCORD_WEBHOOK_URL_BACKUPS` → `DISCORD_WEBHOOK_URL` fallback) with size + duration |

### Operating it

All commands run **on the mini**, with the repo env loaded first:

```sh
cd /Volumes/dev/backups
set -a; . ./.env; set +a
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
       RESTIC_REPOSITORY="s3:${R2_ENDPOINT}/${R2_BUCKET}/restic" \
       RESTIC_CACHE_DIR=/Volumes/dev/backups/.restic-cache
```

| Task | Command |
| --- | --- |
| List snapshots | `restic snapshots` |
| Run a backup now | `nomad job periodic force -namespace backup dev-backup` |
| Verify integrity | `restic check --read-data-subset=10%` |
| Restore a path | `restic restore latest --target /tmp/restore --include /Volumes/dev/<path>` |
| Browse a snapshot | `restic ls latest /Volumes/dev/<path>` |
| Clear a stale lock | `restic unlock` (only after checking the holder PID is dead) |
| Rotate the R2 keys | `(cd /Volumes/dev/cloudflare-tfvend && terraform apply -replace=cloudflare_account_token.dev_backup_r2)`, then refresh `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` in `.env` |

Example (measured 2026-07-17):

```
$ restic snapshots
58a11812  2026-07-17 21:10:34  Tommys-Mac-mini.local   /Volumes/dev  35.363 GiB
$ restic check --read-data-subset=10%
no errors were found
```

## The InfluxDB dump (`target=local`)

Daily `influx backup` of the whole engine, streamed out of the container to
`/Volumes/dev/observability/influxdb/backups/` (30-day retention). Runs from a
LaunchAgent, which is TCC-gated from `/Volumes` — `/bin/zsh` needs Full Disk Access.
The dump directory is itself included in the `r2-dev` restic snapshot, which is its
off-site copy. See the header of [`influxdb/backup.sh`](../influxdb/backup.sh) for
details.

## Staleness alerts

Provisioned in [`grafana/provisioning/alerting/backup-stale.yml`](../grafana/provisioning/alerting/backup-stale.yml)
— one rule per target, same shape:

| Rule | Fires when |
| --- | --- |
| `backup-stale` | no `target=local` success in **26 h** |
| `backup-stale-r2-dev` | no `target=r2-dev` success in **26 h** |

- **`noDataState: Alerting`** — an empty window *is* the alert condition; a job that
  silently stops scheduling still pages. (The `r2-dev` rule was added after a 90-day
  silent outage where nothing watched the target.)
- **`execErrState: OK`** — if InfluxDB itself is unreachable the query errors, but
  that's the `influxdb-down` availability rule's page; these rules don't double-page.
- 26 h window + 10 min evaluation = a missed nightly run pages the next afternoon.

To mute one temporarily (e.g. a deliberate pause), prefer an **Alertmanager silence**
matched on `alertname` over editing the rule — it's instant, reversible, and survives
provisioning redeploys.
