#!/bin/zsh
# Deploy the Telegraf host-metrics config: bake the InfluxDB token from
# telegraf/.env into the config and (re)start the Homebrew launchd service.
#
# Why bake instead of run in place: Homebrew's `brew services` launchd job has
# no access to this repo's .env, so the token must be substituted into the
# config it actually reads (/opt/homebrew/etc/telegraf.conf, internal disk).
# That deployed copy holds the real token (chmod 600); the repo copy keeps the
# ${INFLUX_TOKEN} placeholder. This repo file is the source of truth.
set -eu

SRC="${0:A:h}/telegraf.conf"
ENV_FILE="${0:A:h}/.env"
DEST="$(brew --prefix)/etc/telegraf.conf"

[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE (copy .env.example, add the token)"; exit 1; }
set -a; source "$ENV_FILE"; set +a
[[ -n "${INFLUX_TOKEN:-}" ]] || { echo "INFLUX_TOKEN empty in $ENV_FILE"; exit 1; }

# Substitute ONLY ${INFLUX_TOKEN} so nothing else in the config is touched.
envsubst '${INFLUX_TOKEN}' < "$SRC" > "$DEST"
chmod 600 "$DEST"
echo "deployed $SRC -> $DEST"

# Validate the config before (re)starting so a bad edit fails loudly here.
"$(brew --prefix)/opt/telegraf/bin/telegraf" --config "$DEST" --test --once >/dev/null \
  && echo "config test OK" || { echo "telegraf --test failed"; exit 1; }

brew services restart telegraf
echo "restarted telegraf (brew services)"
