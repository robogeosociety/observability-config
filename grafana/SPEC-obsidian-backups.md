# SPEC — `obsidian-backups` dashboard (proposal)

Status: **proposal** (index entry is `status: pending`; no JSON yet).

## Why

Today the **Backups** dashboard (`backups.json`) shows one row per backup *target*
— local InfluxDB, R2 offsite, and "Obsidian vault" — from the `ops` bucket
`backup` measurement. The Obsidian vault is treated as a single target.

The Obsidian repo is being restructured (see the vault's *"Migration — vaults,
backup, automations"* note): the single `tbd` vault splits into **camping /
gear / dev / home**, backed up by a **fan-out** script that commits each vault
independently and emits a **per-vault heartbeat** — the existing
`backup,target=obsidian` line protocol gains a `vault=<name>` tag. The single
`backups` "Obsidian vault" stat can't show which vault went stale.

This dashboard answers, per vault: **did it back up, how fresh is it, and is it
growing/shrinking unexpectedly?**

## Data source

- Bucket: `ops`, measurement `backup`, tag `target=obsidian`, new tag `vault`
  (`camping|gear|dev|home`).
- Fields (from the fan-out `emit`): `success` (1i/0i), `changed` (files this run),
  `bytes` (vault size), `duration_s`.
- Datasource: `influxdb`.

## Panels (proposed)

1. **Per-vault freshness** — a stat/table row per `vault`: last-success age
   (`now() - last success`), colored by threshold (green < 36h, red > 48h since
   the backup runs ~daily), plus last result.
2. **Vault size trend** — `bytes` per vault over time; surfaces silent truncation
   (sudden drop) or runaway growth.
3. **Files changed per run** — `changed` per vault; a flatline at 0 across all
   vaults for days may mean the backup isn't seeing the iCloud mount.
4. **Last run table** — vault · last success · result · size · files · duration.

## Open questions / todos

- Decide: **extend `backups.json`** with a per-vault repeating row vs. a **separate
  `obsidian-backups.json`**. Leaning separate, so the top-level Backups stays a
  one-line-per-system overview and this is the drill-down.
- Confirm the fan-out heartbeat tag name (`vault`) and field names before building
  the queries.
- Add an **alert** when any vault's last-success age exceeds the backup interval
  (mirrors the existing `backups` todo).
- Coordinate landing via the **Idea lane** in `COORDINATION-PLAN.md` once the
  conf.d index (`dashboards.index.d/`) exists — until then this is a `status:
  pending` entry in the monolith `dashboards.index.yaml`.

## Dependencies

- The fan-out backup script (in the obsidian-tbd repo's `automations/`) must be
  emitting the `vault`-tagged heartbeat before the JSON is built; otherwise the
  queries return empty.
