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

    # UNLIKE the other ctl-* jobs, this one KEEPS THE CONTAINER ALIVE rather than
    # merely mirroring its state. The shared "attempts = 0, re-launch by hand"
    # stance exists so a supervisor can't fight a compose redeploy — but the
    # compose stack that rationale referred to was InfluxDB's, retired
    # 2026-07-22 (rgs#167). transit-tracker is now a standalone container with
    # no redeploy to fight, and it drives a physical ESP32 display: silence is a
    # dead sign on the wall, not a stale UI row.
    #
    # What went wrong without this: the mini rebooted 2026-07-25 12:33 PT, the
    # alloc failed, `attempts = 0` meant nothing rescheduled it, and the job sat
    # dead until a human noticed. See discobots#74 for the sibling case.
    #
    # restart  — bounded, so a container that CANNOT start (e.g. supervise.sh
    #            exit 3, no such container) fails loudly instead of hot-looping.
    # reschedule — unlimited with exponential backoff, so a reboot or a lost
    #            alloc always comes back on its own.
    restart {
      attempts = 3
      interval = "10m"
      delay    = "30s"
      mode     = "fail"
    }
    reschedule {
      delay          = "30s"
      delay_function = "exponential"
      max_delay      = "10m"
      unlimited      = true
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
