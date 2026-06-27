# Discord ops — transit alerts → Discord, every 5 min.
# Convention-aligned (workspace CLAUDE.md): scheduled batch → Nomad; deps via `uv run --with`.
# Self-discovers DISCORD_WEBHOOK_URL from observability/grafana/.env (Nomad agent has FDA).
#
# Runs via run-transit.sh, which sources the real OBA key from the transit_tracker app's
# .local/service.yaml (single source of truth, no secret copied).
#
# transit_discord.py polls per-route trips-for-route/{routeId}.json and reads alerts from
# data.references.situations[] (the old situations-for-agency endpoint was a 404 — fixed).
#
#   nomad job run    discord-collectors/nomad/discord-transit.hcl
#   nomad job periodic force discord-transit     # run now
job "discord-transit" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "*/5 * * * *"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "post" {
    task "run" {
      driver = "raw_exec"
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/discord-collectors/run-transit.sh"]
      }
      env {
        PATH         = "/Users/tommydoerr/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME         = "/Users/tommydoerr"
        UV_CACHE_DIR = "/Volumes/dev/.caches/uv"
      }
      resources {
        cpu    = 500
        memory = 256
      }
    }
  }
}
