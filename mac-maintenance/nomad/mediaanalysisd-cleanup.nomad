# Weekly threshold-gated cleanup of the macOS mediaanalysisd ANE cache leak.
# See ../mediaanalysisd-cleanup.sh for the why (a runaway e5bundlecache once hit
# 42GB and filled the internal disk). No-op until the cache exceeds 3GB.
#
#   nomad job run mac-maintenance/nomad/mediaanalysisd-cleanup.nomad
#   nomad job periodic force -namespace backup mediaanalysisd-cleanup   # run now
#
# Same host-execution model as influxdb-backup: raw_exec runs /bin/zsh (which holds
# Full Disk Access) as tommydoerr, so it can read ~/Library/Containers and kill the
# host mediaanalysisd daemon. raw_exec inherits a minimal env, so PATH/HOME are set
# explicitly (the script needs HOME to locate the cache and the influxdb/.env token).
job "mediaanalysisd-cleanup" {
  namespace   = "backup"
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "0 4 * * 0"
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "cleanup" {
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
        args    = ["/Volumes/dev/observability/mac-maintenance/mediaanalysisd-cleanup.sh"]
      }
      resources {
        cpu    = 500
        memory = 256
      }
    }
  }
}
