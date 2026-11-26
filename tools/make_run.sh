#!/usr/bin/env bash
# Pack a `flet build linux` output directory into a self-extracting .run installer.
# Usage: tools/make_run.sh <build_dir> <version> [exe_name] [out_dir]
set -euo pipefail
BUILD="${1:?build dir (build/linux)}"
VERSION="${2:?version}"
EXE="${3:-}"
OUT="${4:-dist}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "$EXE" ]; then   # the launcher is the only executable file at the top level (no .so)
  EXE="$(find "$BUILD" -maxdepth 1 -type f -perm -u+x ! -name '*.so*' -printf '%f\n' | head -n1)"
fi
[ -n "$EXE" ] && [ -f "$BUILD/$EXE" ] || { echo "executable not found in $BUILD" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/payload" "$OUT"
cp -a "$BUILD"/. "$WORK/payload"/
cp "$ROOT/installer/linux/localdrop-512.png" "$WORK/payload/localdrop-icon.png"
tar -C "$WORK/payload" -czf "$WORK/payload.tar.gz" .
SHA="$(sha256sum "$WORK/payload.tar.gz" | cut -d' ' -f1)"
RUN="$OUT/LocalDrop-${VERSION}-x86_64.run"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@EXE@/$EXE/g" -e "s/@SHA256@/$SHA/g" \
    "$ROOT/installer/linux/install-header.sh" > "$RUN"
cat "$WORK/payload.tar.gz" >> "$RUN"
chmod +x "$RUN"
echo "Created $RUN ($(du -h "$RUN" | cut -f1)), launcher: $EXE"
