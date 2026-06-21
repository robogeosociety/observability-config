# DEX synthetic test — implementation plan

Stand up the Cloudflare **DEX synthetic test** for `walksheds.xyz` uptime (the decision +
rationale live in `README.md`), wire failure notifications, and slim the host
`grafana/walksheds-uptime/` collector to the signals DEX can't do. DEX tests are
**account-level Zero Trust config**, not part of this repo's `deploy-provisioning.sh`, so
this is mostly an external stand-up plus a small repo-side cleanup.

## Prereqs
- Cloudflare One / Zero Trust on account `d7adee58513c1b2f770ccaac90cf114f` (DEX is free on all ZT plans).
- DEX probes any public hostname from Cloudflare's network, so `walksheds.xyz` being GitHub-Pages (not a CF zone) is fine.

## Steps

### 1. Create the test
- **Dashboard:** Cloudflare One → **DEX → Tests → Add a test** → HTTP, target `https://walksheds.xyz`, method GET, interval **5 min**. Save; **record the test ID** in `README.md`.
- **IaC option:** check whether the `cloudflare/cloudflare` v5 provider exposes a DEX-test resource (e.g. `cloudflare_zero_trust_dex_test`). If yes, add it to `terraform/` (consistent with the R2-token IaC) so the test is store-as-code. If not, the dashboard/API is the source of truth — pin the config here as code-adjacent docs.
- Ref: <https://developers.cloudflare.com/cloudflare-one/insights/dex/tests/>

### 2. Wire failure notification
DEX results land in the **Zero Trust DEX dashboard, not Grafana** — so the walksheds uptime signal leaves Grafana's alerting entirely. Replace it:
- **Option A (preferred):** Cloudflare **Notifications** → a DEX/health trigger → a **Discord webhook** (or email→Discord), matching the stack's Discord contact point. Use this if a DEX-test-failure notification type exists.
- **Option B (fallback):** a tiny Worker **cron** that reads the DEX results API and posts to Discord — more control, more code. Only if Option A can't trigger on DEX failures.

### 3. Verify
- Confirm the test runs: DEX → **Test results** (status-code time series + hop-by-hop traceroute).
- Fire a failure (temporarily target a 404 path) → confirm the notification fires → revert.

### 4. Slim the host collector
- `grafana/walksheds-uptime/`: **remove the uptime HTTP-probe** portion (now DEX). **Keep** the CI-smoke + deploy-status pollers — they read the GitHub API, which DEX can't do.
- `walksheds-uptime` Grafana dashboard: drop the uptime panels that read the InfluxDB `ops` `uptime` measurement (it stops getting written); keep the CI/deploy panels.
- Update the collector's deploy script / launchd entry to reflect the slimmed scope.

### 5. Docs
- `README.md`: mark the test live, record the test ID + the notification path.
- `CLAUDE.md` / `AGENTS.md`: update the collector inventory if `walksheds-uptime`'s scope changes.

## Open questions / risks
- Does CF **Notifications** support a DEX-test-failure trigger? If not → Option B Worker.
- **Terraform** DEX-test resource availability in cloudflare v5 — verify before committing IaC; otherwise dashboard/API is the source of truth.
- DEX free-tier limits (number of tests / interval floor) — confirm 5-min is allowed.
- Accept losing the Grafana uptime panel/alert for walksheds (DEX dashboard + CF notification is the replacement) — recommended, vs keeping a thin Grafana mirror.

## Checklist
- [ ] DEX test created + running (test ID in `README.md`)
- [ ] Failure notification wired + test-fired
- [ ] `walksheds-uptime` collector slimmed (uptime probe out; CI/deploy pollers kept)
- [ ] Grafana `walksheds-uptime` dashboard updated
- [ ] Docs updated

> Sibling: `README.md` (decision + the "what moves vs what stays" split). This is the *how*.
