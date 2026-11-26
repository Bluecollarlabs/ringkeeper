#!/usr/bin/env bash
# Fetch the open_oura protocol library (MIT, https://github.com/Th0rgal/open_oura) at the
# commit RingKeeper is tested against, into upstream/open_oura.
#
#   scripts/fetch-upstream.sh            # submodule init (or plain clone if not a git checkout)
#   scripts/fetch-upstream.sh --patch    # also apply the optional BlueZ 5.87 workaround (patches/)
#   scripts/fetch-upstream.sh --build    # also build the `oura` CLI (needs Rust; see core/TOOLCHAIN.md)
#
# upstream/ is third-party code: read it before you run it, and read the diff before bumping PIN.
set -euo pipefail

PIN=e57c43ce5fe0cf9a646afa790bda3731be767507
URL=https://github.com/Th0rgal/open_oura
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/upstream/open_oura"
PATCH=0 BUILD=0
for a in "$@"; do
  case "$a" in
    --patch) PATCH=1 ;;
    --build) BUILD=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "fetch-upstream: unknown argument: $a" >&2; exit 64 ;;
  esac
done

cd "$ROOT"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 && [[ -f "$ROOT/.gitmodules" ]]; then
  git submodule update --init upstream/open_oura
else
  [[ -d "$DEST/.git" ]] || git clone "$URL" "$DEST"
fi
git -C "$DEST" fetch -q origin "$PIN" 2>/dev/null || true
git -C "$DEST" checkout -q "$PIN"
echo "open_oura at $(git -C "$DEST" rev-parse --short HEAD)"

if [[ $PATCH == 1 ]]; then
  for p in "$ROOT"/patches/*.patch; do
    if git -C "$DEST" apply --check "$p" 2>/dev/null; then
      git -C "$DEST" apply "$p"; echo "applied $(basename "$p")"
    else
      echo "skipped $(basename "$p") (already applied or does not apply)"
    fi
  done
fi

if [[ $BUILD == 1 ]]; then
  ( cd "$DEST" && cargo build --release --locked )
  echo "CLI: $DEST/target/release/oura"
fi
