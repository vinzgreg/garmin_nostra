#!/bin/bash
set -e

CONFIG_FILE="${CONFIG_FILE:-/app/config.toml}"

# Read interval_minutes from config — pass path via argv, not shell interpolation
INTERVAL=$(python3 -c "
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(sys.argv[1], 'rb') as f:
    cfg = tomllib.load(f)
print(cfg.get('sync', {}).get('interval_minutes', 60))
" "$CONFIG_FILE")

INTERVAL_S=$(( INTERVAL * 60 ))
echo "garmin-nostra: sync interval = ${INTERVAL} min (${INTERVAL_S}s)"

# Initial sync on startup (non-fatal so the loop always starts)
echo "Running initial sync..."
python3 /app/src/sync.py "$CONFIG_FILE" || echo "Initial sync failed, loop will retry."

# Sleep loop replaces cron — no root required, logs go directly to stdout
while true; do
    echo "Sleeping ${INTERVAL} min until next sync..."
    sleep "${INTERVAL_S}"
    echo "Starting scheduled sync..."
    python3 /app/src/sync.py "$CONFIG_FILE" || echo "Sync failed, will retry next cycle."
done
