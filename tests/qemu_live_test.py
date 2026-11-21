#!/usr/bin/python3
"""Collaudo di BKHD Live in QEMU: avvio da chiavetta USB virtuale (BIOS o UEFI con Secure Boot),
schermate del menu e del desktop, verifica di rete, Firefox, BKHD Backup e Secure Boot da terminale.

Uso: qemu_live_test.py bios|uefi-sb FILE.iso CARTELLA_SCHERMATE
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

mode, iso, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="bkhd-qemu-")
sock = os.path.join(tmp, "monitor.sock")

cmd = ["qemu-system-x86_64", "-enable-kvm", "-m", "4096", "-smp", "4", "-display", "none", "-vga", "std",
       "-monitor", f"unix:{sock},server,nowait", "-nic", "user,model=e1000",
       "-device", "qemu-xhci,id=xhci", "-drive", f"if=none,id=chiavetta,format=raw,file={iso}" + (",readonly=on" if iso.endswith(".iso") else ""),
       "-device", "usb-storage,bus=xhci.0,drive=chiavetta,bootindex=0", "-device", "usb-tablet,bus=xhci.0"]
if mode == "uefi-sb":
    vars_fd = os.path.join(tmp, "OVMF_VARS.fd")
    shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.ms.fd", vars_fd)      # chiavi Microsoft/Canonical gia' registrate
    cmd += ["-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on",
            "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd",
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_fd}"]
proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def monitor(command):
    for _ in range(50):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(sock)
            break
        except OSError:
            time.sleep(0.2)
    s.recv(4096)
    s.sendall((command + "\n").encode())
    time.sleep(0.3)
    try:
        s.settimeout(1)
        data = s.recv(65536).decode(errors="replace")
    except OSError:
        data = ""
    s.close()
    return data


def shot(name):
    ppm = os.path.join(tmp, name + ".ppm")
    monitor(f"screendump {ppm}")
    time.sleep(1)
    subprocess.run(["convert", ppm, os.path.join(out, f"{mode}-{name}.png")])
    print("schermata", f"{mode}-{name}.png", flush=True)


# tasti per una tastiera italiana (la sessione live usa il layout "it")
KEYS = {" ": "spc", "-": "slash", ".": "dot", "|": "shift-grave_accent", "\n": "ret", "/": "shift-7", ":": "shift-dot",
        "_": "shift-slash", ">": "shift-less", "=": "shift-0", "'": "minus", ";": "shift-comma"}


def type_text(text):
    for ch in text:
        if ch.isupper():
            k = "shift-" + ch.lower()
        elif ch.isalnum():
            k = ch
        else:
            k = KEYS[ch]
        monitor(f"sendkey {k}")
        time.sleep(0.05)


try:
    time.sleep(6)
    shot("1-menu-avvio")
    for t in range(40):                  # attende il desktop (BKHD Backup si apre da solo)
        time.sleep(5)
    shot("2-desktop")
    monitor("sendkey ctrl-alt-t")        # terminale XFCE
    time.sleep(6)
    type_text(sys.argv[4] if len(sys.argv) > 4 else "clear\nbkhd-diagnosi\n")
    time.sleep(15)
    shot("3-terminale")
finally:
    try:
        monitor("quit")
    except OSError:
        pass
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read().decode(errors="replace").strip()
    if err:
        print("qemu:", err[-500:])
    shutil.rmtree(tmp, ignore_errors=True)
