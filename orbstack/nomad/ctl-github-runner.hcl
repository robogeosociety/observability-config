# Nomad service job supervising the OrbStack `github-runner` container (the reusable
# self-hosted GitHub Actions runner), in the `orbstack` namespace. One file per container
# (conf.d ethos) so it shows as its own Running/Dead row with native Stop/Start/Restart.
# Deploy via deploy-jobs.sh. See supervise.sh for the lifecycle. Created 2026-07.
variable "supervise" {
  type    = string
  default = "/Volumes/dev/observability/orbstack/nomad/supervise.sh"
}

job "ctl-github-runner" {
  namespace   = "orbstack"
  type        = "service"
  datacenters = ["*"]

  group "supervise" {
    count = 1

    # Reflect the container's real state; don't auto-restart/reschedule and fight a
    # compose redeploy. Re-launch by hand (UI "Start" or deploy-jobs.sh up).
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
        args    = ["${var.supervise}", "github-runner"]
      }

      resources {
        cpu    = 50
        memory = 64
      }
    }
  }
}
