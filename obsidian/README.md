# Obsidian vault backup

Auto-backup of the **tbd** Obsidian vault to GitHub
([`tommyroar/obsidian-tbd`](https://github.com/tommyroar/obsidian-tbd)), with a
heartbeat to the `ops` bucket so it shows on the **Backups** Grafana dashboard
(`target=obsidian`).

## Why it's shaped this way

The vault lives in iCloud Drive (`~/Library/Mobile Documents/iCloud~md~obsidian/…`).
iCloud makes **conflict copies of git internals** (observed: it renamed `.git` →
`.git 2`), so a normal in-vault repo corrupts. Instead:

- The git **database lives outside iCloud** at `~/git-repos/obsidian-tbd.git`.
- `backup.sh` drives it with `--git-dir` + `--work-tree=<vault>` — nothing
  git-related is in the synced folder.
- Push uses **HTTPS + the `gh` credential helper** (no SSH/keychain dependency).
- The heartbeat reuses `influxdb/.env` (`INFLUX_ADMIN_TOKEN`); best-effort, so a
  missing token never breaks the backup.

## Files

| File | Role |
|------|------|
| `backup.sh` | the backup script (source of truth) |
| `com.tommydoerr.obsidian-tbd-backup.plist` | LaunchAgent, runs `backup.sh` every 600 s |
| `deploy.sh` | copy `backup.sh` → `~/git-repos/`, install plist → `~/Library/LaunchAgents/`, reload |

## Install / update

```sh
./deploy.sh
```

**Requires Full Disk Access on `/bin/zsh`** (System Settings → Privacy & Security
→ Full Disk Access) — same grant the InfluxDB backup needs; the launchd job can't
read the iCloud vault or `/Volumes/.../influxdb/.env` without it.

Log: `~/git-repos/obsidian-tbd-backup.log`.
