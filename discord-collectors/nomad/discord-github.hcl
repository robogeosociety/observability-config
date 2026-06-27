# Discord ops — GitHub activity → Discord, every 30 min.
# Convention-aligned (workspace CLAUDE.md): scheduled batch → Nomad; deps via
# `uv run --with` (no global pip); PATH/HOME set explicitly for raw_exec. The
# script self-discovers DISCORD_WEBHOOK_URL from env → observability/grafana/.env.
# The Nomad agent has Full Disk Access, so raw_exec reaches /Volumes + reads that .env.
#
#   nomad job run    discord-collectors/nomad/discord-github.hcl
#   nomad job periodic force discord-github     # run now
job "discord-github" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "*/30 * * * *"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "post" {
    task "run" {
      driver = "raw_exec"
      config {
        command = "/Users/tommydoerr/.local/bin/uv"
        args = ["run", "--with", "httpx",
          "/Volumes/dev/observability/discord-collectors/github_discord.py"]
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
