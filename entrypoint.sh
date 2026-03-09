#!/bin/bash
set -e

CONFIG_FILE="${CONFIG_FILE:-/app/config.toml}"

# Read interval_minutes from config
INTERVAL=$(python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('${CONFIG_FILE}', 'rb') as f:
    cfg = tomllib.load(f)
print(cfg.get('sync', {}).get('interval_minutes', 60))
")

echo "garmin-nostra: sync interval = ${INTERVAL} min"

# Write crontab
CRON_EXPR="*/${INTERVAL} * * * *"
echo "${CRON_EXPR} python3 /app/src/sync.py /app/config.toml >> /var/log/garmin-nostra.log 2>&1" \
    > /etc/cron.d/garmin-nostra
chmod 0644 /etc/cron.d/garmin-nostra
crontab /etc/cron.d/garmin-nostra

# Initial sync on startup
echo "Running initial sync..."
python3 /app/src/sync.py /app/config.toml

echo "Starting cron daemon..."
cron -f
