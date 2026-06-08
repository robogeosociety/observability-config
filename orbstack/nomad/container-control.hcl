# On-demand start / stop / restart for any OrbStack container, as a Nomad
# parameterized batch job. Nomad is this machine's single home for batch ops
# (one UI, retries, dead-job visibility) — so container control lives here too,
# beside the orbstack-containers dashboard that surfaces *why* you'd restart one.
#
# This is a PARAMETERIZED job: registering it does nothing on its own; each run is
# a dispatch carrying meta. Register once after merge, then dispatch per action:
#
#   nomad job run    nomad/container-control.hcl                                  # register (maintainer)
#   nomad job dispatch -meta action=restart -meta container=grafana container-control
#   ../control.sh restart grafana          # convenience wrapper (same dispatch)
#
# raw_exec runs the task as the host user, who owns OrbStack's docker.sock, so the
# `docker` CLI reaches the engine with no extra perms (same access model as the
# docker-prune job). The Nomad agent + /bin/zsh hold Full Disk Access, needed for
# the /Volumes read of the task script. Created 2026-06-08.
job "container-control" {
  type        = "batch"
  datacenters = ["*"]

  # action + container are supplied per dispatch; a bare `nomad job run` can't
  # execute anything, which is the intended safety property.
  parameterized {
    meta_required = ["action", "container"]
  }

  group "control" {
    count = 1

    # One-shot: a failed start/stop/restart should surface as a dead allocation in
    # `nomad job status`, not silently retry against a flapping container.
    restart {
      attempts = 0
      mode     = "fail"
    }
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "docker" {
      driver = "raw_exec"

      # raw_exec inherits a minimal env; set PATH so the OrbStack `docker` shim
      # (/usr/local/bin/docker) resolves, and HOME for its CLI context.
      env {
        PATH = "/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        HOME = "/Users/tommydoerr"
      }

      # Dispatch meta is interpolated into the args; the script validates both
      # before touching docker (action allowlist + container-exists check).
      config {
        command = "/bin/zsh"
        args = [
          "/Volumes/dev/observability/orbstack/nomad/container-control.sh",
          "${NOMAD_META_action}",
          "${NOMAD_META_container}",
        ]
      }

      resources {
        cpu    = 200
        memory = 128
      }
    }
  }
}
