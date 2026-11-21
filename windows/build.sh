#!/bin/bash
# Compila BKHD Backup per Windows con mingw-w64:
#   x86 (32 bit): per Pentium III e successivi, solo SSE (niente SSE2), Windows XP SP3 / 7
#   x64 (64 bit): Windows 10 / 11
# Uso: windows/build.sh [x86|x64|tutti]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TP="$HERE/third_party"
WHAT="${1:-tutti}"
VERSION=$(sed -n 's/^#define BKHD_VERSION "\(.*\)"/\1/p' "$HERE/src/util.h")

ZSTD_SRC=$(ls "$TP"/zstd/common/*.c "$TP"/zstd/compress/*.c "$TP"/zstd/decompress/*.c)
MBED_SRC=""
for f in aes gcm cipher cipher_wrap sha256 platform_util constant_time block_cipher; do
  MBED_SRC="$MBED_SRC $TP/mbedtls/library/$f.c"
done
ENGINE_SRC="util.c json.c crypto.c disk.c rclone.c report.c store.c engine.c"

build() {
  local arch="$1" cc cflags ldflags windres
  case "$arch" in
    x86)
      cc=i686-w64-mingw32-gcc; windres=i686-w64-mingw32-windres
      cflags="-march=pentium3 -mtune=pentium3 -mno-sse2 -mfpmath=387"
      ldflags="-Wl,--large-address-aware -Wl,--major-subsystem-version,5,--minor-subsystem-version,1 \
-Wl,--major-os-version,5,--minor-os-version,1"
      ;;
    x64)
      cc=x86_64-w64-mingw32-gcc; windres=x86_64-w64-mingw32-windres
      cflags="-march=x86-64 -mtune=generic"
      ldflags="-Wl,--major-subsystem-version,6,--minor-subsystem-version,1"
      ;;
  esac
  local obj="$HERE/build/$arch" out="$ROOT/dist/windows/$arch"
  mkdir -p "$obj" "$out"
  local common="-O2 -std=gnu11 -ffunction-sections -fdata-sections -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0501 -DWINVER=0x0501 \
-DMBEDTLS_CONFIG_FILE=\"bkhd_mbedtls_config.h\" -DZSTD_DISABLE_ASM -DZSTD_LEGACY_SUPPORT=0 \
-I$HERE/src -I$TP/zstd -I$TP/zstd/common -I$TP/mbedtls/include -I$TP/mbedtls/library $cflags"
  echo "== $arch: librerie"
  local objs=()
  for f in $ZSTD_SRC $MBED_SRC; do
    local o="$obj/lib_$(basename "${f%.c}").o"
    if [ ! -f "$o" ] || [ "$f" -nt "$o" ] || [ "$HERE/src/bkhd_mbedtls_config.h" -nt "$o" ]; then
      $cc $common -w -c "$f" -o "$o"
    fi
    objs+=("$o")
  done
  echo "== $arch: programma"
  for f in $ENGINE_SRC cli.c gui.c; do
    [ -f "$HERE/src/$f" ] || continue
    $cc $common -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -c "$HERE/src/$f" \
        -o "$obj/${f%.c}.o"
  done
  local eng=()
  for f in $ENGINE_SRC; do eng+=("$obj/${f%.c}.o"); done
  $windres -DBKHD_ARCH_$arch -I"$HERE/res" "$HERE/res/bkhd.rc" -O coff -o "$obj/res.o"
  local libs="-lshell32 -lcomctl32 -lcomdlg32 -lole32 -luuid -ladvapi32 -lgdi32 -luser32 -lkernel32"
  $cc -static -s -Wl,--gc-sections -mconsole -o "$out/bkhd-cli.exe" "$obj/cli.o" "${eng[@]}" "${objs[@]}" "$obj/res.o" $ldflags $libs
  if [ -f "$obj/gui.o" ]; then
    $cc -static -s -Wl,--gc-sections -mwindows -o "$out/bkhd-backup.exe" "$obj/gui.o" "${eng[@]}" "${objs[@]}" "$obj/res.o" \
        $ldflags $libs
  fi
  if [ "$arch" = x86 ]; then
    for e in "$out"/*.exe; do python3 "$HERE/tools/check_sse.py" "$e"; done
  fi
  ls -la "$out"
}

case "$WHAT" in
  x86|x64) build "$WHAT" ;;
  tutti) build x86; build x64 ;;
  *) echo "uso: $0 [x86|x64|tutti]"; exit 1 ;;
esac
echo "BKHD Backup per Windows $VERSION compilato in $ROOT/dist/windows"
