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

# Write crontab (log to stdout/stderr so docker logs shows cron runs)
CRON_EXPR="*/${INTERVAL} * * * *"
echo "${CRON_EXPR} root /usr/local/bin/python3 /app/src/sync.py /app/config.toml >> /proc/1/fd/1 2>> /proc/1/fd/2" \
    > /etc/cron.d/garmin-nostra
chmod 0644 /etc/cron.d/garmin-nostra

# Initial sync on startup (non-fatal so cron daemon always starts)
echo "Running initial sync..."
python3 /app/src/sync.py /app/config.toml || echo "Initial sync failed, cron will retry."

echo "Starting cron daemon..."
cron -f
