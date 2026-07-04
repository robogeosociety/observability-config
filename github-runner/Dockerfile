# Reusable self-hosted GitHub Actions runner as an OrbStack container on the mini (arm64).
#
# SCOPE — tommyroar is a GitHub *user*, not an org, so runners are **repo-level**. This
# container registers to one repo (GH_REPO in .env) and is "reusable" in that you point it
# at any repo by changing that var and re-launching. If tommyroar ever becomes an org, set
# GH_SCOPE=org in .env for a single runner shared across every repo (one-line change).
#
# LINUX runner — for the cheap, portable gates (lint, unit tests, container builds) across
# repos, self-hosted (no GitHub-minutes, on the tailnet, fast). It is deliberately NOT the
# runner for the Apple-Silicon / MLX gates: tommybot `eval`, `qwengen verify`, and the
# `doctor --deploy-check` post-deploy gate need Metal + MLX + the model weights, which a
# Linux container can't provide — those want a separate bare-metal macOS runner on the mini.
#
# EPHEMERAL — the runner registers with --ephemeral, so it unregisters after one job and the
# container exits; compose's `restart` re-launches and re-registers. A compromised job can't
# leave a persistent runner behind.
FROM ghcr.io/actions/actions-runner:2.321.0

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER runner

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh
ENTRYPOINT ["/home/runner/entrypoint.sh"]
