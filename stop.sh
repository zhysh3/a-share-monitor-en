#!/bin/bash
# stop.sh — stop the local proxy (8899) and static server (8788)
for port in 8899 8788; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill 2>/dev/null || true
    echo "✓ Stopped port $port"
  else
    echo "· port $port not running"
  fi
done
