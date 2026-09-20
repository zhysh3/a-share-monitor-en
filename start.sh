#!/bin/bash
# start.sh — one-command start for the A-Share Risk Monitor
# Starts the local proxy (8899) + static server (8788), then opens the dashboard in your default browser.
# Safe to re-run: services already running won’t be started twice.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs
[ -f .env ] && source .env
PY="$DIR/venv/bin/python"
HTTP_PORT=8788
URL="http://localhost:$HTTP_PORT/arisk_monitor_local.html"

is_up() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$1" 2>/dev/null)" != "000" ]; }

# 1) Proxy on 8899 (browser live data + Miaoxiang API go through it)
if is_up "http://localhost:8899/"; then
  echo "✓ Proxy already running (8899)"
else
  nohup "$PY" proxy.py >> logs/proxy.log 2>&1 &
  sleep 1
  echo "✓ Proxy started (8899)"
fi

# 2) Static server on 8788 (dashboard must be served over http, not opened via file://)
if is_up "$URL"; then
  echo "✓ Web server already running ($HTTP_PORT)"
else
  nohup "$PY" -m http.server "$HTTP_PORT" >> logs/http.log 2>&1 &
  sleep 2
  if is_up "$URL"; then
    echo "✓ Web server started ($HTTP_PORT)"
  else
    echo "✗ Web server failed to start. Check logs/http.log — the usual cause is a missing venv (run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt)"
    exit 1
  fi
fi

echo ""
echo "Dashboard URL:$URL"
open "$URL"
