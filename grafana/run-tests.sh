#!/bin/zsh
# Test pyramid runner for the observability stack (grafana + influxdb).
#
#   ./run-tests.sh [unit|integration|e2e|all]   (default: all)
#
# Tier 1 (unit/static) is hermetic — no running stack needed (also runs in CI).
# Tiers 2/3 contact the live Grafana/InfluxDB/collector; integration tests
# self-skip if a service is down.
set -uo pipefail

ROOT="${0:A:h:h}"            # repo root (/Volumes/dev/observability)
GRAFANA="$ROOT/grafana"
PY_DIRS=("$GRAFANA/tests" "$GRAFANA/dev-status" "$GRAFANA/claude-usage-collector" "$GRAFANA/anomaly-detector" "$ROOT/coordination" "$ROOT/influxdb/tests")
PYTEST=(uv run --no-project --with pytest --with pyyaml python -m pytest --import-mode=importlib)

run_unit() {
  echo "\n── Tier 1: unit + static (hermetic) ──"
  "${PYTEST[@]}" "${PY_DIRS[@]}" -m "not integration" -q
}
run_integration() {
  echo "\n── Tier 2: integration (live stack; self-skips if down) ──"
  "${PYTEST[@]}" "${PY_DIRS[@]}" -m integration -q
}
run_e2e() {
  echo "\n── Tier 3: e2e (Playwright, real browser) ──"
  ( cd "$GRAFANA/playwright" && npx playwright test )
}

case "${1:-all}" in
  unit)            run_unit ;;
  integration|int) run_integration ;;
  e2e)             run_e2e ;;
  all)             run_unit && run_integration && run_e2e ;;
  *) echo "usage: ${0:t} [unit|integration|e2e|all]"; exit 2 ;;
esac
