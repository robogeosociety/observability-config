# dex/ — Cloudflare DEX synthetic endpoint monitoring

Public-endpoint uptime monitoring via **Cloudflare Digital Experience Monitoring
(DEX) synthetic tests** (part of Cloudflare One / Zero Trust). This replaces the
**uptime-probe** half of the host `grafana/walksheds-uptime/` collector.

> **Decision:** `Cloudflare-endpoint-plan.md` recommended a Worker→AE canary as the
> Grafana-native primary; we chose **DEX** instead — free on Zero Trust, no Worker to
> maintain, probes non-CF hostnames, and gives hop-by-hop path telemetry. The accepted
> cost: DEX results live in the **Zero Trust DEX dashboard, not Grafana/AE**.

## What moves vs. what stays

| Signal (today, host `walksheds-uptime`) | Destination |
| --- | --- |
| `walksheds.xyz` HTTP up/down + response time | **DEX synthetic test** (this dir) |
| CI smoke-test status (GitHub Actions API) | **stays** on a slimmed host job — not an HTTP probe, DEX can't do it |
| Deploy status (GitHub API) | **stays** on the slimmed host job |

DEX synthetic tests are **HTTP GET + traceroute only** — no body-substring or
latency-budget assertions (those would need the Worker canary). For a static
availability check that's sufficient.

## Tests to create

| Field | Value |
| --- | --- |
| Type | HTTP |
| Method | GET |
| Target | `https://walksheds.xyz` |
| Interval | 5 min (DEX scheduled-test cadence) |
| Account | `d7adee58513c1b2f770ccaac90cf114f` |
| Expected | 2xx; alert on sustained non-2xx / timeout |

## Apply path

DEX tests are **account-level Zero Trust config**, not part of this repo's
`deploy-provisioning.sh`. Configure via the Cloudflare One dashboard or API; record
the resulting test ID here once created.

**Dashboard:** Cloudflare One → **DEX** → **Tests** → *Add a test* → HTTP, target
`https://walksheds.xyz`, interval 5m, save. Results: DEX → *Test results* (status-code
time series + traceroute path).

**API (confirm exact path against current docs before scripting):** the DEX tests API
lives under `/accounts/{account_id}/dex/...`. See
<https://developers.cloudflare.com/cloudflare-one/insights/dex/tests/>. Terraform support
in the `cloudflare/cloudflare` v5 provider is **unconfirmed** — verify a `cloudflare_dex_test`
(or equivalent) resource exists before adding it to `../terraform/`; otherwise treat the
dashboard/API as the source of truth and pin the test config here as code-adjacent docs.

## Alerting

DEX surfaces test failures in the Zero Trust dashboard and can drive **Cloudflare
Notifications**. Because the result doesn't reach Grafana, the existing Grafana
`collector-freshness` rules do **not** cover walksheds uptime once this lands — wire a
DEX/CF notification (Discord/email) instead, mirroring the stack's Discord contact point.

## Deprecation of the old collector

Only after the DEX test is live and a CF notification is wired:

1. Strip the `uptime` HTTP-probe measurement from `grafana/walksheds-uptime/` (keep the
   CI-smoke + deploy pollers).
2. Repoint or retire the Grafana `walksheds-uptime` dashboard's uptime panels (they read
   the InfluxDB `ops` bucket `uptime` measurement, which stops getting written).
3. Update `CLAUDE.md` / `AGENTS.md` collector inventory.

> Recorded in the dev vault: `~/obsidian/dev/observability.md` (decision + tradeoff).
