#!/bin/bash
# run_arisk_update.sh — run one data update (macOS)
# Called by check_and_update.sh , or run manually: bash run_arisk_update.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/arisk_update.log"
PY="$DIR/venv/bin/python"

exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Update started ==="

# Load MX_APIKEY (optional); works without it, social financing falls back to PBoC direct + AKShare
[ -f "$DIR/.env" ] && source "$DIR/.env"
if [ -z "$MX_APIKEY" ]; then
  echo "  (MX_APIKEY not set → PBoC direct / AKShare fallback, nothing breaks)"
fi

cd "$DIR"
"$PY" update_arisk_data.py

echo "=== done ==="
