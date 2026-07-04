# macos-runner — bare-metal Apple-Silicon GitHub Actions runner (mini)

The counterpart to [`github-runner/`](../github-runner) (the Linux/OrbStack one). This runner
runs **on the macOS host** (no container) so it has **Metal + MLX + the resident model cache** —
which is what the tommybot **Apple-Silicon gates** need and a Linux container can't give:

```
containerized github-runner (Linux):  lint · unit tests · builds        ← pure-Python, portable
this bare-metal runner (macOS):        tommybot eval · qwengen verify ·  ← MLX, model-bound
                                       doctor --deploy-check post-deploy
```

It's what makes the tommybot **CD workflow** (`.github/workflows/deploy.yml`) real: on merge, it
runs the gates on this runner, deploys to the mini's launchd serve, then runs `doctor
--deploy-check` — the on-box gate that catches a silent NumPy fallback cloud CI can't.

## Why bare-metal, not OrbStack
MLX needs Metal (the GPU); OrbStack is a Linux VM with no Metal passthrough. `eval`/`verify` load
the quantized model and run real inference — only the host can. The runner runs as the mini user,
so it reuses the same model cache + `.venv` the serve uses (no re-download).

## Setup
1. **PAT** — fine-grained, **Administration: read+write** on `GH_REPO` (mints the registration token only).
2. `cp .env.example .env` and fill `GH_OWNER`, `GH_REPO`, `RUNNER_PAT`. (`.env` is gitignored.)
3. `./setup.sh` — downloads the osx-arm64 runner, registers it, and installs it as a launchd
   service (`svc.sh`, the runner's native macOS manager). Idempotent; re-run to re-register or bump.
4. Target it: `runs-on: [self-hosted, macos, arm64, metal, mini]`.

Manage it: `~/actions-runner-mini/svc.sh {status,stop,start}`.

## Scope: org vs repo
`setup.sh` registers at whichever scope `.env` selects:
- **Org-level** (`GH_ORG`) — **one** runner serves **every** repo in the org. This is the target
  once `tommyroar` becomes an org: any repo's MLX/Metal job can `runs-on: [self-hosted, …, metal]`
  with a single registration.
- **Repo-level** (`GH_OWNER` + `GH_REPO`) — serves one repo (`tommybot`). The only option on a
  user account; leave it here until the org exists, then flip to `GH_ORG` and re-run `setup.sh`.

## Notes
- **Persistent** (not ephemeral): a dedicated host runner that waits for jobs; launchd keeps it up.
- **Resource note:** an `eval` run loads the model (~3 GB) transiently. It shares the 8 GB box with
  the always-on serve — the workflow should run gates when the serve can spare it (or accept the
  serve idle-evicting during the job). The `qwengen verify` gate itself enforces the memory budget.
