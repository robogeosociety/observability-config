# orbstack/ — on-demand container control via Nomad

One-touch **start / stop / restart** for OrbStack containers, run through Nomad so
every action has a record, retries, and dead-job visibility in one place
(`http://127.0.0.1:4646`) — the same reason the rest of this machine's batch ops
live in Nomad. It's the *action* companion to the read-only
[`orbstack-containers`](../grafana/provisioning/dashboards/infra/orbstack-containers.json)
dashboard (all containers + a btop-style memory treemap): the dashboard shows you
a container is wedged or near its mem cap; this is how you bounce it.

## Layout

- `nomad/container-control.hcl` — a **parameterized** batch job. Registering it
  does nothing on its own; each run is a *dispatch* carrying `action` + `container`
  meta. That's the safety property — a bare `nomad job run` can't act.
- `nomad/container-control.sh` — the task script (raw_exec → `docker <action>
  <container>`). Allowlists the action and checks the container exists before
  touching docker, so a typo fails loudly instead of silently.
- `control.sh` — human convenience wrapper: dispatches one container or `--all`
  managed stack containers.

## Why Nomad (not just `docker restart`)

`docker restart grafana` works, but leaves no trail. Routing through Nomad gives a
dated record in `nomad job status container-control`, dead-alloc visibility when a
restart fails against a flapping container, and keeps container ops in the one
batch surface this machine already uses for backups, prunes, and collectors.

## How it reaches Docker

`raw_exec` runs the task as the host user, who owns OrbStack's `docker.sock`, so
the `docker` CLI reaches the engine with no extra perms — the same access model as
the `docker-prune` job. The Nomad agent and `/bin/zsh` already hold Full Disk
Access (for `/Volumes` reads), which covers reading the task script.

## Usage

```sh
# Register once (maintainer step, after this PR merges):
nomad job run nomad/container-control.hcl

# Then dispatch — directly:
nomad job dispatch -meta action=restart -meta container=grafana container-control
nomad job dispatch -meta action=stop    -meta container=transit-tracker container-control

# …or via the wrapper:
./control.sh restart grafana
./control.sh stop    transit-tracker
./control.sh restart --all        # grafana, influxdb, transit-tracker, grafana-renderer

# Inspect runs:
nomad job status container-control
nomad alloc logs <alloc-id>
```

## Deploy note

Per `AGENTS.md`, this PR only adds files — it does **not** register the job. The
observability coordinator deploys Grafana *provisioning*; it does not run Nomad
specs. A maintainer runs `nomad job run nomad/container-control.hcl` once after
merge to register it (re-run after any edit to the `.hcl`). The `.sh` files are
read live from `/Volumes/dev/observability/orbstack/` at dispatch time, so script
edits take effect on the next dispatch without re-registering.
