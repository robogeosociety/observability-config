#!/usr/bin/env python3
"""Minimal Cloudflare R2 uploader (wrangler CLI), used by backup.sh.

Auth + config from the environment (sourced from influxdb/.env):
  CLOUDFLARE_API_TOKEN   an API token scoped to R2 edit (Workers R2 Storage:Edit)
  R2_BUCKET              target bucket name (default: influxdb-backups)

This shells out to `wrangler r2 object`, so no S3 access-key pair is needed —
one CLOUDFLARE_API_TOKEN covers put/list/delete. `--remote` is required on every
call, otherwise wrangler reads/writes its *local* simulated R2 instead of the
real bucket.

Usage:
  r2_upload.py put <local_path> <key>   # upload a file
  r2_upload.py check                     # verify write access (put+delete a probe)
  r2_upload.py list [prefix]             # list keys (client-side prefix filter)

Invoke with `uv run --with … python r2_upload.py …` is NOT required — this has no
pip deps; it only needs `wrangler` (and `npx` as a fallback) on PATH.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

# wrangler r2 object paths are always {bucket}/{key}.


def _wrangler_base():
    """Return the argv prefix to invoke wrangler (direct binary, else npx)."""
    if shutil.which("wrangler"):
        return ["wrangler"]
    if shutil.which("npx"):
        return ["npx", "--yes", "wrangler"]
    print("error: neither `wrangler` nor `npx` found on PATH", file=sys.stderr)
    sys.exit(3)


def _run(args, **kw):
    """Run wrangler with the API token in the environment. Returns CompletedProcess."""
    env = dict(os.environ)
    # wrangler reads CLOUDFLARE_API_TOKEN for non-interactive auth.
    if "CLOUDFLARE_API_TOKEN" not in env:
        print("error: CLOUDFLARE_API_TOKEN not set (needed for R2 access)", file=sys.stderr)
        sys.exit(3)
    return subprocess.run(_wrangler_base() + args, env=env,
                          capture_output=True, text=True, **kw)


def _bucket():
    return os.environ.get("R2_BUCKET", "influxdb-backups")


def cmd_put(local_path, key):
    bucket = _bucket()
    r = _run(["r2", "object", "put", f"{bucket}/{key}",
              "--file", local_path, "--remote",
              "--content-type", "application/gzip"])
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        return r.returncode
    print(f"uploaded {local_path} -> r2://{bucket}/{key}")
    return 0


def cmd_check():
    bucket = _bucket()
    # put + delete a tiny probe object to prove write access end-to-end.
    with tempfile.NamedTemporaryFile("w", suffix=".probe", delete=False) as fh:
        fh.write("ok")
        probe = fh.name
    try:
        rp = _run(["r2", "object", "put", f"{bucket}/.write-probe",
                   "--file", probe, "--remote", "--content-type", "text/plain"])
        if rp.returncode != 0:
            sys.stderr.write(rp.stdout + rp.stderr)
            return rp.returncode
        rd = _run(["r2", "object", "delete", f"{bucket}/.write-probe", "--remote"])
        if rd.returncode != 0:
            sys.stderr.write(rd.stdout + rd.stderr)
            return rd.returncode
    finally:
        os.unlink(probe)
    print(f"write OK -> r2://{bucket}")
    return 0


def cmd_list(prefix=""):
    """List objects via the Cloudflare REST API (wrangler has no object lister).

    Needs CLOUDFLARE_ACCOUNT_ID alongside CLOUDFLARE_API_TOKEN. This is a
    convenience/debug command — the backup pipeline itself only uses `put`.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    bucket = _bucket()
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not acct:
        print("error: `list` needs CLOUDFLARE_ACCOUNT_ID set (token alone is "
              "enough for put/check, but object listing uses the REST API)",
              file=sys.stderr)
        return 3
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
           f"/r2/buckets/{bucket}/objects")
    if prefix:
        url += f"?prefix={urllib.parse.quote(prefix)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"{e.code} {e.reason}: {e.read().decode(errors='replace')}\n")
        return 1
    n = 0
    for obj in payload.get("result", []) or []:
        print(f"{obj.get('size', 0):>12}  {obj.get('key', '')}")
        n += 1
    print(f"({n} objects)")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "put":
        return cmd_put(sys.argv[2], sys.argv[3])
    if cmd == "check":
        return cmd_check()
    if cmd == "list":
        return cmd_list(sys.argv[2] if len(sys.argv) > 2 else "")
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
