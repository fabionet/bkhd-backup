#!/bin/bash
# Crea gli zip della release di BKHD Backup per Windows.
# Uso: RCLONE_X64=/percorso/rclone.exe RCLONE_X86=/percorso/rclone.exe windows/package.sh
#   RCLONE_X64: rclone ufficiale per windows-amd64 (https://rclone.org/downloads/)
#   RCLONE_X86: rclone per windows-386 compilato senza SSE2 (vedi rclone-x86-sse.txt):
#               Go 1.20.14, GOOS=windows GOARCH=386 GO386=softfloat CGO_ENABLED=0, rclone v1.67.0
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VERSION=$(sed -n 's/^#define BKHD_VERSION "\(.*\)"/\1/p' "$HERE/src/util.h")
: "${RCLONE_X64:?serve RCLONE_X64}" "${RCLONE_X86:?serve RCLONE_X86}"
bash "$HERE/build.sh" tutti
OUT="$ROOT/dist/windows/release"
rm -rf "$OUT"
mkdir -p "$OUT"
crlf() { sed 's/\r*$/\r/' "$1" > "$2"; }      # testi leggibili anche con il Blocco note di Windows XP

for arch in x64 x86; do
  label=$([ "$arch" = x64 ] && echo x64 || echo x86-sse)
  name="bkhd-backup-windows-$VERSION-$label"
  d="$OUT/$name"
  mkdir -p "$d/licenses"
  cp "$ROOT/dist/windows/$arch/bkhd-backup.exe" "$ROOT/dist/windows/$arch/bkhd-cli.exe" "$d/"
  if [ "$arch" = x64 ]; then cp "$RCLONE_X64" "$d/rclone.exe"; else cp "$RCLONE_X86" "$d/rclone.exe"; fi
  crlf "$HERE/LEGGIMI.txt" "$d/LEGGIMI.txt"
  crlf "$ROOT/LICENSE" "$d/LICENSE.txt"
  for f in "$HERE"/licenses/*.txt; do crlf "$f" "$d/licenses/$(basename "$f")"; done
  if [ "$arch" = x86 ]; then
    sum=$(sha256sum "$RCLONE_X86" | cut -d' ' -f1)
    sed "s/@SHA256@/$sum/" "$HERE/rclone-x86-sse.txt" > "$d/rclone-x86-sse.tmp"
    crlf "$d/rclone-x86-sse.tmp" "$d/rclone-x86-sse.txt"
    rm "$d/rclone-x86-sse.tmp"
  fi
  (cd "$OUT" && zip -q -9 -r "$name.zip" "$name")
  rm -rf "$d"
done
(cd "$OUT" && sha256sum *.zip > SHA256SUMS && cat SHA256SUMS && ls -la)
