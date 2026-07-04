# macos-runner — bare-metal CD runner on the mini

The counterpart to [`github-runner/`](../github-runner) (the Linux/OrbStack one), with a
deliberately small job description: **CD only**. It runs on the macOS host (no container)
because a deploy has to touch the host — pull the serve clone, kickstart launchd services, and
run the on-box `tommybot doctor --deploy-check` gate. A Linux container can't reach the host's
launchd; this runner can.

```
containerized github-runner (Linux):  lint · unit tests · builds      ← pure-Python, portable
this bare-metal runner (macOS):       deploy (pull + kickstart) ·
                                      doctor --deploy-check post-deploy
```

**No model-bound CI runs here.** Local LLM generation on the mini was deprecated 2026-07-04
(the bot rebackends to the Claude API; vecserve + reindex stay). `tommybot eval` and `qwengen
verify` gate **Air-tier** artifacts in their own lane and never target this runner — hence no
`metal` label.

It's what makes the tommybot **CD workflow** real
([tommybot#90](https://github.com/robogeosociety/tommybot/pull/90)): on merge to main, deploy +
post-deploy health gate.

## Labels

```yaml
runs-on: [self-hosted, macos, arm64, mini]
```

The same set the observability-config integration tier targets (#145), so **one** org-level
runner serves both consumers.

## Service model: system LaunchDaemon, not `svc.sh`

The runner's stock `svc.sh` installs a **gui-domain LaunchAgent**. On this box — FileVault on,
no auto-login — that dies on GUI logout and doesn't return after a reboot until someone logs in
at the console: the exact Nomad failure mode. `setup.sh` therefore skips `svc.sh` and installs a
**system LaunchDaemon** (`/Library/LaunchDaemons/com.github.actions-runner.mini.plist`, running
as the mini user via the same `runsvc.sh` wrapper the stock service uses):

- survives GUI logout; starts at boot with **no GUI login needed** — only the FileVault
  pre-boot unlock, which is physics no daemon can route around;
- **one interactive `sudo`** at install (the only privileged step; `setup.sh` prompts for it);
- logs: `~/actions-runner-mini/daemon.{out,err}.log`.

Manage it via the system domain:

```sh
sudo launchctl print system/com.github.actions-runner.mini        # status
sudo launchctl kickstart -k system/com.github.actions-runner.mini # restart
sudo launchctl bootout system/com.github.actions-runner.mini      # stop
sudo launchctl bootstrap system /Library/LaunchDaemons/com.github.actions-runner.mini.plist  # start
```

Uninstall: `bootout` as above, `sudo rm /Library/LaunchDaemons/com.github.actions-runner.mini.plist`,
then `./config.sh remove --token <registration-token>` in `~/actions-runner-mini`.

## Setup

1. **PAT** — fine-grained, **Administration: read+write** at the scope you register (org or
   repo). It mints the short-lived registration token only; it is never stored in the runner.
2. `cp .env.example .env` and fill it. (`.env` is gitignored.)
3. `./setup.sh` — downloads the osx-arm64 runner, registers it, and installs the LaunchDaemon.
   Idempotent; re-run to re-register or bump the version. Run it from an **interactive shell on
   the mini**: registration snapshots the shell's PATH into the runner's `.path`, so run it
   where `uv`/`git` resolve.
4. Target it: `runs-on: [self-hosted, macos, arm64, mini]`.

## Scope: org vs repo

`setup.sh` registers at whichever scope `.env` selects:

- **Org-level** (`GH_ORG=robogeosociety`) — **one** runner serves **every** repo in the org.
  This is the standing plan now the org exists (repos transferred 2026-07-04): tommybot's CD
  and this repo's integration tier share the single registration.
- **Repo-level** (`GH_OWNER` + `GH_REPO`) — serves a single repo. The fallback for anything
  still user-owned.

## Notes

- **Persistent** (not ephemeral): a dedicated host runner that waits for jobs; launchd keeps it
  up across crashes, logouts, and (post-unlock) reboots.
- **Light by design:** CD jobs are `git` + `launchctl` + `doctor` — no multi-GB model loads on
  this runner, ever. The old resource caveat about eval sharing the 8 GB box is gone with the
  eval gate itself.
- **Daemon context has no user keychain.** A deploy step that pulls over SSH needs a key the
  daemon can read headlessly (on-disk key or system agent) — keychain-held credentials are only
  available in a GUI session.
- **gui-domain targets:** a job on this runner can `launchctl kickstart gui/<uid>/…` only while
  a GUI session exists. App services that must be reachable headlessly need their own move out
  of the gui domain (tracked in the lab-host plan, supervisor#1).
