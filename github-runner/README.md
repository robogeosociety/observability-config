# github-runner — reusable self-hosted GitHub Actions runner (OrbStack, mini)

A self-hosted GitHub Actions runner as an OrbStack container on `tommys-mac-mini`,
following the repo's one-subdir-per-container pattern (`Dockerfile` + `docker-compose.yml`,
supervised by [`orbstack/nomad/ctl-github-runner.hcl`](../orbstack/nomad/ctl-github-runner.hcl)).

**Why:** run the cheap, portable CI gates (lint, unit tests, container builds) for the org's
repos **self-hosted** — no GitHub-Actions minutes, on the tailnet, fast — instead of on
GitHub's cloud runners.

## Scope — decided: a GitHub org (`GH_SCOPE=org`)

The direction is an **org**: one org-level runner serves **every** repo with a single
registration and `runs-on: [self-hosted, …]` — the genuine "reuse one container across all
repos" this container was built for. `entrypoint.sh` already does it (`GH_SCOPE=org`); it's a
one-line `.env` flip.

**Blocked on the org existing.** `tommyroar` is still a GitHub *user*, and user accounts can't
have org-wide runners — so **until the org is created + repos moved in, this stays repo-scoped**
(`GH_REPO=tommybot`, the repo with real CI needs). Draft until then; on org day, set `GH_SCOPE=org`
+ the org `GH_OWNER`, re-launch, done.

## One open decision — memory

It competes with the model. The mini's OrbStack VM is capped at **2 GB** to protect the
bare-metal qwen model (see the repo README + mini-cleanup notes). A build/test job wants ~1–2 GB,
making this the biggest OrbStack tenant. Org-scope *helps* here — it's **one** runner, not one
per repo, so idle footprint stays flat as repos are added. Still to settle: raise
`orb config set memory_mib` while the runner runs, keep jobs light, or gate the runner off during
model-heavy windows.

## Scope boundary — this is the *Linux* runner only

It handles the portable gates. It is **not** the runner for the Apple-Silicon / MLX gates —
`tommybot eval`, `qwengen verify`, and the `doctor --deploy-check` post-deploy gate — which
need Metal + MLX + the model weights and so want a **separate bare-metal macOS runner** on the
mini (tracked separately). Splitting them keeps this container small and the model box clean.

```
cloud/this-runner (Linux, arm64):   lint · unit tests · container builds     ← reusable, cheap
bare-metal macOS runner (separate): eval · qwengen verify · deploy-check gate ← MLX, model-bound
```

## Setup

1. **Mint a PAT.** A fine-grained PAT with **Administration: read+write** on the target
   repo(s) — used *only* to mint short-lived registration tokens at container start (the PAT
   never lands in the runner config).
2. **`.env`.** `cp .env.example .env` and fill `GH_OWNER`, `GH_REPO`, `RUNNER_PAT`. (`.env` is
   gitignored.)
3. **Launch:** `docker compose up -d --build` from this dir.
4. **Supervise:** deploy the control job so it shows in the Nomad `orbstack` console with
   Stop/Start/Restart —
   `nomad job run orbstack/nomad/ctl-github-runner.hcl` (or `orbstack/nomad/deploy-jobs.sh`).
5. **Use it:** in a workflow, `runs-on: [self-hosted, linux, arm64, mini]`.

## Notes

- **Ephemeral** (`--ephemeral`): the runner unregisters after one job and the container exits;
  `restart: unless-stopped` re-launches + re-registers. A compromised job can't persist a runner.
- **Image** is pinned (`ghcr.io/actions/actions-runner:2.321.0`) — bump deliberately.
- Labels default to `self-hosted,linux,arm64,orbstack,mini`; override via `RUNNER_LABELS`.
