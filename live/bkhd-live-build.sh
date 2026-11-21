#!/bin/bash
# BKHD Live - costruisce l'immagine ISO avviabile "BKHD Live":
# Ubuntu 24.04 minimale (XFCE) con BKHD Backup, Firefox, rete LAN/Wi-Fi e strumenti di diagnostica.
# Avvio da BIOS e da UEFI con Secure Boot (shim e GRUB firmati da Canonical).
#
# Copyright (C) 2026 fabionet <fabionet666@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Uso (da root):
#   bkhd-live-build.sh --payload DIR --output FILE.iso [--workdir DIR] [--mirror URL] [--version X]
#
# Le righe che iniziano con "##BKHD-" sono lette dall'interfaccia grafica per mostrare l'avanzamento.

set -euo pipefail

WORK=/var/tmp/bkhd-live
OUT=
PAYLOAD=
MIRROR=http://archive.ubuntu.com/ubuntu
SECURITY=http://security.ubuntu.com/ubuntu
SUITE=noble
VERSION=dev
KEEP_CHROOT=1
MOZILLA_FPR=35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3   # impronta della chiave del repository APT di Mozilla
TOTAL=9

fase() { echo "##BKHD-FASE $1/$TOTAL $2"; }
info() { echo "##BKHD-LOG $*"; }
die() { echo "##BKHD-ERRORE $*"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --workdir) WORK="$2"; shift 2 ;;
        --output) OUT="$2"; shift 2 ;;
        --payload) PAYLOAD="$2"; shift 2 ;;
        --mirror) MIRROR="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --clean) KEEP_CHROOT=0; shift ;;
        *) die "opzione sconosciuta: $1" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "serve l'accesso da amministratore"
[ -n "$OUT" ] && [ -n "$PAYLOAD" ] && [ -d "$PAYLOAD" ] || die "uso: $0 --payload DIR --output FILE.iso"
[ "$(uname -m)" = x86_64 ] || die "la costruzione e' supportata solo su PC a 64 bit (x86_64)"
[[ "$MIRROR" =~ ^https?://[A-Za-z0-9._~:/%-]+$ ]] || die "indirizzo del mirror non valido"
[[ "$VERSION" =~ ^[A-Za-z0-9._~-]+$ ]] || die "versione non valida"
# la cartella di lavoro deve essere quella preparata (e verificata) dal motore di BKHD Backup
[ -d "$WORK" ] && [ -f "$WORK/.bkhd-workdir" ] && [ "$(stat -c %u "$WORK")" = 0 ] \
    || die "cartella di lavoro non preparata da BKHD Backup"
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg
[ -f "$KEYRING" ] || die "manca il portachiavi di Ubuntu per verificare i pacchetti: sudo apt install ubuntu-keyring"
for t in debootstrap mksquashfs xorriso mkfs.vfat mcopy mmd; do
    command -v "$t" >/dev/null || die "manca il programma '$t': sudo apt install debootstrap squashfs-tools xorriso dosfstools mtools"
done

CH="$WORK/chroot"
ISO="$WORK/iso"
mkdir -p "$WORK"
avail=$(df --output=avail -B1 "$WORK" | tail -1)
[ "$avail" -ge $((10 * 1024 * 1024 * 1024)) ] || [ -f "$CH/.bkhd-base-ok" ] \
    || die "spazio insufficiente in $WORK: servono almeno 10 GB liberi ($((avail / 1073741824)) GB disponibili)"

# --- montaggi del chroot, sempre smontati all'uscita --------------------------------------------
umount_chroot() {
    for m in run sys/firmware/efi/efivars sys proc dev/pts dev; do
        if mountpoint -q "$CH/$m" 2>/dev/null; then
            umount "$CH/$m" 2>/dev/null || umount -l "$CH/$m" 2>/dev/null || true
        fi
    done
}
mount_chroot() {
    mount --bind /dev "$CH/dev"
    mount -t devpts devpts "$CH/dev/pts"
    mount -t proc proc "$CH/proc"
    mount -t sysfs sysfs "$CH/sys"
    mount -t tmpfs tmpfs "$CH/run"
}
assert_unmounted() {
    if grep -qs " $CH/" /proc/mounts; then
        die "impossibile smontare il sistema di lavoro: interrotto per sicurezza (riavvia il PC e riprova)"
    fi
}
trap umount_chroot EXIT
trap 'echo "##BKHD-ERRORE costruzione interrotta (riga $LINENO)"; exit 1' ERR
in_chroot() { chroot "$CH" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
                  DEBIAN_FRONTEND=noninteractive "$@"; }
apt_in() { in_chroot apt-get -y -o Dpkg::Options::=--force-confnew -o APT::Install-Recommends=false "$@"; }

# --- 1. sistema di base ----------------------------------------------------------------------------
fase 1 "Scaricamento del sistema di base Ubuntu 24.04 (debootstrap)"
umount_chroot
assert_unmounted
if [ "$KEEP_CHROOT" = 1 ] && [ -f "$CH/.bkhd-base-ok" ]; then
    info "Sistema di base gia' presente in $CH: riutilizzato"
else
    rm -rf --one-file-system "$CH"
    debootstrap --arch=amd64 --variant=minbase --components=main,restricted,universe,multiverse \
        --keyring="$KEYRING" --force-check-gpg \
        --include=ca-certificates,gnupg,curl "$SUITE" "$CH" "$MIRROR"
    touch "$CH/.bkhd-base-ok"
fi

# DNS provvisorio per scaricare i pacchetti dentro il sistema in costruzione
chroot_dns() {
    rm -f "$CH/etc/resolv.conf"
    if [ -s /run/systemd/resolve/resolv.conf ]; then
        cp /run/systemd/resolve/resolv.conf "$CH/etc/resolv.conf"
    else
        cp -L /etc/resolv.conf "$CH/etc/resolv.conf"
    fi
}
mount_chroot
chroot_dns
printf '#!/bin/sh\nexit 101\n' > "$CH/usr/sbin/policy-rc.d"; chmod 755 "$CH/usr/sbin/policy-rc.d"
echo "bkhd-live" > "$CH/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 bkhd-live\n::1 localhost ip6-localhost ip6-loopback\n' > "$CH/etc/hosts"
: > "$CH/etc/apt/sources.list"
cat > "$CH/etc/apt/sources.list.d/ubuntu.sources" <<EOF
Types: deb
URIs: $MIRROR
Suites: $SUITE $SUITE-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: $SECURITY
Suites: $SUITE-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF

# --- 2. aggiornamento e kernel -----------------------------------------------------------------------
fase 2 "Aggiornamento del sistema, kernel e firmware (schede di rete e Wi-Fi)"
apt_in update
apt_in dist-upgrade
apt_in install linux-image-generic linux-firmware intel-microcode amd64-microcode casper initramfs-tools \
    systemd-sysv sudo locales tzdata keyboard-configuration console-setup kbd udev kmod dbus-x11 \
    less nano bash-completion

# --- 3. rete e desktop -------------------------------------------------------------------------------
fase 3 "Rete (LAN, Wi-Fi) e desktop leggero XFCE"
apt_in install systemd-resolved network-manager network-manager-gnome wpasupplicant wireless-regdb iw rfkill iproute2 \
    iputils-ping bind9-dnsutils ethtool net-tools wget openssh-client \
    xserver-xorg xinit lightdm lightdm-gtk-greeter xfce4-session xfwm4 xfce4-panel xfdesktop4 xfce4-settings \
    xfce4-terminal xfce4-notifyd xfce4-power-manager thunar mousepad \
    policykit-1-gnome yaru-theme-gtk yaru-theme-icon adwaita-icon-theme fonts-dejavu-core fonts-ubuntu \
    librsvg2-common xdg-utils libnotify-bin gvfs udisks2
chroot_dns      # systemd-resolved ha ricollegato resolv.conf al suo servizio, non attivo durante la costruzione

# --- 4. Firefox dal repository ufficiale Mozilla (il pacchetto Ubuntu e' uno snap, inutilizzabile in un live)
fase 4 "Firefox (repository ufficiale Mozilla) come browser predefinito"
install -d -m 755 "$CH/etc/apt/keyrings"
in_chroot curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc
keys=$(in_chroot gpg --show-keys --with-colons /etc/apt/keyrings/packages.mozilla.org.asc)
npub=$(printf '%s\n' "$keys" | grep -c '^pub')
fpr=$(printf '%s\n' "$keys" | awk -F: '/^fpr/{print $10; exit}')
[ "$npub" = 1 ] && [ "$fpr" = "$MOZILLA_FPR" ] \
    || die "la chiave del repository Mozilla non corrisponde (chiavi: $npub, impronta: $fpr): scaricamento non sicuro"
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
    > "$CH/etc/apt/sources.list.d/mozilla.list"
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > "$CH/etc/apt/preferences.d/mozilla"
apt_in update
apt_in install firefox firefox-l10n-it

# --- 5. BKHD Backup e strumenti di diagnostica -------------------------------------------------------
fase 5 "BKHD Backup e strumenti di diagnostica"
apt_in install python3 python3-gi gir1.2-gtk-3.0 python3-cryptography rclone zstd partclone pkexec polkitd \
    smartmontools gsmartcontrol gparted gnome-disk-utility testdisk gddrescue nvme-cli hdparm lshw inxi \
    pciutils usbutils dmidecode memtester stress-ng iperf3 mtr-tiny htop ncdu \
    e2fsprogs dosfstools exfatprogs ntfs-3g btrfs-progs xfsprogs cryptsetup lvm2 mdadm grub-pc-bin gdisk
cp -a "$PAYLOAD"/. "$CH"/
in_chroot gtk-update-icon-cache -q -f /usr/share/icons/hicolor || true
in_chroot update-desktop-database -q || true

# Secure Boot: shim e GRUB firmati, estratti senza installarli (i loro script toccherebbero l'EFI del PC)
rm -rf --one-file-system "$CH/tmp/efi" && mkdir -p "$CH/tmp/efi/x"
in_chroot sh -c 'cd /tmp/efi && apt-get download shim-signed grub-efi-amd64-signed memtest86+'
for d in "$CH"/tmp/efi/*.deb; do dpkg-deb -x "$d" "$CH/tmp/efi/x"; done

# --- 6. configurazione del sistema live --------------------------------------------------------------
fase 6 "Configurazione: italiano, accesso automatico, collegamenti sul desktop"
sed -i 's/^# *it_IT.UTF-8/it_IT.UTF-8/' "$CH/etc/locale.gen"
in_chroot locale-gen
echo 'LANG=it_IT.UTF-8' > "$CH/etc/default/locale"
cat > "$CH/etc/default/keyboard" <<'EOF'
XKBMODEL="pc105"
XKBLAYOUT="it"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
EOF
ln -sf /usr/share/zoneinfo/Europe/Rome "$CH/etc/localtime"; echo Europe/Rome > "$CH/etc/timezone"
# tastiera italiana anche nella sessione grafica (Xorg non legge sempre /etc/default/keyboard nel live)
mkdir -p "$CH/etc/X11/xorg.conf.d"
cat > "$CH/etc/X11/xorg.conf.d/00-tastiera.conf" <<'FINE'
Section "InputClass"
    Identifier "tastiera italiana"
    MatchIsKeyboard "on"
    Option "XkbLayout" "it"
EndSection
FINE
cat > "$CH/etc/casper.conf" <<'EOF'
export USERNAME="bkhd"
export USERFULLNAME="BKHD Live"
export HOST="bkhd-live"
export BUILD_SYSTEM="Ubuntu"
export FLAVOUR="BKHD"
EOF
mkdir -p "$CH/etc/lightdm/lightdm.conf.d"
cat > "$CH/etc/lightdm/lightdm.conf.d/50-bkhd-live.conf" <<'EOF'
[Seat:*]
autologin-user=bkhd
autologin-user-timeout=0
autologin-session=xfce
user-session=xfce
greeter-session=lightdm-gtk-greeter
EOF
# sessione live: come il "sudo senza password" dell'utente live di Ubuntu, le azioni da amministratore
# (BKHD, GParted, Dischi...) non chiedono password. Vale solo dentro la pennetta, mai sul PC dell'utente.
mkdir -p "$CH/etc/polkit-1/rules.d"
cat > "$CH/etc/polkit-1/rules.d/49-bkhd-live.rules" <<'EOF'
polkit.addRule(function(action, subject) {
    if (subject.local && subject.active && subject.isInGroup("sudo")) {
        return polkit.Result.YES;
    }
});
EOF
# browser e lettore PDF predefiniti: Firefox
cat > "$CH/etc/xdg/mimeapps.list" <<'EOF'
[Default Applications]
x-scheme-handler/http=firefox.desktop
x-scheme-handler/https=firefox.desktop
text/html=firefox.desktop
application/xhtml+xml=firefox.desktop
application/pdf=firefox.desktop
EOF
mkdir -p "$CH/etc/xdg/xfce4"
printf 'WebBrowser=firefox\nTerminalEmulator=xfce4-terminal\nFileManager=thunar\n' > "$CH/etc/xdg/xfce4/helpers.rc"
in_chroot update-alternatives --set x-www-browser /usr/bin/firefox 2>/dev/null || true
# pannello XFCE predefinito senza domande al primo avvio
mkdir -p "$CH/etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
[ -f "$CH/etc/xdg/xfce4/panel/default.xml" ] && \
    cp "$CH/etc/xdg/xfce4/panel/default.xml" "$CH/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
cat > "$CH/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="Yaru"/>
    <property name="IconThemeName" type="string" value="Yaru"/>
  </property>
</channel>
EOF
# collegamenti sul desktop e avvio automatico di BKHD Backup
SKEL="$CH/etc/skel"
mkdir -p "$SKEL/Desktop" "$SKEL/.config/autostart" "$CH/usr/local/bin"
# diagnosi rapida: rete, Internet, Secure Boot, dischi, memoria
cat > "$CH/usr/local/bin/bkhd-diagnosi" <<'FINE'
#!/bin/bash
export PAGER=cat SYSTEMD_PAGER=cat
v() { printf '\033[1;34m%s\033[0m\n' "$1"; }
v "=== BKHD Live: diagnosi rapida ==="
cat /etc/bkhd-live-release 2>/dev/null
v "--- Avvio"
if [ -d /sys/firmware/efi ]; then
    sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
    if [ "$sb" = 1 ]; then echo "UEFI, Secure Boot ATTIVO"; else echo "UEFI, Secure Boot non attivo"; fi
else
    echo "BIOS (avvio tradizionale)"
fi
v "--- Rete"
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>/dev/null | grep -v '^lo:'
ip -4 -br address | grep -v '^lo '
v "--- Internet"
if curl -s -o /dev/null -m 10 https://ubuntu.com; then
    echo "Internet raggiungibile"
else
    echo "Internet NON raggiungibile: collega il cavo o scegli una rete Wi-Fi in alto a destra"
fi
v "--- Dischi"
lsblk -d -e 2,7,11 -o NAME,SIZE,MODEL,TRAN
v "--- Memoria"
free -h | head -2
v "--- Programmi"
echo "$(firefox --version 2>/dev/null) | $(bkhd-engine --version 2>/dev/null | head -1)"
FINE
chmod 755 "$CH/usr/local/bin/bkhd-diagnosi"
cat > "$CH/usr/share/applications/bkhd-diagnosi.desktop" <<'FINE'
[Desktop Entry]
Type=Application
Name=Diagnosi rapida
Comment=Rete, Internet, Secure Boot, dischi e memoria
Icon=utilities-system-monitor
Exec=xfce4-terminal --hold -x bkhd-diagnosi
Categories=System;
FINE
for app in bkhd-backup bkhd-diagnosi firefox gparted gsmartcontrol org.gnome.DiskUtility xfce4-terminal; do
    f=$(ls "$CH/usr/share/applications/$app.desktop" 2>/dev/null || true)
    [ -n "$f" ] && install -m 755 "$f" "$SKEL/Desktop/"
done
if [ -f "$CH/usr/share/doc/bkhd-backup/Guida-BKHD-Backup.pdf" ]; then
    cat > "$SKEL/Desktop/guida-bkhd.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Guida di BKHD Backup
Icon=help-browser
Exec=firefox /usr/share/doc/bkhd-backup/Guida-BKHD-Backup.pdf
EOF
    chmod 755 "$SKEL/Desktop/guida-bkhd.desktop"
fi
cat > "$CH/usr/local/bin/bkhd-live-session" <<'EOF'
#!/bin/sh
# Primo accesso: tastiera italiana, collegamenti "fidati", sfondo, e apertura di BKHD Backup.
setxkbmap it 2>/dev/null
for f in "$HOME"/Desktop/*.desktop; do
    [ -f "$f" ] && gio set "$f" metadata::xfce-exe-checksum "$(sha256sum "$f" | cut -c1-64)" 2>/dev/null
done
sleep 3
if [ -f /usr/share/bkhd-backup/bkhd-wallpaper.svg ]; then
    for p in $(xfconf-query -c xfce4-desktop -l 2>/dev/null | grep last-image); do
        xfconf-query -c xfce4-desktop -p "$p" -s /usr/share/bkhd-backup/bkhd-wallpaper.svg
    done
fi
# pennetta con partizione dati BKHD-DATI (facoltativa): montata e proposta come destinazione dei backup
if [ -e /dev/disk/by-label/BKHD-DATI ]; then
    udisksctl mount -b /dev/disk/by-label/BKHD-DATI --no-user-interaction >/dev/null 2>&1
    mp=$(findmnt -nr -o TARGET -S LABEL=BKHD-DATI | head -1)
    cfg="$HOME/.config/bkhd-backup/impostazioni.json"
    if [ -n "$mp" ] && [ ! -e "$cfg" ]; then
        mkdir -p "$(dirname "$cfg")"
        python3 -c 'import json, sys; json.dump({"dest_mode": "local", "local_dest": sys.argv[1], "path": "BKHD"}, open(sys.argv[2], "w"))' "$mp" "$cfg"
    fi
fi
exec bkhd-backup
EOF
chmod 755 "$CH/usr/local/bin/bkhd-live-session"
cat > "$SKEL/.config/autostart/bkhd-live-session.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=BKHD Live
Exec=/usr/local/bin/bkhd-live-session
X-GNOME-Autostart-enabled=true
EOF
# NetworkManager gestisce tutte le schede (anche quelle via cavo)
mkdir -p "$CH/etc/NetworkManager/conf.d"
printf '[keyfile]\nunmanaged-devices=none\n' > "$CH/etc/NetworkManager/conf.d/10-globally-managed-devices.conf"
echo "BKHD Live $VERSION - Ubuntu 24.04 LTS minimal diagnostic suite" > "$CH/etc/bkhd-live-release"
in_chroot update-initramfs -u -k all

# file per l'avvio, preparati finche' il sistema di lavoro e' montato
rm -rf --one-file-system "$WORK/boot" && mkdir -p "$WORK/boot/i386-pc"
cat > "$CH/tmp/early.cfg" <<'EOF2'
search --no-floppy --file --set=root /.disk/info
set prefix=($root)/boot/grub
EOF2
in_chroot grub-mkimage -O i386-pc-eltorito -o /tmp/eltorito.img -p /boot/grub -c /tmp/early.cfg \
    biosdisk iso9660 part_msdos part_gpt search search_fs_file normal configfile linux linux16 \
    echo test gfxterm all_video font reboot halt ls cat chain
mv "$CH/tmp/eltorito.img" "$WORK/boot/eltorito.img"
cp "$CH"/usr/lib/grub/i386-pc/*.mod "$CH"/usr/lib/grub/i386-pc/*.lst "$WORK/boot/i386-pc/"
cp "$CH/usr/lib/grub/i386-pc/boot_hybrid.img" "$WORK/boot/boot_hybrid.img"
cp "$CH/usr/share/grub/unicode.pf2" "$WORK/boot/" 2>/dev/null || true
rm -rf --one-file-system "$WORK/efi-x"
mv "$CH/tmp/efi/x" "$WORK/efi-x"
rm -rf --one-file-system "$CH/tmp/efi" "$CH/tmp/early.cfg"
EFIX="$WORK/efi-x"

# --- 7. pulizia e file system compresso -------------------------------------------------------------
fase 7 "Compressione del sistema (squashfs, alcuni minuti)"
apt_in clean
rm -rf --one-file-system "$CH"/var/lib/apt/lists/* "$CH"/var/log/*.log "$CH"/root/.bash_history
rm -f "$CH/usr/sbin/policy-rc.d"
ln -sf ../run/systemd/resolve/stub-resolv.conf "$CH/etc/resolv.conf"
: > "$CH/etc/machine-id"
umount_chroot
assert_unmounted
rm -rf --one-file-system "$ISO"
mkdir -p "$ISO/casper" "$ISO/boot/grub/i386-pc" "$ISO/boot/grub/fonts" "$ISO/.disk" "$ISO/EFI/BOOT"
KVER=$(ls "$CH"/boot/vmlinuz-* | sort -V | tail -1); KVER=${KVER##*/vmlinuz-}
cp "$CH/boot/vmlinuz-$KVER" "$ISO/casper/vmlinuz"
cp "$CH/boot/initrd.img-$KVER" "$ISO/casper/initrd"
mksquashfs "$CH" "$ISO/casper/filesystem.squashfs" -noappend -comp zstd -Xcompression-level 19 -b 1M \
    -wildcards -e 'boot/vmlinuz*' 'boot/initrd*' '.bkhd-base-ok' 'proc/*' 'sys/*' 'dev/*' 'run/*' 'tmp/*'
du -sx --block-size=1 "$CH" | cut -f1 > "$ISO/casper/filesystem.size"
echo "BKHD Live $VERSION (Ubuntu 24.04 LTS \"Noble Numbat\" - amd64)" > "$ISO/.disk/info"

# --- 8. avvio: GRUB per BIOS e UEFI (Secure Boot) ----------------------------------------------------
fase 8 "Menu di avvio per BIOS e UEFI (Secure Boot)"
cp "$WORK/boot/unicode.pf2" "$ISO/boot/grub/fonts/" 2>/dev/null || true
MT_BIN=$(find "$EFIX" -name 'memtest86+*x64.bin' | head -1); MT_EFI=$(find "$EFIX" -name 'memtest86+*x64.efi' | head -1)
[ -n "$MT_BIN" ] && cp "$MT_BIN" "$ISO/boot/memtest86+x64.bin"
[ -n "$MT_EFI" ] && cp "$MT_EFI" "$ISO/boot/memtest86+x64.efi"
CMD="boot=casper locale=it_IT.UTF-8 keyboard-configuration/layoutcode=it timezone=Europe/Rome"
cat > "$ISO/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=10
if loadfont \$prefix/fonts/unicode.pf2 ; then
    set gfxmode=auto
    insmod all_video
    insmod gfxterm
    terminal_output gfxterm
fi
set menu_color_normal=white/blue
set menu_color_highlight=blue/white
set color_normal=white/blue

menuentry "BKHD Live $VERSION - avvia" {
    linux /casper/vmlinuz $CMD quiet splash ---
    initrd /casper/initrd
}
menuentry "BKHD Live - modalita' sicura (grafica di base, per schede video problematiche)" {
    linux /casper/vmlinuz $CMD nomodeset ---
    initrd /casper/initrd
}
menuentry "BKHD Live - carica tutto in RAM (poi la pennetta si puo' togliere)" {
    linux /casper/vmlinuz $CMD toram quiet splash ---
    initrd /casper/initrd
}
if [ "\$grub_platform" = "efi" ]; then
    menuentry "Memtest86+ - test della memoria RAM (disattiva Secure Boot)" {
        chainloader /boot/memtest86+x64.efi
    }
    menuentry "Impostazioni del firmware UEFI" {
        fwsetup
    }
else
    menuentry "Memtest86+ - test della memoria RAM" {
        linux16 /boot/memtest86+x64.bin
    }
fi
menuentry "Riavvia" {
    reboot
}
menuentry "Spegni" {
    halt
}
EOF
# BIOS: immagine El Torito di GRUB (preparata nella fase 6) con i moduli i386-pc
cp "$WORK/boot/eltorito.img" "$ISO/boot/grub/i386-pc/eltorito.img"
cp "$WORK"/boot/i386-pc/* "$ISO/boot/grub/i386-pc/"
# UEFI: shim firmato -> GRUB firmato "per CD" (cerca /.disk/info e carica /boot/grub/grub.cfg)
SHIM=$(find "$EFIX" -name 'shimx64.efi.signed.latest' | head -1)
[ -n "$SHIM" ] || SHIM=$(find "$EFIX" -name 'shimx64.efi.signed*' | head -1)
GRUBEFI=$(find "$EFIX" -name 'gcdx64.efi.signed' | head -1)
MMX=$(find "$EFIX" -name 'mmx64.efi' | head -1)
[ -n "$SHIM" ] && [ -n "$GRUBEFI" ] || die "file EFI firmati non trovati (shim-signed, grub-efi-amd64-signed)"
cp "$SHIM" "$ISO/EFI/BOOT/BOOTX64.EFI"; cp "$GRUBEFI" "$ISO/EFI/BOOT/grubx64.efi"
[ -n "$MMX" ] && cp "$MMX" "$ISO/EFI/BOOT/mmx64.efi"
rm -f "$WORK/efi.img"
mkfs.vfat -C -n BKHD-EFI "$WORK/efi.img" 10240 >/dev/null
mmd -i "$WORK/efi.img" ::/EFI ::/EFI/BOOT
mcopy -i "$WORK/efi.img" "$ISO"/EFI/BOOT/* ::/EFI/BOOT/

# --- 9. immagine ISO ibrida (DVD e chiavetta USB) ----------------------------------------------------
fase 9 "Creazione dell'immagine ISO"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
xorriso -as mkisofs -r -V "BKHD_LIVE" -o "$OUT" -J -joliet-long -l -iso-level 3 \
    --grub2-mbr "$WORK/boot/boot_hybrid.img" -partition_offset 16 --mbr-force-bootable \
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b "$WORK/efi.img" -appended_part_as_gpt \
    -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7 \
    -c /boot.catalog -b /boot/grub/i386-pc/eltorito.img -no-emul-boot -boot-load-size 4 -boot-info-table \
    --grub2-boot-info -eltorito-alt-boot -e '--interval:appended_partition_2:all::' -no-emul-boot \
    "$ISO"
[ "$KEEP_CHROOT" = 1 ] || rm -rf --one-file-system "$CH"
rm -rf --one-file-system "$ISO" "$EFIX" "$WORK/boot" "$WORK/efi.img"
echo "##BKHD-RISULTATO $OUT $(stat -c %s "$OUT")"
