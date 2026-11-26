#!/usr/bin/env bash
# Snapshot data/oura.db (PC sync) and server/data/ingest.db (phone uploads; the only copy
# once the ring is paired to a phone) via the SQLite online-backup API (consistent even while
# the DB is open, e.g. by the ringkeeper-ingest service), plus secrets/ring.key into $RINGKEEPER_BACKUP_DIR/YYYY-MM-DD/
# (default ~/backups/ringkeeper; set it in ringkeeper.env, see config.example.env).
# Dirs 700, files 600. Keeps 14 days of snapshots. Re-running the same day
# replaces that day's snapshot (idempotent).
#
# "Encrypted-at-rest friendly": plain files in one private tree, so the whole
# ~/backups/ringkeeper dir can be put on an encrypted volume or synced with an
# encrypting tool (restic/age/borg) without special handling.
#
# Usage: tools/backup.sh [DEST_ROOT]   (default $RINGKEEPER_BACKUP_DIR or ~/backups/ringkeeper)
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
[[ -f "$ROOT/ringkeeper.env" ]] && source "$ROOT/ringkeeper.env"
DEST_ROOT="${1:-${RINGKEEPER_BACKUP_DIR:-$HOME/backups/ringkeeper}}"
KEEP_DAYS=14
DAY="$(date +%F)"
DEST="$DEST_ROOT/$DAY"
DB="$ROOT/data/oura.db"
INGEST_DB="$ROOT/server/data/ingest.db"
KEY="$ROOT/secrets/ring.key"

[[ "${1:-}" != -* ]] || { sed -n '2,14p' "$0"; exit 0; }
[[ -f "$DB" || -f "$INGEST_DB" ]] || { echo "backup: neither $DB nor $INGEST_DB exists" >&2; exit 1; }

mkdir -p "$DEST_ROOT"
chmod 700 "$DEST_ROOT"
TMP="$(mktemp -d "$DEST_ROOT/.tmp-$DAY-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

snap() {  # snap SRC DEST_NAME
python3 -I - "$1" "$TMP/$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
n = dst.execute('SELECT COUNT(*) FROM events').fetchone()[0]
dst.close(); src.close()
if ok != 'ok':
    sys.exit(f'backup: integrity_check failed: {ok}')
print(f'{sys.argv[2].rsplit("/", 1)[-1]} ok, {n} events')
PY
}
if [[ -f "$DB" ]]; then snap "$DB" oura.db; else echo "backup: note: $DB not found (phone-only setup?)" >&2; fi
if [[ -f "$INGEST_DB" ]]; then snap "$INGEST_DB" ingest.db; fi

if [[ -f "$KEY" ]]; then
  cp -p "$KEY" "$TMP/ring.key"
else
  echo "backup: warning: $KEY not found, key not backed up" >&2
fi
( cd "$TMP" && sha256sum -- * > SHA256SUMS )
chmod 600 "$TMP"/*
chmod 700 "$TMP"

# atomic-ish swap into place
if [[ -d "$DEST" ]]; then
  OLD="$DEST_ROOT/.old-$DAY-$$"
  mv "$DEST" "$OLD"
  mv "$TMP" "$DEST"
  rm -rf "$OLD"
else
  mv "$TMP" "$DEST"
fi
trap - EXIT

# retention: drop dated snapshot dirs older than KEEP_DAYS
CUTOFF="$(date -d "-$KEEP_DAYS days" +%F)"
for d in "$DEST_ROOT"/????-??-??; do
  [[ -d "$d" ]] || continue
  b="$(basename "$d")"
  if [[ "$b" < "$CUTOFF" ]]; then rm -rf -- "$d"; echo "pruned $b"; fi
done

echo "backup: $DEST ($(du -sh "$DEST" | cut -f1))"
