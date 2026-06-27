# Claude output-token milestone → Discord, every 15 min (America/Los_Angeles).
# Convention-aligned (workspace CLAUDE.md): scheduled batch → Nomad; deps via `uv run --with`.
# Runs via run-claude-tokens.sh, which maps ask-dash/.env's INFLUX_READ_TOKEN → INFLUXDB_TOKEN
# and reads the webhook from grafana/.env (DISCORD_WEBHOOK_URL_CLAUDE). Nomad agent has FDA.
#
#   nomad job run    discord-collectors/nomad/discord-claude-tokens.hcl
#   nomad job periodic force discord-claude-tokens     # run now
job "discord-claude-tokens" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "*/15 * * * *"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "post" {
    task "run" {
      driver = "raw_exec"
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/discord-collectors/run-claude-tokens.sh"]
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
