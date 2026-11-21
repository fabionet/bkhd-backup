#!/bin/bash
# Compatibilita' tra BKHD Backup per Windows (eseguito con Wine) e il motore Linux:
# backup fatti su Windows e ripristinati su Linux e viceversa, con compressione, password, settori
# illeggibili simulati, interruzione e ripresa incrociata, solo settori usati, elenco e verifiche.
#
# Uso: tests/windows_compat.sh        (serve wine; gli eseguibili in dist/windows/{x86,x64})
#      BKHD_RCLONE_WIN64=...\rclone.exe BKHD_RCLONE_WIN32=...\rclone.exe tests/windows_compat.sh
#      aggiunge le prove del cloud (rclone ufficiale a 64 bit e rclone a 32 bit senza SSE2)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIN="python3 $ROOT/src/bkhd_engine.py"
W32="$ROOT/dist/windows/x86/bkhd-cli.exe"
W64="$ROOT/dist/windows/x64/bkhd-cli.exe"
export WINEDEBUG=-all
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
PW="nuvola-azzurra-2026"
FAILS=0
pass() { echo "  OK  $1"; }
fail() { echo "  ERRORE  $1"; FAILS=$((FAILS + 1)); }
win() { winepath -w "$1" 2>/dev/null; }
w() { local exe="$1"; shift; wine "$exe" "$@"; }

echo "== Preparazione immagine di prova (48 MB: dati casuali, zone vuote, testo)"
IMG="$T/disk.img"
{ head -c 12M /dev/urandom; head -c 12M /dev/zero; head -c 12M /dev/urandom; yes "BKHD Backup per Windows" | head -c 12M; } > "$IMG"
WIMG="$(win "$IMG")"
mkdir -p "$T/d"
WD="$(win "$T/d")"

echo "== 1. Windows 32 bit -> Linux: senza compressione, pacchetti da 10 MB"
w "$W32" backup --source "$WIMG" --remote "$WD" --name w32raw --chunk-size 10M > "$T/1.log" 2>&1 \
  && pass "backup Windows 32 bit" || { fail "backup Windows 32 bit"; tail -5 "$T/1.log"; }
$LIN restore --remote "$T/d" --path BKHD/w32raw --target "$T/out1.img" --yes > "$T/1r.log" 2>&1 \
  && cmp -s "$IMG" "$T/out1.img" && pass "ripristino Linux identico all'originale" || { fail "ripristino Linux"; tail -3 "$T/1r.log"; }

echo "== 2. Windows 64 bit -> Linux: zstd + password + settore illeggibile simulato"
echo "$PW" | BKHD_TEST_BAD_SECTORS=5000192 w "$W64" --password-stdin backup --source "$WIMG" --remote "$WD" \
  --name w64enc --chunk-size 7M --compression zstd --level 5 --encrypt > "$T/2.log" 2>&1 \
  && grep -q "Settore illeggibile all'offset 5000192" "$T/2.log" && pass "backup protetto con settore illeggibile" \
  || { fail "backup protetto"; tail -5 "$T/2.log"; }
cp "$IMG" "$T/expect2.img"
dd if=/dev/zero of="$T/expect2.img" bs=512 seek=$((5000192 / 512)) count=1 conv=notrunc status=none
echo "$PW" | $LIN --password-stdin restore --remote "$T/d" --path BKHD/w64enc --target "$T/out2.img" --yes \
  > "$T/2r.log" 2>&1 && cmp -s "$T/expect2.img" "$T/out2.img" \
  && pass "Linux verifica firme e sigilli di Windows e ripristina (settore illeggibile = zeri)" \
  || { fail "ripristino Linux del backup protetto"; tail -3 "$T/2r.log"; }
python3 - "$T/d/BKHD/w64enc/manifest.json" <<'EOF' && pass "manifest: settori illeggibili e cifratura registrati" || fail "manifest Windows"
import json, sys
m = json.load(open(sys.argv[1]))
assert m["bad_sectors_total"] == 1 and m["encryption"]["alg"] == "aes-256-gcm" and m["status"] == "complete"
assert m["engine"].startswith("win-") and m["source"]["platform"] == "windows"
EOF
echo "sbagliata-123" | $LIN --password-stdin restore --remote "$T/d" --path BKHD/w64enc --verify-only > "$T/2x.log" 2>&1
grep -q "password errata" "$T/2x.log" && pass "Linux rifiuta la password errata" || fail "password errata (Linux)"

echo "== 3. Linux -> Windows: zstd + password"
echo "$PW" | $LIN --password-stdin backup --source "$IMG" --remote "$T/d" --path BKHD --name linenc \
  --chunk-size 9M --compression zstd --encrypt > "$T/3.log" 2>&1 && pass "backup Linux protetto" \
  || { fail "backup Linux"; tail -3 "$T/3.log"; }
echo "$PW" | w "$W32" --password-stdin restore --remote "$WD" --path BKHD/linenc --target "$(win "$T/out3.img")" \
  --yes > "$T/3r.log" 2>&1 && cmp -s "$IMG" "$T/out3.img" && pass "Windows 32 bit ripristina il backup Linux protetto" \
  || { fail "ripristino Windows 32 bit"; tail -5 "$T/3r.log"; }
echo "$PW" | w "$W64" --password-stdin restore --remote "$WD" --path BKHD/linenc --verify-only > "$T/3v.log" 2>&1 \
  && grep -q "integri" "$T/3v.log" && pass "Windows 64 bit verifica il backup Linux" || { fail "verifica Windows 64"; tail -3 "$T/3v.log"; }
echo "sbagliata-123" | w "$W64" --password-stdin restore --remote "$WD" --path BKHD/linenc --verify-only > "$T/3x.log" 2>&1
grep -q "password errata" "$T/3x.log" && pass "Windows rifiuta la password errata" || fail "password errata (Windows)"

echo "== 4. Pacchetto manomesso: Windows se ne accorge"
cp -r "$T/d/BKHD/linenc" "$T/d/BKHD/manomesso"
f=$(ls "$T/d/BKHD/manomesso"/*.part00002.bkhd)
printf '\xff' | dd of="$f" bs=1 seek=5000 conv=notrunc status=none
echo "$PW" | w "$W64" --password-stdin restore --remote "$WD" --path BKHD/manomesso --verify-only > "$T/4.log" 2>&1
grep -q "danneggiato" "$T/4.log" && pass "pacchetto alterato rifiutato" || { fail "manomissione non rilevata"; tail -3 "$T/4.log"; }

echo "== 5. Interruzione su Windows e ripresa incrociata"
BKHD_TEST_CANCEL_AFTER=2 w "$W32" backup --source "$WIMG" --remote "$WD" --name ripresa --chunk-size 5M \
  --compression zstd > "$T/5a.log" 2>&1
grep -q "Indice salvato" "$T/5a.log" && pass "annullamento con indice salvato" || { fail "annullamento"; tail -3 "$T/5a.log"; }
w "$W64" backup --source "$WIMG" --remote "$WD" --name ripresa --chunk-size 5M --compression zstd --resume \
  > "$T/5b.log" 2>&1 && grep -q "Ripresa del backup interrotto dal pacchetto 3/" "$T/5b.log" \
  && pass "ripreso da Windows 64 bit dal pacchetto 3" || { fail "ripresa Windows"; tail -3 "$T/5b.log"; }
$LIN restore --remote "$T/d" --path BKHD/ripresa --target "$T/out5.img" --yes > "$T/5r.log" 2>&1 \
  && cmp -s "$IMG" "$T/out5.img" && pass "backup ripreso ripristinato da Linux" || { fail "ripristino ripresa"; tail -3 "$T/5r.log"; }
echo "$PW" | BKHD_TEST_CANCEL_AFTER=3 w "$W32" --password-stdin backup --source "$WIMG" --remote "$WD" \
  --name ripresa-enc --chunk-size 4M --encrypt > "$T/5c.log" 2>&1
echo "$PW" | $LIN --password-stdin backup --source "$IMG" --remote "$T/d" --path BKHD --name ripresa-enc \
  --chunk-size 4M --encrypt --resume > "$T/5d.log" 2>&1 && grep -q "dal pacchetto 4/" "$T/5d.log" \
  && pass "backup protetto interrotto su Windows ripreso da Linux (firme compatibili)" \
  || { fail "ripresa Linux di backup Windows"; tail -3 "$T/5d.log"; }
echo "$PW" | w "$W64" --password-stdin restore --remote "$WD" --path BKHD/ripresa-enc --target "$(win "$T/out5e.img")" \
  --yes > "$T/5e.log" 2>&1 && cmp -s "$IMG" "$T/out5e.img" && pass "ripristinato da Windows 64 bit" \
  || { fail "ripristino Windows della ripresa Linux"; tail -3 "$T/5e.log"; }

echo "== 6. Solo settori usati (backup Linux di un file system ext4) -> Windows"
mkdir -p "$T/fs"; head -c 3M /dev/urandom > "$T/fs/dati.bin"; yes "file di prova" | head -c 2M > "$T/fs/testo.txt"
truncate -s 64M "$T/ext4.img"
if mkfs.ext4 -q -F -d "$T/fs" "$T/ext4.img" 2>/dev/null; then
  $LIN backup --source "$T/ext4.img" --remote "$T/d" --path BKHD --name usati --chunk-size 8M --used-only \
    > "$T/6.log" 2>&1
  if grep -q '"sparse": true' "$T/d/BKHD/usati/manifest.json" 2>/dev/null; then
    w "$W64" restore --remote "$WD" --path BKHD/usati --target "$(win "$T/out6.img")" --yes > "$T/6r.log" 2>&1 \
      && cmp -s "$T/ext4.img" "$T/out6.img" && pass "backup 'solo settori usati' ripristinato da Windows" \
      || { fail "ripristino solo settori usati"; tail -3 "$T/6r.log"; }
  else
    echo "  (il motore Linux non applica 'solo settori usati' a questo file: prova saltata)"; tail -2 "$T/6.log"
  fi
fi

echo "== 7. Elenco dei backup"
w "$W32" list --remote "$WD" > "$T/7.log" 2>&1
n=$(grep -c "BKHD" "$T/7.log")
[ "$n" -ge 6 ] && grep -q "complete" "$T/7.log" && pass "Windows elenca $n backup (anche quelli Linux)" || { fail "elenco"; cat "$T/7.log"; }
$LIN list --remote "$T/d" --path BKHD > "$T/7b.log" 2>&1 && grep -q "w64enc" "$T/7b.log" \
  && pass "Linux elenca i backup di Windows" || fail "elenco Linux"

echo "== 8. Errori gestiti"
w "$W64" backup --source "$WIMG" --remote "$WD" --name w32raw --chunk-size 10M > "$T/8.log" 2>&1
grep -q "esiste gia' un backup completo" "$T/8.log" && pass "nome gia' usato rifiutato" || fail "nome duplicato"
w "$W32" backup --source "$WIMG" --remote "$WD" --name x --chunk-size 1M > "$T/8b.log" 2>&1
grep -q "tra 1,2 MB" "$T/8b.log" && pass "dimensione pacchetti troppo piccola rifiutata" || fail "limite pacchetti"
w "$W32" backup --source "$WIMG" --remote "test:" --name x --chunk-size 800M > "$T/8c.log" 2>&1
grep -q "fino a 700 MB" "$T/8c.log" && pass "32 bit: pacchetti cloud oltre 700 MB rifiutati" || { fail "limite 32 bit"; cat "$T/8c.log"; }

if [ -n "${BKHD_RCLONE_WIN64:-}" ] && [ -n "${BKHD_RCLONE_WIN32:-}" ]; then
  echo "== 9. Cloud con rclone.exe (remote di tipo alias verso una cartella)"
  mkdir -p "$T/cloud"
  printf '[prova]\ntype = alias\nremote = %s\n' "$(win "$T/cloud")" > "$T/rclone.conf"
  for exe in "$W64" "$W32"; do
    arch=$(basename "$(dirname "$exe")")
    RC="$BKHD_RCLONE_WIN64"; [ "$arch" = x86 ] && RC="$BKHD_RCLONE_WIN32"
    echo "$PW" | w "$exe" --rclone "$RC" --rclone-config "$(win "$T/rclone.conf")" --password-stdin \
      backup --source "$WIMG" --remote prova: --name cloud-$arch --chunk-size 6M --compression zstd --encrypt \
      > "$T/9-$arch.log" 2>&1 && grep -q "sul cloud e rimosso dalla RAM" "$T/9-$arch.log" \
      && pass "$arch: backup sul cloud tramite rclone (rcat da RAM)" || { fail "$arch: cloud"; tail -5 "$T/9-$arch.log"; }
    echo "$PW" | w "$exe" --rclone "$RC" --rclone-config "$(win "$T/rclone.conf")" --password-stdin \
      restore --remote prova: --path BKHD/cloud-$arch --target "$(win "$T/out9-$arch.img")" --yes > "$T/9r-$arch.log" 2>&1 \
      && cmp -s "$IMG" "$T/out9-$arch.img" && pass "$arch: ripristino dal cloud (rclone cat in RAM)" \
      || { fail "$arch: ripristino cloud"; tail -5 "$T/9r-$arch.log"; }
    echo "$PW" | $LIN --password-stdin restore --remote "$T/cloud" --path BKHD/cloud-$arch --verify-only > "$T/9l.log" 2>&1 \
      && pass "$arch: backup cloud di Windows verificato da Linux" || { fail "$arch: verifica Linux"; tail -3 "$T/9l.log"; }
  done
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "TUTTI I TEST DI COMPATIBILITA' SUPERATI"; else echo "TEST FALLITI: $FAILS"; exit 1; fi
