# drift-sentinel

> [!IMPORTANT]
> **Retired 2026-07-25 — not running, and `deploy.sh` refuses to reinstall it.**
>
> The sentinel reads InfluxDB, and the mini no longer runs InfluxDB or Grafana —
> both containers are absent from `docker ps -a` and nothing listens on `:8086`
> or `:3000`. The launchd job is unloaded; its last successful run wrote
> `state.json` on 2026-07-02 and every invocation since died with `HTTPError: 404`
> out of `flux_count()`.
>
> This is the mini-side decommission #156 calls for, not a port to Actions —
> a scheduled hosted run would have nothing to query. **The code below stays
> in-tree deliberately**: the commit-aware freshness check is worth re-emitting
> against Analytics Engine as part of that migration, the same way the parked
> collectors (#151/#152/#153) are held for re-emission. Everything below
> describes the design as built.
>
> Verified against `supervisor/.github/workflows/cf-shadow.yml` (a Worker shadow
> deploy — no drift detection) and `infra/.github/workflows/terraform-plan.yml`
> (the real Terraform lane). Neither overlaps this; despite the name, the sentinel
> has never had anything to do with Terraform drift.
> Refs robogeosociety/robot-geographical-society#175.

Catches **stale or degraded dashboard data** and posts a Discord warning enriched
with the **recent upstream commits** that likely caused it — the "why did this
dashboard stop?" investigation, automated.

It's driven by the `source:` provenance blocks in `grafana/dashboards.index.d/`:
for every dashboard whose data we produce (`status: active`, with a bucket), it
checks InfluxDB, and on trouble fetches the commits touching that pipeline's
`source.paths` and pings the same Discord channel the Grafana alerts use.

## Why a sidecar, not a Grafana alert

Grafana alert templates can render labels/annotations but **can't fetch GitHub at
fire time**. The "include the suspect commits" requirement is what makes this a
small collector rather than a Grafana rule. (Staleness alone *is* a standard
Grafana pattern — and we already have collector-freshness rules for the hot
buckets. This adds the cross-repo, commit-aware layer on top.)

## What it checks

Per tracked pipeline:
- **stale** — no points in `max(3 × cadence, 15m)` → the collector died.
- **degraded** — recent write-rate < `DEGRADED_RATIO` (default 0.4) of the
  trailing-24h baseline. **Only applied to continuous buckets** (cadence ≤ 120s);
  for daily/bursty buckets a rate ratio is noise, so staleness is the only signal.
- Buckets marked `source.monitor: skip` (usage/event-driven, e.g. `claude_code`)
  are excluded — idle ≠ broken.

Repeat alerts for the same pipeline+state are throttled (`REALERT_AFTER_S`, 6h).

## Run

```sh
cp .env.example .env && chmod 600 .env     # INFLUX_READ_TOKEN + DISCORD_WEBHOOK_URL
uv sync
uv run python sentinel.py --dry-run        # check + print, no Discord post
uv run python sentinel.py                   # real run (posts on drift)
uv run pytest                               # hermetic logic tests
```

The read-only InfluxDB token can be the same one `ask-dash` uses; the webhook is
the one in `grafana/.env`.

## Schedule (launchd, every 10 min)

```sh
./deploy.sh
```

`deploy.sh` copies `sentinel.py` + `.env` + the venv **and a snapshot of the
index** to `~/.local/share/drift-sentinel/`, then loads `com.tommy.drift-sentinel`.
Everything the scheduled run reads is on the internal disk, so launchd never
touches `/Volumes` (the TCC gotcha) — re-run `deploy.sh` whenever the sentinel or
the index changes.

## Extending

- Fill `source.repo` for the `status: external` dashboards (transit, campsites-AE,
  tempest, HA-Pi, obsidian) to bring them under watch.
- A future **source-drift** mode (compare each pipeline's upstream HEAD SHA to a
  committed lock) would catch a schema-changing deploy *before* the data breaks —
  the leading-indicator half.
