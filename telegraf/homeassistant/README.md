# telegraf/homeassistant/ — Home Assistant host metrics

Telegraf running on the **Home Assistant OS** box, writing its system metrics to
the `system` InfluxDB bucket for the **Home Assistant — System** Grafana
dashboard (`uid: ha-system`).

## Why an add-on (not native, not deploy.sh)

On HA OS you don't manage the OS directly, so Telegraf runs as the **Telegraf
add-on** (Home Assistant Community Add-ons). This differs from the Mac, which
runs Telegraf natively via Homebrew + `deploy.sh`. There is **no `deploy.sh`
here** — the add-on holds its own copy of the config + secret. `telegraf.conf`
in this dir is the **canonical reference**: edit it here, then paste it into the
add-on.

> The HA **Companion app** (the phone app) cannot run Telegraf — it only reports
> phone sensors into HA. Host system metrics require Telegraf on the HA box.

## Network

- HA host: `homeassistant.local` → `192.168.4.101`
- Mac mini (InfluxDB): `192.168.5.232:8086`
- Different subnets; the mini is **not** on the tailnet from HA. The config uses
  the mini's **LAN IP** because `.local` mDNS does not resolve across subnets and
  Tailscale isn't available here. If the mini's IP changes, update `telegraf.conf`.

## Setup (HA OS)

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/hassio-addons/repository`, then install **Telegraf**.
   Don't start it yet.
2. **Configuration** tab → paste `telegraf.conf` from this dir.
3. Set the token: replace `${INFLUX_TOKEN}` with the real value (or use the
   add-on's secret/env option). The token is the system-bucket write token —
   stored on the mini as `INFLUX_TELEGRAF_HA_TOKEN` in `influxdb/.env`.
4. **Start** the add-on; watch the **Log** tab. Healthy = periodic flushes, no
   `error writing to outputs.influxdb_v2` (a 401 = wrong token/scope; a
   connection error = wrong URL or the mini unreachable on that IP).

## Verify (on the Mac mini)

```sh
cd /Volumes/dev/observability && source influxdb/.env
docker exec influxdb influx query --org home --token "$INFLUX_ADMIN_TOKEN" \
  'import "influxdata/influxdb/schema"
   schema.tagValues(bucket: "system", tag: "host")'
```

`homeassistant` should appear alongside `mac` / `tommys-mac-mini`. Then open the
**Home Assistant — System** dashboard (its `$host` defaults to `homeassistant`).

## Token

`INFLUX_TOKEN` is a **write-only** token scoped to the `system` bucket, already
minted and stored on the mini as `INFLUX_TELEGRAF_HA_TOKEN` in `influxdb/.env`
(description `telegraf homeassistant write`). To mint another:

```sh
cd /Volumes/dev/observability && source influxdb/.env
SYS_ID=$(docker exec influxdb influx bucket list --org home --token "$INFLUX_ADMIN_TOKEN" \
  --hide-headers --name system | awk '{print $1}')
docker exec influxdb influx auth create --org home --token "$INFLUX_ADMIN_TOKEN" \
  --write-bucket "$SYS_ID" --description "telegraf homeassistant write"
```
