#!/usr/bin/env python3
"""proc-mem-collector — sample Mac-mini per-process memory into InfluxDB.

Writes two measurements to the `ops` bucket every run (one Nomad periodic tick):
  proc_mem,name=<process>  rss_bytes=<int>i        # top-N processes by RSS
  mem_summary              used_bytes=,total_bytes=,wired_bytes=,comp_bytes=,free_bytes=  (all i)

Cardinality is bounded to the top-N process names (default 15) + one summary series,
so this can't blow up InfluxDB series the way an unfiltered procstat would. The data
feeds the #ops memory-treemap (discord-mini-mem) and is queryable for a Grafana panel.

  uv run --no-project python collector.py --dry-run   # print line protocol, don't write

Token: INFLUX_OPS_TOKEN (or INFLUX_ADMIN_TOKEN) from $RUNTIME/.env, mirroring
runtime-versions / claude-usage-collector — never in the Nomad/launchd env dump.
"""
import argparse, os, subprocess, sys, time, urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".local" / "share" / "proc-mem-collector" / ".env"
TOP_N = int(os.environ.get("PROC_MEM_TOP_N", "15"))
PG = 16384  # Apple-silicon vm_stat page size

# Roll noisy multi-process apps up to a friendlier name (display only; tag value).
RENAME = {
    "OrbStack Helper": "OrbStack", "Google Chrome Helper": "Chrome",
    "Google Chrome Helper (Renderer)": "Chrome", "com.apple.WebKit.WebContent": "WebKit",
}


def load_env_file(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (existing env wins)."""
    try:
        text = path.read_text()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def _esc_tag(v) -> str:
    """Escape an InfluxDB line-protocol tag value."""
    return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


# --- pure parsing (hermetically tested) -------------------------------------

def parse_ps(text: str) -> list[tuple[str, int]]:
    """`ps -axo rss=,comm=` output -> [(name, rss_kb)] aggregated by command basename,
    descending by memory. RSS is in KiB (ps default)."""
    agg: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        rss_s, _, comm = line.partition(" ")
        try:
            rss = int(rss_s)
        except ValueError:
            continue
        name = comm.strip().split("/")[-1]
        if not name:
            continue
        name = RENAME.get(name, name)
        agg[name] = agg.get(name, 0) + rss
    return sorted(agg.items(), key=lambda kv: -kv[1])


def parse_vmstat(text: str) -> dict[str, int]:
    """`vm_stat` output -> {stat: pages}."""
    out: dict[str, int] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            out[k.strip()] = int(v)
    return out


def summary(vm: dict[str, int], total_bytes: int) -> dict[str, int]:
    """Derive the memory totals (bytes) the header line needs."""
    free = (vm.get("Pages free", 0) + vm.get("Pages speculative", 0)) * PG
    return {
        "total_bytes": total_bytes,
        "used_bytes": max(0, total_bytes - free),
        "wired_bytes": vm.get("Pages wired down", 0) * PG,
        "comp_bytes": vm.get("Pages occupied by compressor", 0) * PG,
        "free_bytes": free,
    }


def build_lines(procs: list[tuple[str, int]], summ: dict[str, int],
                top_n: int = TOP_N, ts_ns: int | None = None) -> list[str]:
    """Line protocol for the top-N processes + the summary. Cardinality = top_n + 1."""
    suffix = f" {ts_ns}" if ts_ns is not None else ""
    lines = [
        f"proc_mem,name={_esc_tag(name)} rss_bytes={rss * 1024}i{suffix}"
        for name, rss in procs[:top_n] if rss > 0
    ]
    fields = ",".join(f"{k}={v}i" for k, v in summ.items())
    lines.append(f"mem_summary {fields}{suffix}")
    return lines


# --- IO ---------------------------------------------------------------------

def sample() -> tuple[list[tuple[str, int]], dict[str, int]]:
    ps = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True).stdout
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
    return parse_ps(ps), summary(parse_vmstat(vm), total)


def influx_write(lines: list[str]) -> int:
    load_env_file()
    url = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
    org = os.environ.get("INFLUX_ORG", "home")
    bucket = os.environ.get("INFLUX_OPS_BUCKET", "ops")
    token = os.environ.get("INFLUX_OPS_TOKEN") or os.environ.get("INFLUX_ADMIN_TOKEN")
    if not token:
        raise SystemExit("No InfluxDB token (set INFLUX_OPS_TOKEN in proc-mem-collector/.env)")
    if not lines:
        return 0
    endpoint = f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=ns"
    req = urllib.request.Request(
        endpoint, data="\n".join(lines).encode(), method="POST",
        headers={"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 204):
            raise SystemExit(f"InfluxDB write HTTP {resp.status}")
    return len(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print line protocol, don't write")
    args = ap.parse_args()
    procs, summ = sample()
    lines = build_lines(procs, summ, ts_ns=time.time_ns())
    if args.dry_run:
        print("\n".join(lines))
        print(f"# {len(lines)} points (dry-run)", file=sys.stderr)
        return
    n = influx_write(lines)
    print(f"wrote {n} points to InfluxDB", file=sys.stderr)


if __name__ == "__main__":
    main()
