# Daily InfluxDB backup → 2TB disk + (optional) Cloudflare R2 offsite.
# Migrated from launchd (dev.tommydoerr.influxdb-backup) to Nomad periodic 2026-06-04.
#
#   nomad job run nomad/influxdb-backup.nomad
#   nomad job periodic force influxdb-backup     # run now
#
# /bin/zsh holds Full Disk Access (granted originally for this very backup) and the
# nomad agent binary also has FDA, so its raw_exec child inherits /Volumes access.
# All /Volumes work + the container dump stream-out is done by backup.sh; the heartbeat
# lands in the `ops` bucket for the Grafana "Backups" dashboard. raw_exec inherits a
# minimal env, so PATH/HOME are set explicitly (docker, influx CLI, wrangler under HOME).
job "influxdb-backup" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "30 3 * * *"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "backup" {
    restart {
      attempts = 1
      interval = "1h"
      delay    = "5m"
      mode     = "fail"
    }
    task "run" {
      driver = "raw_exec"
      env {
        PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/influxdb/backup.sh"]
      }
      resources {
        cpu    = 1000
        memory = 1024
      }
    }
  }
}
