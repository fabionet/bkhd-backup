#!/bin/bash
# Crea il pacchetto bkhd-backup_<versione>_all.deb per Ubuntu / Debian e derivate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import sys; sys.path.insert(0,'$ROOT/src'); import bkhd_engine; print(bkhd_engine.VERSION)")
OUT="${1:-$ROOT/dist}"
# immagine BKHD Live da includere (creata con: sudo bkhd-engine live-build --output dist/bkhd-live-<versione>.iso)
LIVE_ISO="${BKHD_LIVE_ISO:-$ROOT/dist/bkhd-live-$VERSION.iso}"
PKG="$(mktemp -d)/bkhd-backup"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/lib/bkhd-backup" "$PKG/usr/bin" "$PKG/usr/sbin" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" \
         "$PKG/usr/share/polkit-1/actions" "$PKG/usr/share/doc/bkhd-backup"
install -m 755 "$ROOT/src/bkhd_engine.py" "$ROOT/src/bkhd_gui.py" "$ROOT/live/bkhd-live-build.sh" \
    "$PKG/usr/lib/bkhd-backup/"
install -d -m 755 "$PKG/usr/share/bkhd-backup"
install -m 644 "$ROOT/data/bkhd-wallpaper.svg" "$ROOT/data/bkhd-banner.svg" "$ROOT/data/bkhd-banner-notte.svg" \
    "$PKG/usr/share/bkhd-backup/"
ln -s ../lib/bkhd-backup/bkhd_gui.py "$PKG/usr/bin/bkhd-backup"
ln -s ../lib/bkhd-backup/bkhd_engine.py "$PKG/usr/sbin/bkhd-engine"
install -m 644 "$ROOT/data/bkhd-backup.desktop" "$PKG/usr/share/applications/"
install -m 644 "$ROOT/data/bkhd-backup.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
# icone PNG per menu, dock, finestre di dialogo e notifiche (servono rsvg-convert: librsvg2-bin)
if command -v rsvg-convert >/dev/null; then
  for s in 16 22 24 32 48 64 128 256 512; do
    mkdir -p "$PKG/usr/share/icons/hicolor/${s}x${s}/apps"
    rsvg-convert -w $s -h $s "$ROOT/data/bkhd-backup.svg" -o "$PKG/usr/share/icons/hicolor/${s}x${s}/apps/bkhd-backup.png"
    chmod 644 "$PKG/usr/share/icons/hicolor/${s}x${s}/apps/bkhd-backup.png"
  done
  mkdir -p "$PKG/usr/share/pixmaps"
  ln -s ../icons/hicolor/48x48/apps/bkhd-backup.png "$PKG/usr/share/pixmaps/bkhd-backup.png"
else
  echo "ATTENZIONE: rsvg-convert non trovato (sudo apt install librsvg2-bin): solo icona SVG" >&2
fi
install -m 644 "$ROOT/data/org.bkhd.backup.policy" "$PKG/usr/share/polkit-1/actions/"
install -m 644 "$ROOT/README.md" "$PKG/usr/share/doc/bkhd-backup/"
if [ -f "$LIVE_ISO" ]; then
  install -d -m 755 "$PKG/usr/share/bkhd-backup/live"
  install -m 644 "$LIVE_ISO" "$PKG/usr/share/bkhd-backup/live/bkhd-live-$VERSION.iso"
  (cd "$PKG/usr/share/bkhd-backup/live" && sha256sum "bkhd-live-$VERSION.iso" > "bkhd-live-$VERSION.iso.sha256")
  chmod 644 "$PKG/usr/share/bkhd-backup/live/bkhd-live-$VERSION.iso.sha256"
  echo "Immagine BKHD Live inclusa: $LIVE_ISO"
else
  echo "ATTENZIONE: immagine BKHD Live non trovata ($LIVE_ISO): il pacchetto non la conterra'" >&2
fi
[ -f "$ROOT/docs/Guida-BKHD-Backup.pdf" ] && install -m 644 "$ROOT/docs/Guida-BKHD-Backup.pdf" "$PKG/usr/share/doc/bkhd-backup/"
cat > "$PKG/usr/share/doc/bkhd-backup/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: bkhd-backup
Upstream-Contact: fabionet <fabionet666@gmail.com>

Files: *
Copyright: 2026 fabionet <fabionet666@gmail.com>
License: GPL-3.0+
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 On Debian systems, the complete text of the GNU General Public
 License version 3 can be found in "/usr/share/common-licenses/GPL-3".
COPY
chmod 644 "$PKG/usr/share/doc/bkhd-backup/copyright"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: bkhd-backup
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-3.0, python3-cryptography, rclone, util-linux, e2fsprogs,
 pkexec | policykit-1
Recommends: zstd, libnotify-bin, partclone, smartmontools, librsvg2-common, exfatprogs, dosfstools, gdisk,
 xorriso, udisks2
Suggests: debootstrap, squashfs-tools, mtools, ntfs-3g
Installed-Size: $(du -sk "$PKG/usr" | cut -f1)
Maintainer: fabionet <fabionet666@gmail.com>
Description: Backup settore per settore di interi dischi direttamente sul cloud
 Copia un disco (o una partizione) settore per settore in pacchetti .bkhd
 da 1,2 MB (floppy) a 8 GB, preparati in una cartella temporanea in RAM e
 caricati uno alla volta sul cloud con rclone (Google Drive, OneDrive,
 Dropbox, S3, Mega, WebDAV, SFTP...) oppure scritti direttamente su un disco
 locale. Copia dei soli settori usati, protezione con password AES-256-GCM,
 gestione dei settori danneggiati, ripresa dei backup interrotti e
 creazione della pennetta avviabile BKHD Live (Ubuntu minimale con Firefox,
 rete LAN/Wi-Fi e strumenti di diagnostica).
 .
 Il pacchetto include l'immagine BKHD Live pronta da scrivere su chiavetta
 USB (anche con spazio aggiuntivo per i backup) o da masterizzare su DVD,
 oltre alla formattazione delle chiavette da usare come destinazione.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -f /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null && update-desktop-database -q || true
POST
chmod 755 "$PKG/DEBIAN/postinst"
find "$PKG" -type d -exec chmod 755 {} +
mkdir -p "$OUT"
# l'immagine ISO e' gia' compressa (squashfs zstd): compressione veloce per non perdere minuti
dpkg-deb --root-owner-group -Zgzip -z1 --build "$PKG" "$OUT/bkhd-backup_${VERSION}_all.deb"
rm -rf "$(dirname "$PKG")"
echo "Creato: $OUT/bkhd-backup_${VERSION}_all.deb"
