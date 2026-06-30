# Daily keepalive for the #ops "Live Dashboards" thread — posts one heartbeat so the
# thread doesn't auto-archive (the dashboard containers only EDIT, which doesn't reset
# the timer). Created 2026-06-30.
#
#   nomad job run discord-collectors/nomad/thread-keepalive.hcl
#   nomad job periodic force thread-keepalive     # run now
job "thread-keepalive" {
  type        = "batch"
  datacenters = ["*"]

  periodic {
    cron             = "23 6 * * *"   # daily 06:23 PT — well under the 7-day archive window
    prohibit_overlap = true
    time_zone        = "America/Los_Angeles"
  }

  group "post" {
    task "run" {
      driver = "raw_exec"
      config {
        command = "/bin/zsh"
        args    = ["/Volumes/dev/observability/discord-collectors/run-thread-keepalive.sh"]
      }
      env {
        PATH = "/Users/tommydoerr/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }
      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
