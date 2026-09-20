#!/bin/bash
# check_and_update.sh — called hourly by launchd to decide whether arisk_data.json needs a fresh fetch
# (macOS; paths relative to script dir, python from venv)
#
# The check uses the latest trading date inside the data, not the file modification time:
#   1. JSON missing                              → update
#   2. turnover.date behind"the expected latest trading day"    → update
#   3. data date is current                      → skip (close data won’t change)
#
# "the expected latest trading day" = most recent weekday; before 18:00 it counts as the previous weekday.
# If the data date is still behind after updating and $EXP is over a day old with no data, it is treated as a non-trading day and logged to
# .arisk_nontrading_dates, then skipped automatically; if $EXP is today, it is not blacklisted and will retry next hour.

DIR="$(cd "$(dirname "$0")" && pwd)"
JSON="$DIR/arisk_data.json"
LOG="$DIR/arisk_update.log"
NONTRADING="$DIR/.arisk_nontrading_dates"
UPDATER="$DIR/run_arisk_update.sh"
PY="$DIR/venv/bin/python"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

expected_date() {
    "$PY" - "$NONTRADING" <<'PY'
import sys, datetime
try:
    skip = {l.strip() for l in open(sys.argv[1]) if l.strip()}
except FileNotFoundError:
    skip = set()
now = datetime.datetime.now()
d = now.date()
if now.hour < 18:                              # Before close + publish buffer, today’s data shouldn’t exist yet
    d -= datetime.timedelta(days=1)
for _ in range(30):
    if d.weekday() < 5 and d.isoformat() not in skip:
        break
    d -= datetime.timedelta(days=1)
print(d.isoformat())
PY
}

data_date() {
    "$PY" - "$JSON" <<'PY'
import sys, json
try:
    print((json.load(open(sys.argv[1])).get('turnover') or {}).get('date') or '')
except Exception:
    print('')
PY
}

if [ ! -f "$JSON" ]; then
    log "check: json missing → update"
    exec "$UPDATER"
fi

EXP=$(expected_date)
CUR=$(data_date)

if [ -n "$CUR" ] && [[ ! "$CUR" < "$EXP" ]]; then
    log "check: skip — data date $CUR covers the expected trading day $EXP"
    exit 0
fi

log "check: data date ${CUR:-<missing>} is behind the expected trading day $EXP → update"
"$UPDATER"
RC=$?

if [ "$RC" -ne 0 ]; then
    log "check: update script failed (exit $RC), retrying next hour"
    exit "$RC"
fi

NEW=$(data_date)
TODAY=$(date +%F)
if [ -n "$NEW" ] && [[ "$NEW" < "$EXP" ]]; then
    if [[ "$EXP" < "$TODAY" ]]; then
        grep -qxF "$EXP" "$NONTRADING" 2>/dev/null || echo "$EXP" >> "$NONTRADING"
        log "check: after update, data date is still $NEW, and $EXP is over a day old with no data; treated as a non-trading day and logged to $(basename $NONTRADING)"
    else
        log "check: after update, data date is still $NEW, $EXP is today; source not published yet, retrying next hour (not blacklisted)"
    fi
else
    log "check: update complete, data date ${NEW:-<missing>}"
fi
