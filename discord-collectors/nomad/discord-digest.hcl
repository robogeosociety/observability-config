# Discord ops — weekly digest → Discord, Mondays 08:15 (America/Los_Angeles).
# Convention-aligned (workspace CLAUDE.md): scheduled batch → Nomad; deps via `uv run --with`.
# Runs via run-digest.sh, which maps ask-dash/.env's INFLUX_READ_TOKEN → INFLUXDB_TOKEN
# (digest.py reads InfluxDB) and reads the webhook from grafana/.env. Nomad agent has FDA.
#
#   nomad job run    discord-collectors/nomad/discord-digest.hcl
#   nomad job periodic force discord-digest     # run now
job "discord-digest" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "15 8 * * 1"   # Monday 08:15 (cron dow 1 = Monday)
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "post" {
    task "run" {
      driver = "raw_exec"
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/discord-collectors/run-digest.sh"]
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
