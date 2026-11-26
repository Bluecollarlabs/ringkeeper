#!/usr/bin/env bash
# Mint an ingest token for one phone, restricted to one ring serial, and write it ONLY to
# secrets/<DEVICE>.token (dir 700, file 600). The token is never printed. Also prints the APK
# path and the health/ingest URLs built from your config (ringkeeper.env, see config.example.env).
#
# Usage: DEVICE=my-phone SERIAL=<your ring serial> tools/phone_setup.sh
#   Then copy the token onto the phone without a cloud messenger, e.g.
#     qrencode -t ansiutf8 < secrets/my-phone.token     (scan, paste into RingKeeper → Save token)
#   and clear the terminal afterwards (clear && printf '\e[3J').
# Rotate: remove the device entry from server/tokens.json, trash secrets/<DEVICE>.token, re-run.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=/dev/null
[[ -f "$ROOT/ringkeeper.env" ]] && source "$ROOT/ringkeeper.env"

case "${1:-}" in -h|--help) sed -n '2,10p' "$0"; exit 0 ;; "") ;; *) echo "phone_setup: unknown argument: $1" >&2; exit 64 ;; esac

DEVICE="${DEVICE:-my-phone}"
SERIAL="${SERIAL:-${RINGKEEPER_RING_SERIAL:-}}"
if [[ -z "$SERIAL" ]]; then
  echo "phone_setup: set SERIAL=<ring serial> (or RINGKEEPER_RING_SERIAL in ringkeeper.env)" >&2
  exit 64
fi
TOKEN_FILE="$ROOT/secrets/$DEVICE.token"
APK="$ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
PUBLIC_HOST="${RINGKEEPER_PUBLIC_HOST:-your-server.your-tailnet.ts.net}"
PUBLIC_PORT="${RINGKEEPER_INGEST_PUBLIC_PORT:-8444}"
HEALTH_URL="https://$PUBLIC_HOST:$PUBLIC_PORT/v1/health"
INGEST_URL="https://$PUBLIC_HOST:$PUBLIC_PORT/v1/events"

mkdir -p "$ROOT/secrets"; chmod 700 "$ROOT/secrets"
if [[ -s "$TOKEN_FILE" ]]; then
  echo "token: $TOKEN_FILE already exists (not re-minted; see --help to rotate)"
else
  # mint-token prints the raw token on stdout (once) and a note on stderr; capture stdout straight
  # into the file (never into a variable or the terminal). Only the sha256 lands in server/tokens.json.
  tmp="$(mktemp "$ROOT/secrets/.$DEVICE.token.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  if ! python3 -I "$ROOT/server/ingest.py" mint-token --name "$DEVICE" --serial "$SERIAL" >"$tmp" 2>/dev/null; then
    echo "phone_setup: mint-token failed (device '$DEVICE' already in server/tokens.json? remove it to rotate)" >&2
    exit 1
  fi
  # sanity: exactly one urlsafe token line, no output of its value
  if [[ "$(wc -l <"$tmp")" -ne 1 ]] || ! grep -Eq '^[A-Za-z0-9_-]{40,}$' "$tmp"; then
    echo "phone_setup: unexpected mint-token output; token file not written" >&2
    exit 1
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$TOKEN_FILE"
  trap - EXIT
  echo "token: minted for device '$DEVICE' (serial $SERIAL) -> $TOKEN_FILE (600, $(wc -c <"$TOKEN_FILE") bytes)"
fi

if [[ -f "$APK" ]]; then
  echo "apk:   $APK ($(date -r "$APK" '+%F %H:%M'))"
else
  echo "apk:   $APK (missing: cd android && ./gradlew assembleDebug)"
fi
echo "health URL (open on the phone, Tailscale on): $HEALTH_URL"
echo "ingest URL (paste into the app's Setup):     $INGEST_URL"
if command -v systemctl >/dev/null; then
  echo "ringkeeper-ingest service: $(systemctl --user is-active ringkeeper-ingest 2>/dev/null || true)"
fi
