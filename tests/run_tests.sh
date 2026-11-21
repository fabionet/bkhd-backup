#!/bin/bash
# Test end-to-end del motore con un finto cloud locale (non richiede root).
# Uso: tests/run_tests.sh [cartella_di_lavoro]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$HERE/../src/bkhd_engine.py"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
export FAKE_RCLONE_ROOT="$WORK/cloud"
RC="--rclone $HERE/fake_rclone.py --retries 2"
pass() { echo "  OK  $*"; }
fail() { echo "  FALLITO  $*"; exit 1; }

echo "== Preparazione immagine di prova (1,5 GB: dati casuali + zone vuote)"
IMG="$WORK/disk.img"
rm -rf "$FAKE_RCLONE_ROOT" "$IMG"
truncate -s 1610612736 "$IMG"
head -c 200M /dev/urandom | dd of="$IMG" conv=notrunc status=none
head -c 100M /dev/urandom | dd of="$IMG" bs=1M seek=900 conv=notrunc status=none
echo "BKHD-FINE-DISCO" | dd of="$IMG" bs=1 seek=$((1610612736-16)) conv=notrunc status=none
ORIG=$(sha256sum "$IMG" | cut -d' ' -f1)

echo "== 1. Backup senza compressione, pacchetti da 650 MB (primo upload fallisce -> retry)"
FAKE_RCLONE_FAIL_FIRST="$WORK/failmark" python3 "$ENGINE" $RC backup --source "$IMG" \
  --remote test: --path BKHD --name prova-raw --chunk-size-mib 650
n=$(ls "$FAKE_RCLONE_ROOT/BKHD/prova-raw/"*.bkhd | wc -l); [ "$n" = 3 ] && pass "3 pacchetti .bkhd sul cloud" || fail "pacchetti: $n"
[ -z "$(ls /dev/shm | grep bkhd- || true)" ] && pass "nessun residuo in RAM" || fail "residui in /dev/shm"

echo "== 2. Ripristino su file e confronto"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/prova-raw --target "$WORK/restored.img" --yes
[ "$(sha256sum "$WORK/restored.img" | cut -d' ' -f1)" = "$ORIG" ] && pass "immagine ripristinata identica" || fail "differenze"

echo "== 3. Backup con zstd, annullato a meta' e poi ripreso"
python3 "$ENGINE" --json $RC backup --source "$IMG" --remote test: --path BKHD --name prova-zstd \
  --chunk-size-mib 650 --compression zstd > "$WORK/events.jsonl" < <(
    while ! grep -q '"chunk_done"' "$WORK/events.jsonl" 2>/dev/null; do sleep 0.2; done; echo cancel; sleep 5) || true
grep -q '"cancelled": true' "$WORK/events.jsonl" && pass "annullamento ricevuto" || fail "annullamento"
grep -q '"in_progress"' "$FAKE_RCLONE_ROOT/BKHD/prova-zstd/manifest.json" && pass "manifest in_progress" || fail "manifest"
if python3 "$ENGINE" $RC backup --source "$IMG" --remote test: --path BKHD --name prova-zstd \
  --chunk-size-mib 650 --compression zstd >/dev/null 2>&1; then fail "doveva rifiutare senza --resume"; else pass "rifiuta senza --resume"; fi
python3 "$ENGINE" $RC backup --source "$IMG" --remote test: --path BKHD --name prova-zstd \
  --chunk-size-mib 650 --compression zstd --resume | tee "$WORK/resume.log"
grep -q "Ripresa del backup interrotto dal pacchetto 2" "$WORK/resume.log" && pass "ripreso dal pacchetto 2" || fail "ripresa"
du -sh "$FAKE_RCLONE_ROOT/BKHD/prova-zstd"

echo "== 4. Verifica integrita' e ripristino del backup compresso"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/prova-zstd --verify-only
python3 "$ENGINE" $RC restore --remote test: --path BKHD/prova-zstd --target "$WORK/restored2.img" --yes
[ "$(sha256sum "$WORK/restored2.img" | cut -d' ' -f1)" = "$ORIG" ] && pass "immagine ripristinata identica" || fail "differenze"

echo "== 5. Rilevamento di un pacchetto corrotto sul cloud"
P="$FAKE_RCLONE_ROOT/BKHD/prova-raw/prova-raw.part00002.bkhd"
printf 'X' | dd of="$P" bs=1 seek=5000000 conv=notrunc status=none
python3 "$ENGINE" --retries 1 --rclone "$HERE/fake_rclone.py" restore --remote test: --path BKHD/prova-raw \
   --verify-only > "$WORK/corrupt.log" 2>&1 || true
grep -q "danneggiato" "$WORK/corrupt.log" && pass "corruzione rilevata" || fail "corruzione non rilevata"

echo "== 6. Pacchetti floppy 1,44 MB, sottocartelle e indice a gruppi"
SMALL="$WORK/small.img"
rm -f "$SMALL"; truncate -s 30M "$SMALL"
head -c 6M /dev/urandom | dd of="$SMALL" conv=notrunc status=none
head -c 2M /dev/urandom | dd of="$SMALL" bs=1M seek=20 conv=notrunc status=none
SORIG=$(sha256sum "$SMALL" | cut -d' ' -f1)
export BKHD_GROUP_THRESHOLD=5 BKHD_INDEX_BATCH=4 BKHD_SAVE_INTERVAL=0
python3 "$ENGINE" $RC backup --source "$SMALL" --remote test: --path BKHD --name floppy \
  --chunk-size 1440K --compression zstd > "$WORK/floppy.log"
D="$FAKE_RCLONE_ROOT/BKHD/floppy"
n=$(find "$D" -name '*.bkhd' | wc -l); [ "$n" = 22 ] && pass "22 pacchetti da 1,44 MB" || fail "pacchetti: $n"
ls "$D" | grep -q '^g00000$' && ls "$D/g00000" | grep -q 'floppy.part00001.bkhd' && pass "sottocartelle g00000..." || fail "sottocartelle"
[ "$(ls "$D/index" | wc -l)" = 6 ] && pass "6 file indice da 4 pacchetti" || fail "indice: $(ls "$D/index")"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/floppy --target "$WORK/floppy-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/floppy-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino floppy identico" || fail "differenze"

echo "== 7. Annullamento e ripresa con pacchetti piccoli"
python3 "$ENGINE" --json $RC backup --source "$SMALL" --remote test: --path BKHD --name floppy2 \
  --chunk-size 2880K > "$WORK/ev2.jsonl" < <(
    while [ "$(grep -c '"chunk_done"' "$WORK/ev2.jsonl" 2>/dev/null)" -lt 6 ]; do sleep 0.05; done; echo cancel; sleep 3) || true
python3 "$ENGINE" $RC backup --source "$SMALL" --remote test: --path BKHD --name floppy2 \
  --chunk-size 2880K --resume > "$WORK/resume2.log"
grep -oq "Ripresa del backup interrotto dal pacchetto [6-9]" "$WORK/resume2.log" && pass "$(grep -o 'dal pacchetto [0-9]*/[0-9]*' "$WORK/resume2.log")" || fail "ripresa: $(cat "$WORK/resume2.log" | head -3)"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/floppy2 --target "$WORK/floppy2-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/floppy2-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino dopo ripresa identico" || fail "differenze"

echo "== 8. Compatibilita' con i backup della versione 1.0 (manifest v1)"
python3 - "$FAKE_RCLONE_ROOT/BKHD" <<'PY'
import json, os, shutil, sys
base = sys.argv[1]
src, dst = os.path.join(base, "floppy"), os.path.join(base, "vecchio")
shutil.rmtree(dst, ignore_errors=True); os.makedirs(dst)
m = json.load(open(os.path.join(src, "manifest.json")))
recs = {}
for f in os.listdir(os.path.join(src, "index")):
    for r in json.load(open(os.path.join(src, "index", f)))["chunks"]:
        recs[r["i"]] = r
chunks = []
for i in sorted(recs)[:15]:                    # backup v1 interrotto al pacchetto 15
    r = recs[i]; off = (i - 1) * m["chunk_size"]
    fn = f"vecchio.part{i:05d}.bkhd"
    data = open(os.path.join(src, "g%05d" % ((i - 1) // 1000), f"floppy.part{i:05d}.bkhd"), "rb").read()
    hdr = json.loads(data[6:4096].split(b"\0", 1)[0]); hdr["name"] = "vecchio"; hdr["file"] = fn
    blob = b"BKHD\x01\n" + json.dumps(hdr).encode(); blob += b"\0" * (4096 - len(blob))
    open(os.path.join(dst, fn), "wb").write(blob + data[4096:])
    chunks.append({"index": i, "file": fn, "offset": off, "length": min(m["chunk_size"], m["source"]["size"] - off),
                   "payload_size": r["s"], "raw_sha256": r["r"], "payload_sha256": r.get("p", r["r"]), "bad_sectors": []})
v1 = {k: v for k, v in m.items() if k not in ("layout", "index_batch", "chunks_done", "stored_bytes")}
v1.update(version=1, name="vecchio", status="in_progress", chunks=chunks)
json.dump(v1, open(os.path.join(dst, "manifest.json"), "w"))
PY
python3 "$ENGINE" $RC backup --source "$SMALL" --remote test: --path BKHD --name vecchio \
  --chunk-size 1440K --compression zstd --resume > "$WORK/v1.log"
grep -q "dal pacchetto 16/22" "$WORK/v1.log" && pass "backup v1 ripreso dal pacchetto 16 e convertito" || fail "ripresa v1"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/vecchio --target "$WORK/v1-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/v1-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino del backup convertito identico" || fail "differenze"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/floppy --verify-only > /dev/null && pass "verifica integrita' floppy" || fail "verifica"
if python3 "$ENGINE" $RC backup --source "$SMALL" --remote test: --path BKHD --name x --chunk-size 1000K >/dev/null 2>&1; then
  fail "doveva rifiutare 1000 KB"; else pass "rifiuta pacchetti sotto 1,2 MB"; fi
unset BKHD_GROUP_THRESHOLD BKHD_INDEX_BATCH BKHD_SAVE_INTERVAL

echo "== 9. Destinazione su disco locale (scrittura diretta, senza RAM)"
LOC="$WORK/disco-locale"; rm -rf "$LOC"
python3 "$ENGINE" $RC backup --source "$SMALL" --remote "$LOC" --path BKHD --name locale \
  --chunk-size 2880K --compression zstd > "$WORK/loc.log"
[ "$(find "$LOC/BKHD/locale" -name '*.bkhd' | wc -l)" = 11 ] && pass "11 pacchetti scritti su disco" || fail "pacchetti locali"
[ -z "$(find "$LOC" -name '*.partial' -o -name '*.tmp')" ] && pass "nessun file temporaneo rimasto" || fail "file temporanei"
grep -q "scrittura diretta su disco" "$WORK/loc.log" && pass "nessun passaggio in RAM" || fail "modalita' locale"
python3 "$ENGINE" $RC restore --remote "$LOC" --path BKHD/locale --target "$WORK/loc-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/loc-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino da disco locale identico" || fail "differenze"
python3 "$ENGINE" list --remote "$LOC" --path BKHD | grep -q "complete" && pass "elenco backup locali" || fail "elenco locale"

echo "== 10. Solo settori usati (ext4 + FAT + spazio non partizionato)"
SP="$WORK/sparse"; rm -rf "$SP"; mkdir -p "$SP/files"
SIMG="$SP/disco.img"
head -c 160M /dev/urandom > "$SIMG"                       # spazio libero pieno di "vecchi dati"
printf 'label: dos\nstart=2048, size=131072, type=83\nstart=133120, size=32768, type=c\n' | sfdisk -q "$SIMG"
head -c 3M /dev/urandom > "$SP/files/foto.jpg"; head -c 2M /dev/urandom > "$SP/files/documento.pdf"
mkfs.ext4 -q -F -E offset=1048576 -d "$SP/files" "$SIMG" 64M
mkfs.vfat --offset 133120 "$SIMG" 16384 >/dev/null
python3 "$ENGINE" $RC backup --source "$SIMG" --remote "$LOC" --path BKHD --name usati \
  --chunk-size 8M --compression zstd --used-only > "$WORK/sparse.log"
grep -q "dumpe2fs" "$WORK/sparse.log" && pass "ext4 analizzato con dumpe2fs" || fail "analisi ext4: $(grep -i ext4 "$WORK/sparse.log")"
grep -q "solo spazio libero" "$WORK/sparse.log" && pass "pacchetti vuoti non salvati" || fail "pacchetti vuoti"
[ -f "$LOC/BKHD/usati/usedmap.json" ] && pass "mappa dei settori usati salvata" || fail "usedmap"
used=$(du -sb "$LOC/BKHD/usati" | cut -f1); [ "$used" -lt 40000000 ] && pass "occupati $((used/1048576)) MB invece di 160 MB" || fail "troppo grande: $used"
python3 "$ENGINE" $RC restore --remote "$LOC" --path BKHD/usati --target "$SP/out.img" --yes > /dev/null
e2fsck -fn "$SP/out.img?offset=1048576" > /dev/null 2>&1 && pass "ext4 ripristinato integro (e2fsck)" || fail "e2fsck"
for f in foto.jpg documento.pdf; do
  debugfs -R "dump /$f $SP/rip-$f" "$SP/out.img?offset=1048576" 2>/dev/null
  cmp -s "$SP/files/$f" "$SP/rip-$f" && pass "file $f identico" || fail "file $f"
done
cmp -s <(dd if="$SIMG" bs=512 skip=133120 count=32768 status=none) <(dd if="$SP/out.img" bs=512 skip=133120 count=32768 status=none) \
  && pass "partizione FAT copiata per intero" || fail "FAT"
cmp -s <(dd if="$SIMG" bs=512 count=2048 status=none) <(dd if="$SP/out.img" bs=512 count=2048 status=none) \
  && pass "tabella delle partizioni identica" || fail "MBR"

echo "== 11. Protezione con password"
dd if=/dev/zero of="$SMALL" bs=1 seek=3000000 conv=notrunc status=none count=0
printf 'SEGRETO-BKHD-1234' | dd of="$SMALL" bs=1 seek=25000000 conv=notrunc status=none
SORIG=$(sha256sum "$SMALL" | cut -d' ' -f1)
echo "cavallo-batteria-graffetta" | python3 "$ENGINE" --password-stdin $RC backup --source "$SMALL" --remote test: \
  --path BKHD --name cifrato --chunk-size 2880K --compression zstd --encrypt > "$WORK/enc.log"
! grep -rqa "SEGRETO-BKHD" "$FAKE_RCLONE_ROOT/BKHD/cifrato" && pass "nessun dato in chiaro sul cloud" || fail "dati in chiaro!"
grep -q '"alg": "aes-256-gcm"' "$FAKE_RCLONE_ROOT/BKHD/cifrato/manifest.json" && ! grep -q "cavallo" "$FAKE_RCLONE_ROOT/BKHD/cifrato/manifest.json" \
  && pass "manifest senza password" || fail "manifest"
echo "sbagliata-123" | python3 "$ENGINE" --password-stdin $RC restore --remote test: --path BKHD/cifrato --verify-only > "$WORK/wrong.log" 2>&1 || true
grep -q "password errata" "$WORK/wrong.log" && pass "password errata rifiutata" || fail "password errata: $(tail -1 "$WORK/wrong.log")"
python3 "$ENGINE" $RC restore --remote test: --path BKHD/cifrato --verify-only > "$WORK/nopw.log" 2>&1 < /dev/null || true
grep -q "protetto da password" "$WORK/nopw.log" && pass "senza password rifiutato" || fail "senza password"
echo "cavallo-batteria-graffetta" | python3 "$ENGINE" --password-stdin $RC restore --remote test: --path BKHD/cifrato \
  --target "$WORK/enc-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/enc-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino cifrato identico" || fail "differenze"
python3 - "$FAKE_RCLONE_ROOT/BKHD/cifrato" <<'PY'
import hashlib, json, os, sys
d = sys.argv[1]; fn = os.path.join(d, "cifrato.part00003.bkhd")
data = bytearray(open(fn, "rb").read()); data[5000] ^= 0xFF; open(fn, "wb").write(data)   # manomissione
idx = os.path.join(d, "index", "index-00000.json"); j = json.load(open(idx))
for r in j["chunks"]:
    if r["i"] == 3: r["p"] = hashlib.sha256(bytes(data[4096:])).hexdigest()   # anche l'indice "corretto"
json.dump(j, open(idx, "w"))
PY
echo "cavallo-batteria-graffetta" | python3 "$ENGINE" --retries 1 --password-stdin $RC restore --remote test: \
  --path BKHD/cifrato --verify-only > "$WORK/tamper.log" 2>&1 || true
grep -q "firma non valida" "$WORK/tamper.log" && pass "indice alterato rifiutato (firma HMAC)" || fail "manomissione: $(tail -2 "$WORK/tamper.log")"
python3 - "$ENGINE" "$FAKE_RCLONE_ROOT/BKHD/cifrato" <<'PY' && pass "pacchetto alterato rifiutato dal sigillo AES-GCM" || fail "sigillo AES-GCM"
import json, os, sys
sys.path.insert(0, os.path.dirname(sys.argv[1])); import bkhd_engine as e
d = sys.argv[2]; m = json.load(open(os.path.join(d, "manifest.json")))
c = e.Crypto.open("cavallo-batteria-graffetta", m["encryption"])
fd = os.open(os.path.join(d, "cifrato.part00004.bkhd"), os.O_RDONLY); hdr = e.parse_header(fd)
aad = e.Crypto.aad(m["backup_id"], 4, hdr["offset"], hdr["length"], m["compression"], False)
assert e.gcm_check(fd, c, hdr, aad)                       # pacchetto integro: sigillo valido
mfd = os.memfd_create("t"); data = bytearray(os.pread(fd, 1 << 30, 0)); data[4096 + (len(data) - 4096) // 2] ^= 1; os.write(mfd, data)
assert not e.gcm_check(mfd, c, hdr, aad)                  # un solo bit alterato: rifiutato
assert not e.gcm_check(fd, c, hdr, e.Crypto.aad(m["backup_id"], 5, hdr["offset"], hdr["length"], "zstd", False))
PY
python3 "$ENGINE" --json --password-stdin $RC backup --source "$SMALL" --remote test: --path BKHD --name cifrato2 \
  --chunk-size 2880K --encrypt > "$WORK/ev3.jsonl" < <(echo "cavallo-batteria-graffetta"
    while [ "$(grep -c '"chunk_done"' "$WORK/ev3.jsonl" 2>/dev/null)" -lt 4 ]; do sleep 0.05; done; echo cancel; sleep 3) || true
echo "cavallo-batteria-graffetta" | python3 "$ENGINE" --password-stdin $RC backup --source "$SMALL" --remote test: \
  --path BKHD --name cifrato2 --chunk-size 2880K --resume > "$WORK/res3.log"
grep -q "Ripresa del backup interrotto" "$WORK/res3.log" && pass "backup cifrato ripreso" || fail "ripresa cifrata"
echo "cavallo-batteria-graffetta" | python3 "$ENGINE" --password-stdin $RC restore --remote test: --path BKHD/cifrato2 \
  --target "$WORK/enc2-out.img" --yes > /dev/null
[ "$(sha256sum "$WORK/enc2-out.img" | cut -d' ' -f1)" = "$SORIG" ] && pass "ripristino dopo ripresa cifrata identico" || fail "differenze"

echo "== 12. Settori danneggiati (simulati) e controllo del disco"
export BKHD_TEST_BAD_SECTORS=4096,4608,10485760
for mode in retry skip; do
  python3 "$ENGINE" $RC backup --source "$SMALL" --remote "$LOC" --path BKHD --name bad-$mode \
    --chunk-size 2880K --bad-sectors $mode > "$WORK/bad-$mode.log"
  grep -q '"bad_sectors_total": 3' "$LOC/BKHD/bad-$mode/manifest.json" && pass "$mode: 3 settori illeggibili annotati" || fail "$mode: conteggio"
done
python3 "$ENGINE" scan --source "$SMALL" > "$WORK/scan.log"
grep -q "3 settori illeggibili" "$WORK/scan.log" && pass "controllo disco: 3 settori trovati" || fail "scan: $(tail -1 "$WORK/scan.log")"
unset BKHD_TEST_BAD_SECTORS
python3 "$ENGINE" scan --source "$SMALL" | grep -q "tutti i settori sono leggibili" && pass "controllo disco sano" || fail "scan sano"
python3 "$ENGINE" $RC restore --remote "$LOC" --path BKHD/bad-skip --target "$WORK/bad-out.img" --yes > /dev/null
python3 - "$SMALL" "$WORK/bad-out.img" <<'PY' && pass "settori illeggibili ripristinati come zeri, il resto identico" || fail "contenuto settori"
import sys
a = bytearray(open(sys.argv[1], "rb").read()); b = open(sys.argv[2], "rb").read()
for off in (4096, 4608, 10485760):
    assert b[off:off + 512] == bytes(512)
    a[off:off + 512] = bytes(512)
assert bytes(a) == b
PY

echo "== 13. Sicurezza: collegamenti simbolici, manifest alterati, downgrade della protezione"
SEC="$WORK/sicurezza"; rm -rf "$SEC"; mkdir -p "$SEC/dest/BKHD/attacco" "$SEC/vittima"
echo "file importante" > "$SEC/vittima/segreto.txt"
ln -s "$SEC/vittima/segreto.txt" "$SEC/dest/BKHD/attacco/attacco.part00001.bkhd.partial"
ln -s "$SEC/vittima/segreto.txt" "$SEC/dest/BKHD/attacco/manifest.json"
python3 "$ENGINE" backup --source "$SMALL" --remote "$SEC/dest" --path BKHD --name attacco --chunk-size 2880K \
  > "$SEC/a.log" 2>&1 || true
[ "$(cat "$SEC/vittima/segreto.txt")" = "file importante" ] && pass "link simbolici nella destinazione non seguiti" || fail "file vittima modificato!"
ln -s "$SEC/vittima" "$SEC/dest/BKHD/link-cartella"
python3 "$ENGINE" backup --source "$SMALL" --remote "$SEC/dest/BKHD" --path link-cartella --name x --chunk-size 2880K \
  > "$SEC/b.log" 2>&1 || true
grep -q "collegamento simbolico" "$SEC/b.log" && [ ! -e "$SEC/vittima/x" ] && pass "cartella-collegamento rifiutata" || fail "cartella-collegamento: $(tail -1 "$SEC/b.log")"
ln -s "$SEC/vittima/segreto.txt" "$SEC/ripristino.img"
python3 "$ENGINE" restore --remote "$LOC" --path BKHD/locale --target "$SEC/ripristino.img" --yes > "$SEC/c.log" 2>&1 || true
[ "$(cat "$SEC/vittima/segreto.txt")" = "file importante" ] && grep -q "collegamento simbolico" "$SEC/c.log" \
  && pass "ripristino su collegamento simbolico rifiutato" || fail "ripristino su link: $(tail -1 "$SEC/c.log")"
cp -r "$LOC/BKHD/locale" "$SEC/dest/BKHD/alterato"
for patch in 'm["name"]="../../etc"' 'm["chunk_count"]=999999' 'm["layout"]["digits"]=10**9' 'm["chunk_size"]="4096"'; do
  python3 - "$SEC/dest/BKHD/alterato/manifest.json" "$patch" <<'PY'
import json, sys
src = json.load(open(sys.argv[1] + ".orig")) if __import__("os").path.exists(sys.argv[1] + ".orig") else None
if src is None:
    src = json.load(open(sys.argv[1])); json.dump(src, open(sys.argv[1] + ".orig", "w"))
m = src; exec(sys.argv[2]); json.dump(m, open(sys.argv[1], "w"))
PY
  python3 "$ENGINE" restore --remote "$SEC/dest" --path BKHD/alterato --verify-only > "$SEC/d.log" 2>&1 || true
  grep -q "non valido" "$SEC/d.log" && pass "manifest alterato rifiutato: $patch" || fail "manifest $patch: $(tail -1 "$SEC/d.log")"
done
cp -r "$FAKE_RCLONE_ROOT/BKHD/cifrato2" "$SEC/dest/BKHD/declassato"
python3 - "$SEC/dest/BKHD/declassato/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1])); m["encryption"] = None; m.pop("mac", None); json.dump(m, open(sys.argv[1], "w"))
PY
python3 "$ENGINE" restore --remote "$SEC/dest" --path BKHD/declassato --verify-only --expect-encrypted > "$SEC/e.log" 2>&1 || true
grep -q "NON protetto" "$SEC/e.log" && pass "rimozione della protezione dal manifest rilevata" || fail "downgrade: $(tail -1 "$SEC/e.log")"

echo "== 14. Chiavetta: formattazione per i backup e pennetta live (su file immagine)"
USB="$WORK/usb"; rm -rf "$USB"; mkdir -p "$USB"
for fs in ext4 vfat; do
  truncate -s 1G "$USB/$fs.img"
  python3 "$ENGINE" usb-format --target "$USB/$fs.img" --fs $fs --label dati --yes --allow-file > /dev/null 2>&1
  [ "$(blkid -p -O 1048576 -o value -s TYPE "$USB/$fs.img")" = "$fs" ] && pass "formattazione $fs" || fail "formattazione $fs"
done
if python3 "$ENGINE" usb-format --target /dev/null --fs ext4 --label x --yes >/dev/null 2>&1; then fail "doveva rifiutare"; else pass "rifiuta dispositivi non validi"; fi
if [ -n "${BKHD_TEST_ISO:-}" ] && [ -f "$BKHD_TEST_ISO" ]; then
  truncate -s 3G "$USB/live.img"; truncate -s 3G "$USB/live-dati.img"
  python3 "$ENGINE" live-write --image "$BKHD_TEST_ISO" --target "$USB/live.img" --yes --allow-file > /dev/null 2>&1
  n=$(sfdisk -J "$USB/live.img" 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)['partitiontable']['partitions']))")
  [ "$n" = 3 ] && cmp -s -n 440 "$BKHD_TEST_ISO" "$USB/live.img" && pass "pennetta solo live (predefinita)" || fail "solo live"
  python3 "$ENGINE" live-write --image "$BKHD_TEST_ISO" --target "$USB/live-dati.img" --yes --allow-file \
    --data-fs ext4 > /dev/null 2>&1
  st=$(sfdisk -J "$USB/live-dati.img" 2>/dev/null | python3 -c "import json,sys; p=json.load(sys.stdin)['partitiontable']['partitions']; print(p[-1]['start']*512, p[-1].get('name'))")
  [ "${st#* }" = "BKHD-DATI" ] && e2fsck -fn "$USB/live-dati.img?offset=${st% *}" >/dev/null 2>&1 \
    && cmp -s -n 440 "$BKHD_TEST_ISO" "$USB/live-dati.img" && pass "pennetta live + partizione BKHD-DATI" || fail "live + dati"
else
  echo "  (BKHD_TEST_ISO non impostato: prova della pennetta live saltata)"
fi

echo "== 15. Elenco backup"
python3 "$ENGINE" --rclone "$HERE/fake_rclone.py" list --remote test: --path BKHD
echo "TUTTI I TEST SUPERATI"
