# orbstack/ — container control as Nomad service jobs

A **Nomad service job per OrbStack container**, all in the **`orbstack` namespace**
(the "parent" grouping), turns the Nomad UI into a real container console: each
container shows up as its own **Running/Dead** row with native **Stop / Start /
Restart**, logs, exec, and live CPU/mem graphs. Filter the Jobs list to the
`orbstack` namespace to see just the containers, grouped away from the periodic ops
jobs. It's the *control* companion to the read-only
[`orbstack-containers`](../grafana/provisioning/dashboards/infra/orbstack-containers.json)
dashboard (all containers + a btop-style memory treemap): the dashboard shows you
a container is wedged or near its mem cap; here you bounce it.

## Why service jobs (not the old parameterized job)

The first cut was a *parameterized batch* job you dispatched with `action` +
`container` meta. It worked, but Nomad's UI was useless for it: a finished batch
alloc is "Dead", so the page only ever showed opaque dispatch records — nothing
about which containers are up, and no per-container controls. A **service** job
that mirrors the container fixes exactly that:

| Job lifecycle event | What the supervisor does |
| --- | --- |
| Job starts (`Start` / `job run`) | `docker start <c>` → alloc goes **Running** |
| `Stop Job` (UI) / `job stop` | SIGTERM → `docker stop <c>` → alloc ends |
| `Restart` (UI) / `job restart` | stop + start the alloc → `docker restart <c>` |
| container exits on its own | the alloc ends too — status stays **honest** |

`attempts = 0` (no auto-restart/reschedule): the job *reflects* the container's
state, it doesn't fight whatever stopped it.

## Layout

- `supervise.sh` — the service task: `docker start`, then block on `docker wait`
  with a SIGTERM trap that `docker stop`s the container. raw_exec, host user.
- `ctl-<container>.hcl` — one service job per container (grafana, influxdb,
  transit-tracker, grafana-renderer, realitycapture-viewer), each pinned to
  `namespace = "orbstack"`. Conf.d style: add a container = add a file. A
  `supervise` var defaults to the canonical script path.
- `deploy-jobs.sh` — `up` (creates the `orbstack` namespace, then registers every
  `ctl-*.hcl`) / `down` / `status`. Scopes all nomad calls to the namespace.
- `ctl.sh` — terminal companion: `./ctl.sh <start|stop|restart> <container>`.

## Usage

Primary surface is the **Nomad UI**, filtered to the `orbstack` namespace —
<http://127.0.0.1:4646/ui/jobs?namespace=orbstack> (tailnet:
`https://tommys-mac-mini.tail59a169.ts.net:4646`). Each `ctl-*` row is a container;
click in for Stop/Start/Restart, logs, exec.

```sh
# Register every supervisor job (maintainer step, after merge):
./deploy-jobs.sh up
./deploy-jobs.sh status            # nomad status per container
./deploy-jobs.sh down              # stop + purge all

# Per-container from a shell:
./ctl.sh restart grafana
./ctl.sh stop    transit-tracker
```

## How it reaches Docker

`raw_exec` runs the task as the host user, who owns OrbStack's `docker.sock`, so
`docker` reaches the engine with no extra perms (same access model as the
`docker-prune` job). The Nomad agent and `/bin/zsh` already hold Full Disk Access,
which covers reading the script from `/Volumes`.

## Caveats

- **Coordinator-managed containers.** When the observability coordinator redeploys
  Grafana (`docker compose up -d`), it *recreates* the `grafana` container, so the
  supervised `ctl-grafana` alloc ends and the job reads **Dead** even though a new
  Grafana is up. Re-launch it (`./ctl.sh start grafana` or the UI). The supervisor
  never auto-restarts, so it can't race the coordinator.
- **Deploy note (`AGENTS.md`).** This PR only adds files; it does **not** register
  jobs. A maintainer runs `./deploy-jobs.sh up` once after merge (re-run after
  editing an `.hcl`). The `.sh` scripts are read live at run time, so script edits
  take effect on the next job (re)start without re-registering.
