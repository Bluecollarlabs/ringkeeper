#!/usr/bin/env bash
# Morning routine: get fresh data, re-export the
# analysis (tools/analysis: nights, stages, baselines, sleep note), re-export the
# dashboard JSON, take a backup, print a short summary + the morning sleep note.
#
# Data source (data/source.json {"mode":"pc"|"phone"}, default pc; env RINGKEEPER_SOURCE overrides):
#   pc     BLE sync the ring into data/oura.db (retrying for up to SYNC_DEADLINE_S)
#   phone  no BLE: rebuild data/phone.db from server/data/ingest.db (tools/phone_db.py,
#          read-only snapshot + `oura redecode`), report last phone upload, warn if > 24 h
# Idempotent: `oura sync` is incremental (cursor in the DB), the export
# rewrites ui-mockup/real-data.json atomically, the backup replaces today's
# snapshot. A lock prevents two concurrent runs.
#
# Usage: tools/morning_sync.sh [--ai]     (env: SYNC_DEADLINE_S=300, RINGKEEPER_OURA_BIN=..., AI_TIMEOUT_S=120)
#   --ai  also write a morning note with the LOCAL LLM (tools/analysis/ai_note.py -> Ollama on
#         127.0.0.1 only; nothing leaves this PC). Never fatal: falls back to the template note.
# Exit: 0 ok · 2 sync failed / phone data missing or > 24 h old (analysis/export/backup still ran
#       on existing data when there was any) ·
#       3 export · 4 backup · 5 analysis failed · other = error
set -uo pipefail
umask 077

USE_AI=0
for arg in "$@"; do
  case "$arg" in
    --ai) USE_AI=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "morning_sync: unknown argument: $arg" >&2; exit 64 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
[[ -f "$ROOT/ringkeeper.env" ]] && source "$ROOT/ringkeeper.env"
cd "$ROOT"
OURA="${RINGKEEPER_OURA_BIN:-$ROOT/upstream/open_oura/target/release/oura}"
KEY="$ROOT/secrets/ring.key"
DEADLINE_S="${SYNC_DEADLINE_S:-300}"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/morning_sync-$(date +%F).log"

exec 9>"$ROOT/data/.morning_sync.lock"
if ! flock -n 9; then echo "morning_sync: another run is in progress" >&2; exit 1; fi

MODE="$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.source_config()["mode"])' "$ROOT/tools/analysis" 2>>"$LOG")" \
  || { echo "morning_sync: bad data/source.json, see $LOG" >&2; exit 1; }
[[ -x "$OURA" ]] || { echo "morning_sync: oura binary not found: $OURA" >&2; exit 1; }

echo "=== $(date -Is) morning_sync (mode $MODE)" >>"$LOG"
sync_ok=0
if [[ "$MODE" == phone ]]; then
  # PHONE mode: the ring is paired to the phone; never touch BLE from here.
  DB="$ROOT/data/phone.db"
  # exit: 0 ok · 4 built but last upload > 24 h ago · 3 no uploads yet · 1 error
  python3 -I "$ROOT/tools/phone_db.py" build 2>>"$LOG" | tee -a "$LOG"; prc=${PIPESTATUS[0]}
  case "$prc" in
    0) sync_ok=1 ;;
    3|4) ;;  # message already printed (no data / stale warning)
    *) echo "phone: rebuilding data/phone.db FAILED (exit $prc), see $LOG" >&2 ;;
  esac
  if [[ ! -f "$DB" ]]; then
    echo "morning_sync: no data/phone.db yet, nothing to analyse" >&2; exit 2
  fi
else
  DB="$ROOT/data/oura.db"
  [[ -f "$KEY" ]] || { echo "morning_sync: missing key $KEY" >&2; exit 1; }
  start=$(date +%s); attempt=0
  while :; do
    attempt=$((attempt + 1))
    left=$(( DEADLINE_S - ($(date +%s) - start) ))
    (( left > 10 )) || break
    echo "--- attempt $attempt ($left s left)" >>"$LOG"
    if timeout --kill-after=10 "$left" "$OURA" --name "" --key-file "$KEY" --db "$DB" sync >>"$LOG" 2>&1; then
      sync_ok=1; break
    fi
    left=$(( DEADLINE_S - ($(date +%s) - start) ))
    (( left > 25 )) || break
    sleep 15
  done
  elapsed=$(( $(date +%s) - start ))
  if (( sync_ok )); then
    echo "sync: ok (attempt $attempt, ${elapsed}s)"
  else
    echo "sync: FAILED after $attempt attempt(s), ${elapsed}s — ring out of range? see $LOG" >&2
  fi
fi

# analysis refresh: $DB -> data/nightly.json (+ staging, baselines) and data/sleep_note.json
NOTE="$(python3 -I "$ROOT/tools/analysis/sleep_note.py" --refresh --db "$DB" 2>>"$LOG")" \
  || { echo "morning_sync: analysis failed, see $LOG" >&2; exit 5; }
printf '%s\n' "$NOTE" >>"$LOG"
if (( USE_AI )); then
  # local LLM note (loopback Ollama only); always exits 0 and falls back to the template note
  AI_NOTE="$(python3 -I "$ROOT/tools/analysis/ai_note.py" note --timeout "${AI_TIMEOUT_S:-120}" 2>>"$LOG")" \
    && [[ -n "$AI_NOTE" ]] && NOTE="$AI_NOTE"
  printf 'ai note:\n%s\n' "${AI_NOTE:-<none>}" >>"$LOG"
fi
python3 -I "$ROOT/tools/export_dashboard.py" --no-refresh --db "$DB" >>"$LOG" 2>&1 \
  || { echo "morning_sync: export failed, see $LOG" >&2; exit 3; }
"$ROOT/tools/backup.sh" >>"$LOG" 2>&1 \
  || { echo "morning_sync: backup failed, see $LOG" >&2; exit 4; }

python3 -I - "$ROOT/ui-mockup/real-data.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
n, s = j['night'], j['summary']
t = lambda iso: iso[11:16]
print(f"Bed {t(n['bed'])} -> wake {t(n['wake'])} ({n['time_in_bed_min'] / 60:.1f} h in bed, {n['bed'][:10]})")
print(f"HR low {s['hr_low']:.0f} bpm at {t(s['hr_low_at'])} (median {s['hr_median']:.0f})")
print(f"Avg RMSSD {s['rmssd_avg']:.0f} ms ({s['rmssd_samples']} x 5-min windows)")
PY
echo
echo "$NOTE"

(( sync_ok )) || exit 2
