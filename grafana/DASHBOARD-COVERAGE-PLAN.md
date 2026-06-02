# Plan — generalize the nodata/regression suite to all dashboards

Tracks **`tommyroar/robot-geographical-society#67`** ("Extend dashboard
nodata/regression test coverage to all dashboards"). This is the implementation
scope for **this** repo (`observability-config`); the issue lives in the other
repo only because that's where the board is.

> **Status: plan only.** This PR adds no test code — it scopes the work so the
> implementation PRs that follow are small and reviewable. See
> [`TESTING.md`](TESTING.md) for the existing three-tier pyramid this extends.

## Why

PR #52 added a layered validation + regression suite **scoped to
`provisioning/dashboards/campsites/`** after three "renders No data" bugs slipped
past plain JSON validation:

1. **Hardcoded query window on a time-axis panel** — the query bounds its own
   window (`now() - INTERVAL '30' DAY`) instead of following the time picker, so a
   shorter/remembered range renders an empty viewport.
2. **Single-value query variable defaults to a dataless edge value** — availability
   `target_date` defaulted to the earliest, past date (`2026-05-01`).
3. **Panel uses a datasource function the backend doesn't implement** — Analytics
   Engine has no `uniqExactIf`/`uniq`; a Flux `reduce` accumulator must be named
   `accumulator`. Surfaces as an errored, empty panel.

The goal: catch all three **repo-wide**, and auto-cover any new dashboard with no
per-dashboard test edits.

## What the audit found (this is the load-bearing part)

I ran the campsites static-guard logic over **all 14 dashboards** in every domain
(`campsites/ dev/ infra/ ops/ transit/ weather/`). Result:

- **Every `timeseries`/`trend` panel is already range-bound** — Flux panels use
  `v.timeRangeStart`, the one ClickHouse dashboard uses `$timeFilter`. **Zero**
  panels hardcode their window today.
- **The only single-value query variable in the repo is campsites `target_date`**,
  and it's already data-scoped. `mac-system`'s `metric` is a `custom` var (not a
  query), every other var is `multi`+`includeAll`. Nothing else can trip rule 2.
- The guard's panel-type set is deliberately narrow (`timeseries`/`trend` only),
  so the **categorical barcharts** the issue worried about (`x = agency`, a
  current-snapshot aggregation) are **already excluded by design** — no allowlist
  needed for them.

**Implication:** promoting the static guards to run over every dashboard is a
**clean lift — it goes green immediately** and becomes a forward-looking
regression gate. The static tier is the cheap win; do it first.

The genuine engineering is in the **integration tier**: generalizing the live
"does every panel actually return rows" check to dashboards whose data is
intermittent, event-driven, or short-window — which needs a real, explicit
per-dashboard allow-empty config so the suite never flaps on a legitimately quiet
panel.

## Plan

### Phase 1 — Static tier (hermetic, CI). *Small, lands first.*

Fold the three campsites guards from
[`tests/test_campsites_dashboards_static.py`](tests/test_campsites_dashboards_static.py)
into [`tests/test_dashboards_static.py`](tests/test_dashboards_static.py), which
already `rglob`s the whole `dashboards/` tree:

- `test_time_axis_panels_follow_the_picker` (rule 1)
- `test_clickhouse_time_macros_declare_date_column` (the silent-`$timeFilter` variant)
- `test_single_value_query_var_is_data_scoped` (rule 2)

Then **delete** `test_campsites_dashboards_static.py` (fully subsumed — keeping it
would double-run the same asserts on campsites).

- Add a module-level `TIME_AXIS_EXCEPTIONS` allowlist keyed by `(dash_uid,
  panel_id)` for any *future* legitimately-fixed-window time-axis panel
  (intentional status stat rendered as a trend, etc.). **Seed it empty** — today
  nothing needs it — with a comment showing the shape, so the next person adds an
  entry instead of weakening the rule.
- Acceptance for this phase: `run-tests.sh unit` stays green with the guards now
  applied to all 14 dashboards.

### Phase 2 — Integration tier (live, local/maintainer-only). *The real work.*

Generalize
[`tests/test_campsites_integration.py`](tests/test_campsites_integration.py) into
a `test_dashboards_integration.py` parametrized over the **whole** `dashboards/`
tree. The mechanics already exist; what changes is breadth + config:

1. **Var resolution for the general case.** `_resolve_vars` / `_interpolate`
   today handle `${name}` and `${name:json}` with the first row as the single
   value. Extend to:
   - `multi`+`includeAll` vars whose Grafana default is `$__all` → expand to all
     values (the existing `:json` path), and the bare `${name}` regex-filter form
     used in Flux/`=~`.
   - `custom` vars (e.g. `mac-system` `metric`) → take the configured default.
   - Leave a var unresolved only when its own query errors; record that, don't crash.
2. **Two windows, per dashboard.** Keep the existing default-range + `now-24h`
   pair, but source the default from each dashboard's `time.from/to` (already
   done) — note several dashboards default to very short windows (`now-30m`
   transit, `now-3h` tempest/mac); the `now-24h` second pass still exercises the
   "hardcoded window" class for those.
3. **Health-aware, unchanged.** A query error on a **healthy** datasource = fail
   (rule 3 — the unsupported-function class); on an unhealthy one (token not
   configured, e.g. the AE/R2 panels) = skip. This is the existing `_ds_healthy`
   logic; it already covers bug class 3 at runtime — no static equivalent needed.
4. **Per-dashboard allow-empty config — new, and the crux.** Replace the
   campsites-only `ALLOW_EMPTY = ("failing",)` title heuristic with an explicit
   file, `tests/dashboard_coverage.yaml`, keyed by dashboard `uid`:
   ```yaml
   campsite-availability:
     allow_empty_panels: [failing-sites]      # by panel title or id
   status-page:
     allow_empty_panels: ["*"]                # status stats are legitimately quiet
   transit-tracker-monitor:
     windows: [default]                       # skip now-24h (event-driven, 30m dashboard)
   walksheds-uptime:
     allow_empty_panels: ["*"]
   ```
   Same "intent is code" philosophy as `dashboards.index.yaml`. A dashboard with
   no entry gets the strict default (every queryable panel must return rows over
   both windows). `test_dashboard_index.py`-style guard: every uid in the config
   must match a real dashboard (no orphan entries).
5. **Candidate allow-empty / window exceptions to confirm against the live stack**
   (the maintainer runs Tier 2 and tunes these — they are *hypotheses* from the
   static survey, not facts):
   - `status-page`, `walksheds-uptime` — uptime/status stats quiet by design.
   - `ops/backups`, `ops/obsidian-backups` — daily heartbeats; may be empty at
     `now-24h` depending on timing.
   - `transit-tracker-*` — `now-30m` dashboards; event-driven, likely default-window only.
   - `claude-usage` (dev) — usage-driven; deliberately **excluded** from collector
     freshness alerts already (see `CLAUDE.md` Alerting), so treat as allow-empty.
   - `ha-pi-system`, `mac-system`, `orbstack-containers`, `tempest-basic` —
     continuously-written; expect **strict** (no exceptions).

### Phase 3 — Visual tier (Playwright, local/maintainer-only).

Extend the
[`playwright/campsites-visual.spec.ts`](playwright/campsites-visual.spec.ts)
pattern — structural guard (`canvas` mounted **and** no "No data") + a loose
`toHaveScreenshot` threshold — to **key panels of every dashboard**:

- Drive the panel list from a small committed manifest (one entry per
  `{dash, panelId, name, vars?}`), mirroring the existing `PANELS` array, so adding
  a dashboard means adding manifest rows, not new spec files.
- **Window choice per dashboard:** relative (`now-7d`-style) for live/continuous
  data with loose thresholds (the campsites approach); a **fixed past window** for
  immutable data where a tight baseline is possible (the `visual.spec.ts` tempest
  approach). Pick per dashboard; default to relative + structural-guard-as-the-real-test.
- The structural guard is the drift-proof regression catch; the screenshot is the
  gross-layout backstop. Commit baselines under
  `playwright/<spec>.spec.ts-snapshots/` (machine-specific).
- **Out of scope to *baseline*** here: dormant/event-driven dashboards where a
  fresh render is routinely blank (would bake "No data" into a baseline). Cover
  those at the integration tier's allow-empty config instead; `log`/comment which
  dashboards are intentionally not visually baselined so the gap is explicit, not
  silent.

### Phase 4 — Docs + runner.

- `TESTING.md`: rewrite the campsites-specific bullets in Tiers 1–3 to describe
  the now-repo-wide guards; document `dashboard_coverage.yaml` and the
  `TIME_AXIS_EXCEPTIONS` allowlist as the two "encode the exception, don't weaken
  the rule" escape hatches.
- `AGENTS.md`: one line under "How to make a change" — *new dashboard ⇒ no test
  edits needed; add a `dashboard_coverage.yaml` entry only if a panel is
  legitimately empty, and a visual-manifest row if you want a baseline.*
- `run-tests.sh`: no structural change (tiers already glob the dirs); confirm the
  renamed integration file is picked up.
- Changelog/index: none — these are test changes, not dashboard add/remove/move,
  so `changelog.jsonl` and `dashboards.index.yaml` are untouched.

## CI / hermetic boundary (unchanged)

- **Tier 1 (static)** runs in GitHub Actions and is the only thing CI gates on.
  After Phase 1 it covers all dashboards.
- **Tiers 2 & 3** stay **local/maintainer-only** — they need the live
  Grafana/InfluxDB stack on the Mac mini, which CI doesn't have. The coordinator
  deploy and `run-tests.sh integration|e2e` remain the maintainer's to run.

## Acceptance (maps to the issue)

- [ ] The three bug classes are caught repo-wide: rules 1–2 statically (Phase 1),
      rule 3 via health-aware integration (Phase 2).
- [ ] New dashboards are auto-covered by glob-parametrized static tests with **no
      per-dashboard test edits** (Phase 1); an allow-empty entry is needed **only**
      for a legitimately-empty panel.
- [ ] CI green on the hermetic tier (Phase 1 lands green on day one).
- [ ] Conventions documented in `TESTING.md` / `AGENTS.md` (Phase 4).

## Suggested PR sequence

1. **Phase 1** — promote static guards + delete the campsites-only file (small,
   CI-green, mergeable on its own).
2. **Phase 2** — generalized integration test + `dashboard_coverage.yaml` (the
   maintainer runs Tier 2 locally to tune allow-empty entries before merge).
3. **Phase 3** — visual manifest + baselines.
4. **Phase 4** — docs (can fold into whichever of the above lands last).
