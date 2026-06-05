# Daily runtime-version collector — writes current-vs-latest for node/python/rust
# to the InfluxDB `ops` bucket (measurement `toolchain_version`) for the "Runtime
# Versions" dashboard. Created 2026-06-05.
#
#   nomad job run grafana/runtime-versions/nomad/runtime-versions.hcl
#   nomad job periodic force runtime-versions     # run now
#
# raw_exec inherits a minimal env, so PATH (volta + uv shims, brew, rustup) and HOME
# are set explicitly. The nomad agent + /bin/zsh hold Full Disk Access for the
# /Volumes read of INFLUX_OPS_TOKEN in observability/influxdb/.env.
job "runtime-versions" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "30 8 * * *"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "collect" {
    restart {
      attempts = 1
      interval = "1h"
      delay    = "5m"
      mode     = "fail"
    }
    task "run" {
      driver = "raw_exec"
      env {
        PATH = "/Users/tommydoerr/.volta/bin:/Users/tommydoerr/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/grafana/runtime-versions/collect.sh"]
      }
      resources {
        cpu    = 500
        memory = 512
      }
    }
  }
}
