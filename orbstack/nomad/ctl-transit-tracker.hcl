# Nomad service job supervising the OrbStack `transit-tracker` container, in the `orbstack`
# namespace (the "parent" grouping — `nomad ... -namespace orbstack`, or the UI
# namespace filter). One file per container (the repo's conf.d ethos) so each shows
# as its own Running/Dead row with native Stop / Start / Restart. Deploy via
# deploy-jobs.sh (creates the namespace first). See supervise.sh for the lifecycle.
variable "supervise" {
  type    = string
  default = "/Volumes/dev/observability/orbstack/nomad/supervise.sh"
}

job "ctl-transit-tracker" {
  namespace   = "orbstack"
  type        = "service"
  datacenters = ["*"]

  group "supervise" {
    count = 1

    # Reflect the container's real state; don't auto-restart/reschedule and fight
    # a compose redeploy. Re-launch by hand (UI "Start" or deploy-jobs.sh up).
    restart {
      attempts = 0
      mode     = "fail"
    }
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "track" {
      driver       = "raw_exec"
      kill_timeout = "30s"
      kill_signal  = "SIGTERM"

      env {
        PATH = "/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }

      config {
        command = "/bin/zsh"
        args    = ["${var.supervise}", "transit-tracker"]
      }

      resources {
        cpu    = 50
        memory = 64
      }
    }
  }
}
