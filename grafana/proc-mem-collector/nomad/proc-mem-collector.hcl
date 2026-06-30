# Per-process memory collector — samples the mini's top-N processes by RSS + the
# memory totals into the InfluxDB `ops` bucket (measurements `proc_mem`, `mem_summary`)
# every minute. Feeds the #ops memory treemap (discord-mini-mem) and a future Grafana
# panel. Created 2026-06-30.
#
#   nomad job run grafana/proc-mem-collector/nomad/proc-mem-collector.hcl
#   nomad job periodic force proc-mem-collector     # run now
#
# raw_exec inherits a minimal env, so PATH (uv shim, brew) and HOME are set
# explicitly. The Nomad agent + /bin/zsh hold Full Disk Access for the /Volumes read
# of INFLUX_OPS_TOKEN in observability/influxdb/.env.
job "proc-mem-collector" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "* * * * *"   # every minute
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "collect" {
    restart {
      attempts = 1
      interval = "10m"
      delay    = "30s"
      mode     = "fail"
    }
    task "run" {
      driver = "raw_exec"
      env {
        PATH = "/Users/tommydoerr/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/grafana/proc-mem-collector/run.sh"]
      }
      resources {
        cpu    = 200
        memory = 128
      }
    }
  }
}
