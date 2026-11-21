#!/usr/bin/python3
# -*- coding: utf-8 -*-
# BKHD Backup - backup settore per settore di dischi direttamente sul cloud
# Copyright (C) 2026 fabionet <fabionet666@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""BKHD Backup - interfaccia grafica (GTK 3) per Ubuntu / Debian e derivate, tema "nuvoletta".

La GUI gira come utente normale; le operazioni sul disco vengono eseguite da
bkhd_engine.py avviato con pkexec (richiede la password di amministratore).
"""

import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import bkhd_engine as eng  # noqa: E402

ENGINE = os.path.join(HERE, "bkhd_engine.py")
ICON_NAME = "bkhd-backup"
APP_NAME = "BKHD Backup"
# risorse: installate dal pacchetto .deb oppure nelle cartelle del progetto (esecuzione dai sorgenti)
GUIDE_FILES = [os.path.join(HERE, "..", "docs", "Guida-BKHD-Backup.pdf"),
               "/usr/share/doc/bkhd-backup/Guida-BKHD-Backup.pdf"]
ICON_FILES = [os.path.join(HERE, "..", "data", "bkhd-backup.svg"),
              "/usr/share/icons/hicolor/scalable/apps/bkhd-backup.svg"]
BANNER_FILES = {
    "giorno": [os.path.join(HERE, "..", "data", "bkhd-banner.svg"), "/usr/share/bkhd-backup/bkhd-banner.svg"],
    "notte": [os.path.join(HERE, "..", "data", "bkhd-banner-notte.svg"),
              "/usr/share/bkhd-backup/bkhd-banner-notte.svg"],
}
THEMES = [("auto", "Automatico (come il desktop)"), ("giorno", "☀  Nuvola di giorno"), ("notte", "🌙  Nuvola di notte")]
PALETTE = {
    "giorno": {"ok": "#2e7d32", "err": "#c7162b", "warn": "#b26a00", "dim": "#66748a"},
    "notte": {"ok": "#7bd88f", "err": "#ff7a85", "warn": "#ffb454", "dim": "#93a4bf"},
}
C = dict(PALETTE["giorno"])             # colori del tema attivo, usati nei testi colorati

# formati pronti: (etichetta, dimensione in KB)
PRESETS = [("Floppy 5¼\" — 1,2 MB", 1200), ("Floppy 3½\" — 1,44 MB", 1440),
           ("Floppy 3½\" ED — 2,88 MB", 2880), ("50 MB", 50 * 1024), ("100 MB", 100 * 1024),
           ("650 MB (CD)", 650 * 1024), ("700 MB (CD)", 700 * 1024), ("1 GB", 1024 * 1024),
           ("2 GB", 2048 * 1024), ("4 GB", 4096 * 1024), ("4,7 GB (DVD)", 4482 * 1024), ("8 GB", 8192 * 1024)]
MANY_CHUNKS = 10000        # oltre: avviso "backup piu' lento"
TOO_MANY_CHUNKS = 50000    # oltre: richiesta di conferma
ZOOM_LEVELS = [0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]
SETTINGS_FILE = os.path.join(GLib.get_user_config_dir(), "bkhd-backup", "impostazioni.json")
PHASES = {"read": "Lettura dal disco", "upload": "Salvataggio sulla destinazione", "download": "Download in RAM",
          "verify": "Verifica", "write": "Scrittura", "scan": "Controllo dei settori", "live": "Costruzione"}
BAD_MODES = [("retry", "Riprova e copia tutto ciò che si riesce a leggere (più lento)"),
             ("skip", "Salta subito i settori danneggiati (più veloce, meno stress per il disco)")]

CSS_GIORNO = """
window.bkhd { background-color: #eef6ff; }
.bkhd headerbar { background-image: linear-gradient(to bottom, #4c9ef1, #2f80ed); border: none;
                  box-shadow: none; color: #ffffff; }
.bkhd headerbar .title, .bkhd headerbar label { color: #ffffff; }
.bkhd headerbar button { color: #ffffff; background-image: none; background-color: rgba(255,255,255,0.12);
                         border: 1px solid rgba(255,255,255,0.30); box-shadow: none; text-shadow: none; }
.bkhd headerbar button:hover { background-color: rgba(255,255,255,0.25); }
.bkhd headerbar stackswitcher button { border-radius: 18px; padding: 4px 16px; margin: 0 3px; }
.bkhd headerbar stackswitcher button:checked { background-color: #ffffff; color: #1f6fd6; }
.bkhd headerbar stackswitcher button:checked label { color: #1f6fd6; }
.banner { background-color: #3b8ff0; background-size: cover; background-position: center; min-height: 78px; }
.banner-title { color: #ffffff; font-size: 19pt; font-weight: bold; }
.banner-sub { color: rgba(255,255,255,0.95); }
.card { background-color: #ffffff; border-radius: 16px; border: 1px solid #d3e5fb;
        box-shadow: 0 2px 6px rgba(47,128,237,0.13); padding: 14px 18px; }
.card-title { color: #1a4f9c; font-weight: bold; font-size: 12pt; }
.card-icon { color: #2f80ed; }
.field { color: #36475e; }
.dim { color: #66748a; }
.warn-text { color: #b26a00; }
.err-text { color: #c7162b; }
.ok-text { color: #2e7d32; }
button.cloud { background-image: linear-gradient(to bottom, #55a6f8, #2f80ed); color: #ffffff; border-radius: 20px;
               border: none; padding: 7px 24px; font-weight: bold; box-shadow: 0 2px 5px rgba(47,128,237,0.35); }
button.cloud label { color: #ffffff; }
button.cloud:hover { background-image: linear-gradient(to bottom, #68b2fb, #3a8af0); }
button.cloud:disabled { background-image: none; background-color: #bcd5f3; box-shadow: none; }
button.danger { background-image: linear-gradient(to bottom, #ef5b6c, #c7162b); color: #ffffff; border-radius: 20px;
                border: none; padding: 7px 24px; font-weight: bold; box-shadow: 0 2px 5px rgba(199,22,43,0.30); }
button.danger label { color: #ffffff; }
button.danger:disabled { background-image: none; background-color: #f1c3c9; box-shadow: none; }
button.soft { border-radius: 16px; }
progressbar trough { min-height: 12px; border-radius: 6px; background-color: #dbeafc; border: none; }
progressbar progress { min-height: 12px; border-radius: 6px; border: none;
                       background-image: linear-gradient(to right, #7cbaf7, #2f80ed); }
.statusbox { background-color: #ffffff; border-top: 1px solid #d3e5fb; }
.status-title { color: #1a4f9c; font-weight: bold; }
treeview.view:selected { background-color: #2f80ed; color: #ffffff; }
"""

# tema "nuvola di notte": cielo blu notte, schede scure, testi chiari, accenti azzurro luna
CSS_NOTTE = """
window.bkhd { background-color: #0d1629; }
.bkhd headerbar { background-image: linear-gradient(to bottom, #1c2f57, #101d38); border: none;
                  box-shadow: none; color: #e6eefc; }
.bkhd headerbar .title, .bkhd headerbar label { color: #e6eefc; }
.bkhd headerbar button { color: #e6eefc; background-image: none; background-color: rgba(255,255,255,0.08);
                         border: 1px solid rgba(255,255,255,0.18); box-shadow: none; text-shadow: none; }
.bkhd headerbar button:hover { background-color: rgba(255,255,255,0.16); }
.bkhd headerbar stackswitcher button { border-radius: 18px; padding: 4px 16px; margin: 0 3px; }
.bkhd headerbar stackswitcher button:checked { background-color: #9cc4ff; color: #0d1629; }
.bkhd headerbar stackswitcher button:checked label { color: #0d1629; }
.banner { background-color: #13234a; background-size: cover; background-position: center; min-height: 78px; }
.banner-title { color: #fff6d8; font-size: 19pt; font-weight: bold; }
.banner-sub { color: rgba(230,238,252,0.85); }
.card { background-color: #16233f; border-radius: 16px; border: 1px solid #263b63;
        box-shadow: 0 2px 8px rgba(0,0,0,0.35); padding: 14px 18px; }
.card label { color: #dfe8f7; }
.card-title, .card .card-title { color: #9cc4ff; font-weight: bold; font-size: 12pt; }
.card-icon { color: #9cc4ff; }
.field, .card .field { color: #b8c7de; }
.dim, .card .dim { color: #93a4bf; }
.warn-text, .card .warn-text { color: #ffb454; }
.err-text, .card .err-text { color: #ff7a85; }
.ok-text { color: #7bd88f; }
.bkhd entry, .bkhd spinbutton, .bkhd combobox button { background-color: #0f1a31; color: #e6eefc;
                                                         border-color: #2c4370; }
.bkhd entry:disabled, .bkhd combobox button:disabled { background-color: #131f38; color: #5f7090; }
.bkhd treeview.view { background-color: #0f1a31; color: #dfe8f7; }
.bkhd treeview.view header button { background-color: #16233f; color: #9cc4ff; border-color: #263b63; }
button.soft { border-radius: 16px; background-image: none; background-color: #1d2d50; color: #dfe8f7;
              border: 1px solid #2c4370; box-shadow: none; }
button.soft label { color: #dfe8f7; }
button.soft:hover { background-color: #26396a; }
button.soft:disabled { background-color: #152240; }
button.soft:disabled label { color: #5f7090; }
button.cloud { background-image: linear-gradient(to bottom, #5b8def, #3a62c9); color: #ffffff; border-radius: 20px;
               border: none; padding: 7px 24px; font-weight: bold; box-shadow: 0 2px 6px rgba(0,0,0,0.45); }
button.cloud label { color: #ffffff; }
button.cloud:hover { background-image: linear-gradient(to bottom, #6c9bf5, #4570d6); }
button.cloud:disabled { background-image: none; background-color: #2a3c63; box-shadow: none; }
button.cloud:disabled label { color: #7f90b0; }
button.danger { background-image: linear-gradient(to bottom, #e0566a, #a3182d); color: #ffffff; border-radius: 20px;
                border: none; padding: 7px 24px; font-weight: bold; box-shadow: 0 2px 6px rgba(0,0,0,0.45); }
button.danger label { color: #ffffff; }
button.danger:disabled { background-image: none; background-color: #4a2a35; box-shadow: none; }
button.danger:disabled label { color: #9a7a85; }
progressbar trough { min-height: 12px; border-radius: 6px; background-color: #1f3054; border: none; }
progressbar progress { min-height: 12px; border-radius: 6px; border: none;
                       background-image: linear-gradient(to right, #6e8fe6, #9cc4ff); }
progressbar text { color: #b8c7de; }
.statusbox { background-color: #111d36; border-top: 1px solid #263b63; }
.statusbox label { color: #dfe8f7; }
.status-title { color: #9cc4ff; font-weight: bold; }
.statusbox textview text { background-color: #0b1427; color: #dfe8f7; }
.statusbox .dim { color: #93a4bf; }
.bkhd checkbutton label, .bkhd radiobutton label { color: #dfe8f7; }
.bkhd infobar { background-color: #3a2f12; }
.bkhd infobar label { color: #ffe3a3; }
treeview.view:selected { background-color: #3a62c9; color: #ffffff; }
"""


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def first_file(paths):
    return next((p for p in paths if os.path.isfile(p)), None)


def fmt_num(n):
    return f"{n:,}".replace(",", ".")


def fmt_chunk(nbytes):
    """Dimensione di un pacchetto: in KB sotto i 10 MB (formati floppy), altrimenti in MB/GB."""
    if nbytes < 10 * eng.MIB:
        return f"{fmt_num(nbytes // 1024)} KB"
    return eng.human(nbytes)


def fmt_dur(sec):
    sec = int(max(sec, 0))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} giorn{'o' if d == 1 else 'i'}" + (f" {h} h" if h else "")
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def list_block_devices():
    """Dischi, partizioni e unita' CD/DVD (esclusi loop, zram, floppy)."""
    out = subprocess.run(["lsblk", "-J", "-b", "-o",
                          "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,TRAN,RM,MOUNTPOINT,FSTYPE,LABEL"],
                         capture_output=True, text=True)
    devs = []

    def mounts_of(d):
        res = [d["mountpoint"]] if d.get("mountpoint") else []
        for c in d.get("children") or []:
            res += mounts_of(c)
        return res

    def walk(d, parent=None):
        model = (d.get("model") or (parent or {}).get("model") or "").strip()
        size = int(d.get("size") or 0)
        if d.get("type") == "rom" and d["name"].startswith("sr"):        # masterizzatore CD/DVD
            devs.append({"path": d["path"], "name": d["name"], "type": "rom", "size": size, "model": model,
                         "serial": "", "removable": True, "mounts": mounts_of(d), "parent": None,
                         "fstype": d.get("fstype") or "", "label": d.get("label") or "",
                         "text": f"{d['path']}  —  {model or 'Unità CD/DVD'}"})
            return
        if d.get("type") not in ("disk", "part") or d["name"].startswith(("zram", "loop", "fd", "sr")):
            return
        if size < eng.MIB:                     # floppy virtuali, lettori vuoti, dispositivi fittizi
            return
        extra = d.get("label") or d.get("fstype") or ""
        removable = d.get("rm") in (True, "1", 1) or (d.get("tran") or (parent or {}).get("tran")) == "usb"
        desc = ("Disco" if d["type"] == "disk" else "Partizione")
        if d.get("tran"):
            desc += f" {d['tran'].upper()}"
        if removable:
            desc += " rimovibile"
        devs.append({"path": d["path"], "name": d["name"], "type": d["type"], "size": size, "model": model,
                     "serial": (d.get("serial") or "").strip(), "removable": removable,
                     "parent": parent["path"] if parent else None, "fstype": d.get("fstype") or "",
                     "label": d.get("label") or "",
                     "mounts": mounts_of(d), "text": f"{d['path']}  —  {model or desc}  —  {eng.human(size)}"
                     + (f"  ({extra})" if extra and d["type"] == "part" else "")})
        for c in d.get("children") or []:
            walk(c, d)

    try:
        for d in json.loads(out.stdout).get("blockdevices", []):
            walk(d)
    except ValueError:
        pass
    return devs


def shipped_iso():
    """Immagine BKHD Live installata con il pacchetto .deb (la piu' recente)."""
    try:
        isos = sorted(f for f in os.listdir(eng.SHIPPED_ISO_DIR) if f.endswith(".iso"))
    except OSError:
        return None
    return os.path.join(eng.SHIPPED_ISO_DIR, isos[-1]) if isos else None


USB_FS = [("exfat", "exFAT — consigliato (Windows, macOS, Linux; file oltre 4 GB)"),
          ("ext4", "ext4 — solo Linux, il più sicuro per BKHD"),
          ("vfat", "FAT32 — massima compatibilità, file fino a 4 GB"),
          ("ntfs", "NTFS — Windows")]
DATA_FS = [("exfat", "exFAT (leggibile anche da Windows e macOS)"), ("ext4", "ext4 (solo Linux)")]


def find_terminal(cmd):
    """Comando per aprire 'cmd' (lista) in un terminale."""
    for term, pre in (("x-terminal-emulator", ["-e"]), ("ptyxis", ["--"]), ("gnome-terminal", ["--"]),
                      ("kgx", ["--"]), ("xfce4-terminal", ["-x"]), ("konsole", ["-e"]),
                      ("mate-terminal", ["-x"]), ("tilix", ["-e"]), ("xterm", ["-e"])):
        exe = shutil.which(term)
        if exe:
            return [exe] + pre + cmd
    return None


_SYSTEM_THEME = []
_PROVIDER = []


def resolve_theme(mode):
    """'auto' segue il desktop: tema scuro del sistema -> notte, altrimenti giorno."""
    if mode in ("giorno", "notte"):
        return mode
    settings = Gtk.Settings.get_default()
    if not _SYSTEM_THEME:
        _SYSTEM_THEME.append((settings.get_property("gtk-theme-name") or "",
                              settings.get_property("gtk-application-prefer-dark-theme")))
    name, prefer_dark = _SYSTEM_THEME[0]
    return "notte" if prefer_dark or "dark" in name.lower() else "giorno"


def apply_theme(mode="auto"):
    """Tema "nuvola di giorno" (chiaro) o "nuvola di notte" (scuro), indipendente dal desktop."""
    real = resolve_theme(mode)
    settings = Gtk.Settings.get_default()
    base = "Yaru" + ("-dark" if real == "notte" else "")
    themes = [d for d in ("/usr/share/themes", os.path.expanduser("~/.themes"))]
    if any(os.path.isdir(os.path.join(d, base)) for d in themes):
        settings.set_property("gtk-theme-name", base)
        settings.set_property("gtk-application-prefer-dark-theme", False)
    else:
        settings.set_property("gtk-theme-name", "Adwaita")
        settings.set_property("gtk-application-prefer-dark-theme", real == "notte")
    C.clear()
    C.update(PALETTE[real])
    css = CSS_NOTTE if real == "notte" else CSS_GIORNO
    banner = first_file(BANNER_FILES[real])
    if banner:
        css += '\n.banner { background-image: url("file://%s"); }\n' % banner
    screen = Gdk.Screen.get_default()
    if _PROVIDER:
        Gtk.StyleContext.remove_provider_for_screen(screen, _PROVIDER.pop())
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _PROVIDER.append(provider)
    return real


class BkhdWindow(Gtk.Window):
    def __init__(self):
        self.settings = load_settings()
        self.theme = apply_theme(self.settings.get("tema", "auto"))
        super().__init__(title=APP_NAME)
        self.get_style_context().add_class("bkhd")
        self.set_default_size(1000, 820)
        if Gtk.IconTheme.get_default().has_icon(ICON_NAME):
            self.set_icon_name(ICON_NAME)
        elif first_file(ICON_FILES):
            self.set_icon_list([self.load_icon(s) for s in (16, 32, 48, 64, 128, 256)])
        self.proc = None
        self.devices = []
        self.backups = []
        self.interrupted = []
        self.rclone = shutil.which("rclone")
        self.name_edited = False
        self.op_mode = None
        self.t_start = 0
        self.overall_start = None
        self.final_event = None
        self.scan_result = None
        self.extra_sources = []
        self.ram_ok = True
        self.n_chunks = 0
        self.syncing_size = False
        self.gtk_settings = Gtk.Settings.get_default()
        self.base_dpi = self.gtk_settings.get_property("gtk-xft-dpi")
        if self.base_dpi <= 0:
            self.base_dpi = 96 * 1024
        self.zoom = 1.0
        self.connect("delete-event", self.on_close)
        self.connect("key-press-event", self.on_key)

        hb = Gtk.HeaderBar(show_close_button=True, title=APP_NAME)
        self.set_titlebar(hb)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.switcher = Gtk.StackSwitcher(stack=self.stack)
        hb.set_custom_title(self.switcher)
        self.menu_btn = Gtk.MenuButton(popover=self.build_menu(), tooltip_text="Menu")
        self.menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(self.menu_btn)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self.build_banner(), False, False, 0)
        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, show_close_button=False)
        self.infobar_label = Gtk.Label(wrap=True, xalign=0)
        self.infobar.get_content_area().add(self.infobar_label)
        self.infobar.set_no_show_all(True)
        root.pack_start(self.infobar, False, False, 0)
        root.pack_start(self.stack, True, True, 0)
        self.stack.add_titled(self.build_backup_page(), "backup", "Backup")
        self.stack.add_titled(self.build_restore_page(), "restore", "Ripristino")
        self.stack.add_titled(self.build_live_page(), "live", "Pennetta e DVD")
        root.pack_start(self.build_progress_area(), False, False, 0)

        for combo in (self.src_combo, self.dst_combo, self.usb_combo, self.disc_combo):
            for cell in combo.get_cells():
                cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
                cell.set_property("width-chars", 40)
        self.set_zoom(self.settings.get("zoom", 1.0), save=False)
        self.refresh_devices()
        self.refresh_remotes()
        self.update_ram_info()
        self.update_dest_mode()
        self.update_live_state()
        GLib.timeout_add_seconds(3, self.update_ram_info)
        if not self.rclone:
            self.show_infobar("rclone non è installato: per il cloud serve rclone (sudo apt install rclone). "
                              "Puoi comunque salvare i backup su un disco o una cartella locale.")

    # ------------------------------------------------------------------ elementi grafici
    def build_banner(self):
        box = Gtk.Box(spacing=14, margin_start=18, margin_end=18, margin_top=10, margin_bottom=10)
        wrap = Gtk.EventBox()
        wrap.get_style_context().add_class("banner")
        wrap.add(box)
        logo = self.load_icon(56)
        if logo:
            box.pack_start(Gtk.Image.new_from_pixbuf(logo), False, False, 0)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        t = Gtk.Label(label=APP_NAME, xalign=0)
        t.get_style_context().add_class("banner-title")
        st = Gtk.Label(label="Il tuo disco, al sicuro tra le nuvole — copia settore per settore, cloud o disco locale",
                       xalign=0, wrap=True)
        st.get_style_context().add_class("banner-sub")
        vb.pack_start(t, False, False, 0)
        vb.pack_start(st, False, False, 0)
        box.pack_start(vb, True, True, 0)
        ver = Gtk.Label(label=f"versione {eng.VERSION}", valign=Gtk.Align.END)
        ver.get_style_context().add_class("banner-sub")
        box.pack_end(ver, False, False, 0)
        return wrap

    def build_menu(self):
        pop = Gtk.Popover()
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=10)
        title = Gtk.Label(xalign=0)
        title.set_markup("<b>Dimensione del testo</b>")
        vb.pack_start(title, False, False, 0)
        row = Gtk.Box()
        row.get_style_context().add_class("linked")
        b_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic", Gtk.IconSize.BUTTON)
        b_out.set_tooltip_text("Riduci  (Ctrl −)")
        b_out.connect("clicked", lambda *_: self.zoom_step(-1))
        self.zoom_btn = Gtk.Button(label="100 %", tooltip_text="Dimensione normale  (Ctrl 0)")
        self.zoom_btn.connect("clicked", lambda *_: self.set_zoom(1.0))
        b_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic", Gtk.IconSize.BUTTON)
        b_in.set_tooltip_text("Ingrandisci  (Ctrl +)")
        b_in.connect("clicked", lambda *_: self.zoom_step(1))
        row.pack_start(b_out, False, False, 0)
        row.pack_start(self.zoom_btn, True, True, 0)
        row.pack_start(b_in, False, False, 0)
        vb.pack_start(row, False, False, 0)
        vb.pack_start(Gtk.Separator(margin_top=6, margin_bottom=4), False, False, 0)
        title = Gtk.Label(xalign=0)
        title.set_markup("<b>Tema</b>")
        vb.pack_start(title, False, False, 0)
        self.theme_buttons = {}
        group = None
        saved = self.settings.get("tema", "auto")
        for tid, text in THEMES:
            rb = Gtk.RadioButton(label=text, group=group)
            group = group or rb
            rb.set_active(tid == saved)
            rb.connect("toggled", lambda b, tid=tid: b.get_active() and self.set_theme(tid))
            self.theme_buttons[tid] = rb
            vb.pack_start(rb, False, False, 0)
        vb.pack_start(Gtk.Separator(margin_top=6, margin_bottom=4), False, False, 0)
        for label, cb in (("Guida illustrata (PDF)", self.on_guide), ("Informazioni su BKHD Backup", self.on_about)):
            mb = Gtk.ModelButton(text=label)
            mb.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            vb.pack_start(mb, False, False, 0)
        vb.show_all()
        pop.add(vb)
        return pop

    def page(self, *cards):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=16)
        for c in cards:
            vb.pack_start(c, False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(vb)
        return sw

    def card(self, title, icon):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.get_style_context().add_class("card")
        head = Gtk.Box(spacing=8)
        img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        img.get_style_context().add_class("card-icon")
        head.pack_start(img, False, False, 0)
        lbl = Gtk.Label(label=title, xalign=0)
        lbl.get_style_context().add_class("card-title")
        head.pack_start(lbl, False, False, 0)
        box.pack_start(head, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        box.pack_start(grid, False, False, 0)
        return box, grid

    def field(self, text, group):
        lbl = Gtk.Label(label=text, xalign=1)
        lbl.get_style_context().add_class("field")
        group.add_widget(lbl)
        return lbl

    @staticmethod
    def hint(text, cls="dim"):
        lbl = Gtk.Label(xalign=0, wrap=True)
        lbl.set_markup(f"<small>{text}</small>")
        lbl.get_style_context().add_class(cls)
        return lbl

    @staticmethod
    def button(label, cls=None, icon=None, tooltip=None):
        b = Gtk.Button(label=label) if label else Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        if label and icon:
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            b.set_always_show_image(True)
        if cls:
            b.get_style_context().add_class(cls)
        if tooltip:
            b.set_tooltip_text(tooltip)
        return b

    def remote_row(self):
        box = Gtk.Box(spacing=6, hexpand=True)
        combo = Gtk.ComboBoxText.new_with_entry()
        combo.set_hexpand(True)
        combo.get_child().set_placeholder_text("es. gdrive:   onedrive:   s3:bucket")
        if self.settings.get("remote"):
            combo.get_child().set_text(self.settings["remote"])
        box.pack_start(combo, True, True, 0)
        r = self.button(None, icon="view-refresh-symbolic", tooltip="Aggiorna l'elenco dei cloud configurati")
        r.connect("clicked", lambda *_: self.refresh_remotes())
        box.pack_start(r, False, False, 0)
        cfg = self.button("Configura cloud…", "soft", tooltip="Apre 'rclone config' per collegare Google Drive, "
                          "OneDrive, Dropbox, Mega, pCloud, S3, Backblaze B2, WebDAV, SFTP e molti altri")
        cfg.connect("clicked", self.on_configure_cloud)
        box.pack_start(cfg, False, False, 0)
        return box, combo

    def folder_row(self, key, placeholder):
        box = Gtk.Box(spacing=6, hexpand=True)
        entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder, text=self.settings.get(key, ""))
        box.pack_start(entry, True, True, 0)
        b = self.button("Sfoglia…", "soft")
        b.connect("clicked", lambda *_: self.pick_folder(entry))
        box.pack_start(b, False, False, 0)
        return box, entry

    # ------------------------------------------------------------------ scheda Backup
    def build_backup_page(self):
        sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        c1, g = self.card("1. Disco da copiare", "drive-harddisk-symbolic")
        g.attach(self.field("Sorgente", sg), 0, 0, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.src_combo = Gtk.ComboBoxText(hexpand=True)
        self.src_combo.connect("changed", self.on_source_changed)
        box.pack_start(self.src_combo, True, True, 0)
        b = self.button(None, icon="view-refresh-symbolic", tooltip="Aggiorna l'elenco dei dischi")
        b.connect("clicked", lambda *_: self.refresh_devices())
        box.pack_start(b, False, False, 0)
        b = self.button("File immagine…", "soft")
        b.connect("clicked", self.on_pick_source_image)
        box.pack_start(b, False, False, 0)
        b = self.button("Controlla disco…", "soft", "dialog-information-symbolic",
                        "Legge tutti i settori e lo stato SMART per scoprire settori danneggiati")
        b.connect("clicked", self.on_scan)
        box.pack_start(b, False, False, 0)
        g.attach(box, 1, 0, 1, 1)
        self.src_warn = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.src_warn, 1, 1, 1, 1)

        c2, g = self.card("2. Dove salvare il backup", "weather-overcast-symbolic")
        g.attach(self.field("Destinazione", sg), 0, 0, 1, 1)
        rb = Gtk.Box(spacing=18)
        self.b_rb_cloud = Gtk.RadioButton(label="Cloud (tramite la RAM, nessuna cache su disco)")
        self.b_rb_local = Gtk.RadioButton(label="Disco o cartella locale (scrittura diretta)",
                                          group=self.b_rb_cloud)
        rb.pack_start(self.b_rb_cloud, False, False, 0)
        rb.pack_start(self.b_rb_local, False, False, 0)
        g.attach(rb, 1, 0, 1, 1)
        g.attach(self.field("Cloud", sg), 0, 1, 1, 1)
        self.b_cloud_box, self.b_remote = self.remote_row()
        g.attach(self.b_cloud_box, 1, 1, 1, 1)
        g.attach(self.field("Disco/cartella", sg), 0, 2, 1, 1)
        self.b_local_box, self.b_local = self.folder_row("local_dest", "es. /media/utente/DiscoEsterno")
        g.attach(self.b_local_box, 1, 2, 1, 1)
        g.attach(self.field("Cartella", sg), 0, 3, 1, 1)
        self.b_path = Gtk.Entry(text=self.settings.get("path", "BKHD"), hexpand=True)
        g.attach(self.b_path, 1, 3, 1, 1)
        g.attach(self.field("Nome del backup", sg), 0, 4, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.b_name = Gtk.Entry(hexpand=True)
        self.b_name.connect("changed", lambda *_: setattr(self, "name_edited", self.b_name.has_focus()
                                                          or self.name_edited))
        box.pack_start(self.b_name, True, True, 0)
        self.btn_find_int = self.button("Cerca backup interrotti", "soft", "edit-find-symbolic",
                                        "Elenca i backup interrotti nella destinazione per riprenderli")
        self.btn_find_int.connect("clicked", self.on_search_interrupted)
        box.pack_start(self.btn_find_int, False, False, 0)
        g.attach(box, 1, 4, 1, 1)
        if self.b_rb_local.get_active() != (self.settings.get("dest_mode") == "local"):
            self.b_rb_local.set_active(self.settings.get("dest_mode") == "local")
        for w in (self.b_rb_cloud, self.b_rb_local):
            w.connect("toggled", lambda *_: self.update_dest_mode())
        # elenco dei backup interrotti
        self.int_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        ivb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.int_store = Gtk.ListStore(str, str, str, str, str, str, int)
        self.int_tree = Gtk.TreeView(model=self.int_store)
        for i, title in enumerate(["Nome", "Iniziato", "Disco originale", "Pacchetti salvati", "Protetto", "Opzioni"]):
            cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END if i in (0, 2, 5)
                                        else Pango.EllipsizeMode.NONE)
            col = Gtk.TreeViewColumn(title, cell, text=i)
            col.set_resizable(True)
            col.set_expand(i in (0, 2))
            self.int_tree.append_column(col)
        self.int_tree.get_selection().connect("changed", lambda *_: self.update_resume_state())
        self.int_tree.connect("row-activated", lambda *_: self.on_resume_interrupted())
        sw = Gtk.ScrolledWindow(min_content_height=120, hexpand=True)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        sw.add(self.int_tree)
        ivb.pack_start(sw, True, True, 0)
        hb = Gtk.Box(spacing=8)
        self.int_info = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.int_info.get_style_context().add_class("dim")
        hb.pack_start(self.int_info, True, True, 0)
        self.btn_resume = self.button("Riprendi backup interrotto", "cloud", "media-playback-start-symbolic")
        self.btn_resume.connect("clicked", lambda *_: self.on_resume_interrupted())
        hb.pack_end(self.btn_resume, False, False, 0)
        ivb.pack_start(hb, False, False, 0)
        self.int_revealer.add(ivb)
        g.attach(self.int_revealer, 1, 5, 1, 1)

        c3, g = self.card("3. Pacchetti e opzioni", "preferences-system-symbolic")
        g.attach(self.field("Dimensione pacchetti", sg), 0, 0, 1, 1)
        box = Gtk.Box(spacing=6)
        self.preset_combo = Gtk.ComboBoxText()
        for text, kib in PRESETS:
            self.preset_combo.append(str(kib), text)
        self.preset_combo.append("custom", "Personalizzata")
        self.preset_combo.connect("changed", self.on_preset_changed)
        box.pack_start(self.preset_combo, False, False, 0)
        adj = Gtk.Adjustment(value=4096, lower=eng.MIN_CHUNK_KIB / 1024, upper=eng.MAX_CHUNK_KIB / 1024,
                             step_increment=1, page_increment=100)
        self.chunk_spin = Gtk.SpinButton(adjustment=adj, numeric=True, digits=2, width_chars=8)
        self.chunk_spin.connect("value-changed", self.on_chunk_changed)
        box.pack_start(self.chunk_spin, False, False, 0)
        self.size_note = Gtk.Label(label="MB  (da 1,2 MB a 8 GB)")
        box.pack_start(self.size_note, False, False, 0)
        g.attach(box, 1, 0, 1, 1)
        self.ram_label = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.ram_label, 1, 1, 1, 1)

        g.attach(self.field("Opzioni", sg), 0, 2, 1, 1)
        box = Gtk.Box(spacing=8)
        self.zstd_check = Gtk.CheckButton(label="Compressione zstd, livello")
        self.zstd_check.set_tooltip_text("Comprime ogni pacchetto: lo spazio vuoto del disco occupa quasi zero.")
        box.pack_start(self.zstd_check, False, False, 0)
        self.zstd_level = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=3, lower=1, upper=19, step_increment=1))
        box.pack_start(self.zstd_level, False, False, 0)
        if shutil.which("zstd"):
            self.zstd_check.set_active(True)
        else:
            self.zstd_check.set_sensitive(False)
            self.zstd_check.set_label("Compressione zstd (installa il pacchetto 'zstd')")
        g.attach(box, 1, 2, 1, 1)
        self.used_check = Gtk.CheckButton(label="Copia solo i settori usati (salta lo spazio libero dei file system)")
        self.used_check.set_tooltip_text(
            "Legge la mappa dei blocchi occupati: ext2/3/4 sempre; NTFS, FAT, exFAT, Btrfs, XFS con partclone. "
            "Partizioni montate, cifrate o sconosciute vengono copiate per intero. Lo spazio libero non viene "
            "letto, salvato né riscritto al ripristino.")
        g.attach(self.used_check, 1, 3, 1, 1)
        if not shutil.which("partclone.ntfs"):
            g.attach(self.hint("Per NTFS, FAT, exFAT, Btrfs e XFS installa <b>partclone</b> "
                               "(sudo apt install partclone); ext2/3/4 funziona sempre."), 1, 4, 1, 1)
        self.enc_check = Gtk.CheckButton(label="Proteggi il backup con una password (cifratura AES-256)")
        self.enc_check.connect("toggled", lambda *_: self.enc_revealer.set_reveal_child(self.enc_check.get_active()))
        g.attach(self.enc_check, 1, 5, 1, 1)
        self.enc_revealer = Gtk.Revealer()
        eb = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.pw1 = Gtk.Entry(visibility=False, placeholder_text="Password (almeno 8 caratteri)", hexpand=True)
        self.pw2 = Gtk.Entry(visibility=False, placeholder_text="Ripeti la password", hexpand=True)
        self.pw1.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.pw2.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        eb.attach(self.pw1, 0, 0, 1, 1)
        eb.attach(self.pw2, 1, 0, 1, 1)
        self.pw_note = self.hint("La password non viene salvata da nessuna parte: <b>se la dimentichi il backup "
                                 "non sarà più recuperabile</b>.", "warn-text")
        eb.attach(self.pw_note, 0, 1, 2, 1)
        for e in (self.pw1, self.pw2):
            e.connect("changed", lambda *_: self.update_pw_note())
        self.enc_revealer.add(eb)
        g.attach(self.enc_revealer, 1, 6, 1, 1)
        if not eng.crypto_available():
            self.enc_check.set_sensitive(False)
            self.enc_check.set_label("Protezione con password (installa python3-cryptography)")
        g.attach(self.field("Settori danneggiati", sg), 0, 7, 1, 1)
        self.bad_combo = Gtk.ComboBoxText()
        for mid, text in BAD_MODES:
            self.bad_combo.append(mid, text)
        self.bad_combo.set_active_id("retry")
        g.attach(self.bad_combo, 1, 7, 1, 1)
        self.resume_check = Gtk.CheckButton(label="Riprendi un backup interrotto con lo stesso nome")
        g.attach(self.resume_check, 1, 8, 1, 1)
        g.attach(self.hint(
            "Con il cloud ogni pacchetto viene preparato in una cartella <b>montata in RAM</b>, caricato e, solo a "
            "upload terminato e verificato, <b>cancellato dalla RAM</b>. Con un disco locale i pacchetti vengono "
            "scritti direttamente sulla destinazione. Per copiare il disco di sistema in modo coerente usa la "
            "<b>pennetta avviabile</b>."), 1, 9, 1, 1)

        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btn_backup = self.button("Avvia backup", "cloud", "document-send-symbolic")
        self.btn_backup.connect("clicked", lambda *_: self.on_start_backup())
        bar.pack_end(self.btn_backup, False, False, 0)
        self.preset_combo.set_active_id(str(4096 * 1024))
        return self.page(c1, c2, c3, bar)

    # ------------------------------------------------------------------ scheda Ripristino
    def build_restore_page(self):
        sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        c1, g = self.card("1. Dove si trova il backup", "weather-overcast-symbolic")
        g.attach(self.field("Origine", sg), 0, 0, 1, 1)
        rb = Gtk.Box(spacing=18)
        self.r_rb_cloud = Gtk.RadioButton(label="Cloud")
        self.r_rb_local = Gtk.RadioButton(label="Disco o cartella locale", group=self.r_rb_cloud)
        rb.pack_start(self.r_rb_cloud, False, False, 0)
        rb.pack_start(self.r_rb_local, False, False, 0)
        g.attach(rb, 1, 0, 1, 1)
        g.attach(self.field("Cloud", sg), 0, 1, 1, 1)
        self.r_cloud_box, self.r_remote = self.remote_row()
        g.attach(self.r_cloud_box, 1, 1, 1, 1)
        g.attach(self.field("Disco/cartella", sg), 0, 2, 1, 1)
        self.r_local_box, self.r_local = self.folder_row("local_dest", "es. /media/utente/DiscoEsterno")
        g.attach(self.r_local_box, 1, 2, 1, 1)
        g.attach(self.field("Cartella", sg), 0, 3, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.r_path = Gtk.Entry(text=self.settings.get("path", "BKHD"), hexpand=True)
        box.pack_start(self.r_path, True, True, 0)
        self.btn_search = self.button("Cerca backup", "cloud", "edit-find-symbolic")
        self.btn_search.connect("clicked", self.on_search_backups)
        box.pack_start(self.btn_search, False, False, 0)
        g.attach(box, 1, 3, 1, 1)
        if self.settings.get("dest_mode") == "local":
            self.r_rb_local.set_active(True)
        for w in (self.r_rb_cloud, self.r_rb_local):
            w.connect("toggled", lambda *_: self.update_dest_mode())

        c2, g = self.card("2. Backup trovati", "document-open-recent-symbolic")
        # nome, data, sorgente, dimensione, pacchetti, occupati, protetto, stato, indice
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, int)
        self.tree = Gtk.TreeView(model=self.store)
        for i, title in enumerate(["Nome", "Data", "Disco originale", "Dimensione", "Pacchetti", "Occupati",
                                   "🔒", "Stato"]):
            cell = Gtk.CellRendererText()
            if i == 0:
                cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
                cell.set_property("width-chars", 22)
            elif i == 2:
                cell.set_property("ellipsize", Pango.EllipsizeMode.END)
                cell.set_property("width-chars", 10)
            col = Gtk.TreeViewColumn(title, cell, text=i)
            col.set_resizable(True)
            if i == 0:
                col.set_expand(True)
            self.tree.append_column(col)
        self.tree.get_selection().connect("changed", self.on_backup_selected)
        sw = Gtk.ScrolledWindow(min_content_height=150, hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        sw.add(self.tree)
        g.attach(sw, 0, 0, 2, 1)
        self.backup_info = Gtk.Label(xalign=0, wrap=True, selectable=True)
        g.attach(self.backup_info, 0, 1, 2, 1)
        self.r_pw_label = self.field("Password", sg)
        self.r_pw = Gtk.Entry(visibility=False, hexpand=True, placeholder_text="Password del backup protetto")
        self.r_pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.r_pw.connect("changed", lambda *_: self.update_restore_state())
        g.attach(self.r_pw_label, 0, 2, 1, 1)
        g.attach(self.r_pw, 1, 2, 1, 1)

        c3, g = self.card("3. Destinazione del ripristino", "drive-harddisk-symbolic")
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.rb_disk = Gtk.RadioButton(label="Disco:")
        hb = Gtk.Box(spacing=6)
        hb.pack_start(self.rb_disk, False, False, 0)
        self.dst_combo = Gtk.ComboBoxText(hexpand=True)
        self.dst_combo.connect("changed", lambda *_: self.update_restore_state())
        hb.pack_start(self.dst_combo, True, True, 0)
        vb.pack_start(hb, False, False, 0)
        self.rb_file = Gtk.RadioButton(label="File immagine:", group=self.rb_disk)
        hb = Gtk.Box(spacing=6)
        hb.pack_start(self.rb_file, False, False, 0)
        self.dst_file = Gtk.Entry(hexpand=True, placeholder_text="/percorso/disco-ripristinato.img")
        self.dst_file.connect("changed", lambda *_: self.update_restore_state())
        hb.pack_start(self.dst_file, True, True, 0)
        b = self.button("Sfoglia…", "soft")
        b.connect("clicked", self.on_pick_target_file)
        hb.pack_start(b, False, False, 0)
        vb.pack_start(hb, False, False, 0)
        self.rb_verify = Gtk.RadioButton(label="Solo verifica integrità (legge e controlla, non scrive nulla)",
                                         group=self.rb_disk)
        vb.pack_start(self.rb_verify, False, False, 0)
        for r in (self.rb_disk, self.rb_file, self.rb_verify):
            r.connect("toggled", lambda *_: self.update_restore_state())
        g.attach(vb, 0, 0, 1, 1)
        self.dst_warn = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.dst_warn, 0, 1, 1, 1)
        g.attach(self.hint("Ogni pacchetto viene verificato (SHA-256 e, se protetto, sigillo AES-GCM) prima di essere "
                           "scritto. <b>Il ripristino cancella tutti i dati presenti sul disco di destinazione.</b>"),
                 0, 2, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btn_restore = self.button("Avvia ripristino", "danger", "document-revert-symbolic")
        self.btn_restore.connect("clicked", self.on_start_restore)
        bar.pack_end(self.btn_restore, False, False, 0)
        self.update_restore_state()
        return self.page(c1, c2, c3, bar)

    # ------------------------------------------------------------------ scheda Pennetta avviabile
    def build_live_page(self):
        sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.copying = None
        intro, g = self.card("BKHD Live — per copiare e ripristinare anche il disco di sistema", "computer-symbolic")
        g.attach(self.hint(
            "BKHD Live è un <b>Ubuntu 24.04 minimale</b> che si avvia da chiavetta o DVD con <b>BKHD Backup</b>, "
            "<b>Firefox</b>, rete <b>via cavo e Wi-Fi</b> e strumenti di diagnostica (GParted, GSmartControl, "
            "TestDisk/PhotoRec, ddrescue, Memtest86+). Funziona su PC con BIOS e UEFI, anche con <b>Secure Boot</b>. "
            "L'immagine è già inclusa nel programma: scegli dove metterla."), 0, 0, 2, 1)

        # 1. immagine
        c1, g = self.card("1. Immagine BKHD Live", "media-optical-symbolic")
        g.attach(self.field("Immagine ISO", sg), 0, 0, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.build_iso = os.path.join(os.path.expanduser("~"), "BKHD-Live", f"bkhd-live-{eng.VERSION}.iso")
        saved = self.settings.get("live_iso")
        start = saved if saved and os.path.isfile(saved) else (shipped_iso() or self.build_iso)
        self.live_iso = Gtk.Entry(hexpand=True, text=start)
        self.live_iso.connect("changed", lambda *_: self.update_live_state())
        box.pack_start(self.live_iso, True, True, 0)
        b = self.button("Scegli…", "soft", tooltip="Usa un'altra immagine ISO di BKHD Live")
        b.connect("clicked", self.on_pick_iso)
        box.pack_start(b, False, False, 0)
        g.attach(box, 1, 0, 1, 1)
        self.live_status = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.live_status, 1, 1, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        b = self.button("Apri cartella", "soft", "folder-open-symbolic")
        b.connect("clicked", lambda *_: self.open_path(os.path.dirname(self.live_iso.get_text())))
        bar.pack_start(b, False, False, 0)
        self.btn_save_iso = self.button("Salva immagine ISO…", "cloud", "document-save-as-symbolic",
                                        "Copia l'immagine dove vuoi: per una macchina virtuale, Ventoy, "
                                        "un altro PC o per masterizzarla più tardi")
        self.btn_save_iso.connect("clicked", self.on_save_iso)
        bar.pack_start(self.btn_save_iso, False, False, 0)
        g.attach(bar, 1, 2, 1, 1)
        exp = Gtk.Expander(label="Crea o aggiorna l'immagine (avanzato: serve Internet)")
        eg = Gtk.Grid(column_spacing=12, row_spacing=10, margin_top=8)
        eg.attach(self.field("Cartella di lavoro", sg), 0, 0, 1, 1)
        box, self.live_work = self.folder_row("live_work", "/var/cache/bkhd-live")
        if not self.live_work.get_text() or self.live_work.get_text() == "/var/tmp/bkhd-live":
            self.live_work.set_text("/var/cache/bkhd-live")
        eg.attach(box, 1, 0, 1, 1)
        eg.attach(self.hint("Scarica circa 2 GB dai server ufficiali di Ubuntu e Mozilla (da 20 a 60 minuti) e crea "
                            f"<b>{GLib.markup_escape_text(self.build_iso)}</b>. Serve solo per avere i pacchetti più "
                            "aggiornati: l'immagine inclusa funziona già."), 1, 1, 1, 1)
        self.btn_live_build = self.button("Crea immagine BKHD Live", "soft", "system-run-symbolic")
        self.btn_live_build.set_halign(Gtk.Align.END)
        self.btn_live_build.connect("clicked", self.on_live_build)
        eg.attach(self.btn_live_build, 1, 2, 1, 1)
        exp.add(eg)
        g.attach(exp, 0, 3, 2, 1)

        # 2. pennetta USB
        c2, g = self.card("2. Pennetta USB", "drive-removable-media-symbolic")
        g.attach(self.field("Chiavetta", sg), 0, 0, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.usb_combo = Gtk.ComboBoxText(hexpand=True)
        self.usb_combo.connect("changed", lambda *_: self.update_live_state())
        box.pack_start(self.usb_combo, True, True, 0)
        b = self.button(None, icon="view-refresh-symbolic", tooltip="Aggiorna l'elenco delle chiavette")
        b.connect("clicked", lambda *_: self.refresh_devices())
        box.pack_start(b, False, False, 0)
        self.btn_eject = self.button("Espelli", "soft", "media-eject-symbolic",
                                     "Smonta la chiavetta e la spegne: poi si può togliere in sicurezza")
        self.btn_eject.connect("clicked", self.on_eject)
        box.pack_start(self.btn_eject, False, False, 0)
        g.attach(box, 1, 0, 1, 1)
        self.usb_info = Gtk.Label(xalign=0, wrap=True, selectable=True)
        g.attach(self.usb_info, 1, 1, 1, 1)
        self.usb_warn = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.usb_warn, 1, 2, 1, 1)

        sub = Gtk.Label(xalign=0)
        sub.set_markup("<b>Pennetta avviabile BKHD Live</b>")
        g.attach(sub, 1, 3, 1, 1)
        g.attach(self.hint("La chiavetta diventa la pennetta live per fare backup e ripristini avviando il PC da "
                           "essa. <b>Tutto il contenuto attuale della chiavetta verrà cancellato.</b>"), 1, 4, 1, 1)
        box = Gtk.Box(spacing=8)
        self.data_check = Gtk.CheckButton(label="In più, usa lo spazio rimanente per salvare i backup (facoltativo)")
        self.data_check.set_tooltip_text("Crea una partizione «BKHD-DATI» dopo il sistema live: in BKHD Live viene "
                                         "proposta da sola come destinazione dei backup.")
        self.data_check.connect("toggled", lambda *_: self.update_live_state())
        box.pack_start(self.data_check, False, False, 0)
        self.data_fs = Gtk.ComboBoxText()
        for fid, text in DATA_FS:
            self.data_fs.append(fid, text)
        self.data_fs.set_active_id("exfat")
        box.pack_start(self.data_fs, False, False, 0)
        g.attach(box, 1, 5, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btn_live_write = self.button("Crea pennetta BKHD Live", "danger", "media-removable-symbolic")
        self.btn_live_write.connect("clicked", self.on_live_write)
        bar.pack_start(self.btn_live_write, False, False, 0)
        g.attach(bar, 1, 6, 1, 1)

        g.attach(Gtk.Separator(margin_top=4, margin_bottom=4), 1, 7, 1, 1)
        sub = Gtk.Label(xalign=0)
        sub.set_markup("<b>Oppure: chiavetta per i soli backup</b>")
        g.attach(sub, 1, 8, 1, 1)
        g.attach(self.field("Formato", sg), 0, 9, 1, 1)
        box = Gtk.Box(spacing=6)
        self.fmt_fs = Gtk.ComboBoxText()
        for fid, text in USB_FS:
            self.fmt_fs.append(fid, text + ("" if shutil.which(eng.FS_TOOLS[fid]) else "  (programma mancante)"))
        self.fmt_fs.set_active_id("exfat")
        box.pack_start(self.fmt_fs, False, False, 0)
        box.pack_start(Gtk.Label(label="Etichetta"), False, False, 0)
        self.fmt_label = Gtk.Entry(text=eng.DATA_LABEL, max_length=11, width_chars=12)
        box.pack_start(self.fmt_label, False, False, 0)
        g.attach(box, 1, 9, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btn_use_dest = self.button("Usa come destinazione dei backup", "soft", "emblem-default-symbolic",
                                        "Monta la chiavetta e la imposta come destinazione nella scheda Backup")
        self.btn_use_dest.connect("clicked", self.on_use_as_dest)
        bar.pack_start(self.btn_use_dest, False, False, 0)
        self.btn_format = self.button("Formatta", "danger", "edit-clear-all-symbolic")
        self.btn_format.connect("clicked", self.on_usb_format)
        bar.pack_start(self.btn_format, False, False, 0)
        g.attach(bar, 1, 10, 1, 1)

        # 3. DVD
        c3, g = self.card("3. DVD avviabile", "media-optical-symbolic")
        g.attach(self.field("Masterizzatore", sg), 0, 0, 1, 1)
        box = Gtk.Box(spacing=6, hexpand=True)
        self.disc_combo = Gtk.ComboBoxText(hexpand=True)
        self.disc_combo.connect("changed", lambda *_: self.update_live_state())
        box.pack_start(self.disc_combo, True, True, 0)
        b = self.button(None, icon="view-refresh-symbolic", tooltip="Aggiorna l'elenco delle unità CD/DVD")
        b.connect("clicked", lambda *_: self.refresh_devices())
        box.pack_start(b, False, False, 0)
        g.attach(box, 1, 0, 1, 1)
        self.disc_info = Gtk.Label(xalign=0, wrap=True)
        g.attach(self.disc_info, 1, 1, 1, 1)
        g.attach(self.hint("L'immagine occupa circa 1,3 GB: serve un <b>DVD</b> (4,7 GB, anche riscrivibile). "
                           "<b>Non entra in un CD</b> da 700 MB. Dopo la masterizzazione il disco viene riletto "
                           "e verificato."), 1, 2, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btn_burn = self.button("Masterizza DVD", "danger", "media-optical-symbolic")
        self.btn_burn.connect("clicked", self.on_disc_burn)
        bar.pack_start(self.btn_burn, False, False, 0)
        g.attach(bar, 1, 3, 1, 1)
        return self.page(intro, c1, c2, c3)

    def build_progress_area(self):
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                     margin_top=10, margin_bottom=10)
        wrap = Gtk.EventBox()
        wrap.get_style_context().add_class("statusbox")
        wrap.add(vb)
        hb = Gtk.Box(spacing=12)
        self.lbl_status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.lbl_status.get_style_context().add_class("status-title")
        self.lbl_status.set_text("Pronto.")
        hb.pack_start(self.lbl_status, True, True, 0)
        self.btn_cancel = self.button("Annulla", "soft", "process-stop-symbolic")
        self.btn_cancel.set_sensitive(False)
        self.btn_cancel.connect("clicked", self.on_cancel)
        hb.pack_start(self.btn_cancel, False, False, 0)
        vb.pack_start(hb, False, False, 0)
        self.bar_chunk = Gtk.ProgressBar(show_text=True, text="Fase corrente")
        self.bar_total = Gtk.ProgressBar(show_text=True, text="Totale")
        vb.pack_start(self.bar_chunk, False, False, 0)
        vb.pack_start(self.bar_total, False, False, 0)
        self.lbl_eta = Gtk.Label(xalign=0)
        vb.pack_start(self.lbl_eta, False, False, 0)
        self.lbl_detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.lbl_detail.get_style_context().add_class("dim")
        vb.pack_start(self.lbl_detail, False, False, 0)
        exp = Gtk.Expander(label="Registro attività")
        self.logbuf = Gtk.TextBuffer()
        self.logbuf.create_tag("warning", foreground=C["warn"])
        self.logbuf.create_tag("error", foreground=C["err"], weight=Pango.Weight.BOLD)
        self.logbuf.create_tag("ok", foreground=C["ok"], weight=Pango.Weight.BOLD)
        self.logview = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow(min_content_height=150)
        sw.add(self.logview)
        exp.add(sw)
        vb.pack_start(exp, False, False, 0)
        return wrap

    # ------------------------------------------------------------------ utilita'
    def load_icon(self, size):
        try:
            if Gtk.IconTheme.get_default().has_icon(ICON_NAME):
                return Gtk.IconTheme.get_default().load_icon(ICON_NAME, size, 0)
            f = first_file(ICON_FILES)
            if f:
                return GdkPixbuf.Pixbuf.new_from_file_at_size(f, size, size)
        except GLib.Error:
            pass
        return None

    def set_zoom(self, z, save=True):
        z = min(ZOOM_LEVELS, key=lambda lv: abs(lv - float(z)))
        self.zoom = z
        self.gtk_settings.set_property("gtk-xft-dpi", int(self.base_dpi * z))
        self.zoom_btn.set_label(f"{round(z * 100)} %")
        if save:
            self.settings["zoom"] = z
            save_settings(self.settings)

    def set_theme(self, mode):
        self.theme = apply_theme(mode)
        self.remember(tema=mode)
        self.update_log_tags()
        self.refresh_colored_texts()

    def update_log_tags(self):
        table = self.logbuf.get_tag_table()
        for name, key in (("warning", "warn"), ("error", "err"), ("ok", "ok")):
            tag = table.lookup(name)
            if tag:
                tag.set_property("foreground", C[key])

    def refresh_colored_texts(self):
        """Ricalcola i testi colorati con i colori del tema attivo."""
        self.update_ram_info()
        self.on_source_changed()
        self.update_restore_state()
        self.update_live_state()
        if self.selected_backup():
            self.on_backup_selected()

    def zoom_step(self, delta):
        i = ZOOM_LEVELS.index(self.zoom) + delta
        self.set_zoom(ZOOM_LEVELS[max(0, min(len(ZOOM_LEVELS) - 1, i))])

    def on_key(self, _w, ev):
        if not ev.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        name = Gdk.keyval_name(ev.keyval) or ""
        if name in ("plus", "equal", "KP_Add"):
            self.zoom_step(1)
        elif name in ("minus", "underscore", "KP_Subtract"):
            self.zoom_step(-1)
        elif name in ("0", "KP_0", "KP_Insert"):
            self.set_zoom(1.0)
        else:
            return False
        return True

    def remember(self, **kw):
        self.settings.update(kw)
        save_settings(self.settings)

    def note_protected(self, items):
        """Ricorda i backup protetti da password: se un giorno risultassero "non protetti" e' una manomissione."""
        known = set(self.settings.get("protetti", []))
        new = {m["backup_id"] for m in items if m.get("encryption") and m.get("backup_id")}
        if new - known:
            self.remember(protetti=sorted(known | new))

    def was_protected(self, m):
        return m.get("backup_id") in set(self.settings.get("protetti", []))

    def show_infobar(self, text):
        self.infobar_label.set_text(text)
        self.infobar.show()
        self.infobar_label.show()

    def message(self, kind, title, text=""):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=title)
        if text:
            d.format_secondary_text(text)
        d.run()
        d.destroy()

    def ask(self, title, text, ok_label="Continua"):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text=title)
        d.format_secondary_text(text)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, ok_label, Gtk.ResponseType.OK)
        res = d.run()
        d.destroy()
        return res == Gtk.ResponseType.OK

    def ask_password(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text=title)
        d.format_secondary_text(text)
        e = Gtk.Entry(visibility=False, activates_default=True)
        e.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        d.get_message_area().pack_start(e, False, False, 0)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Continua", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        res = d.run()
        pw = e.get_text()
        d.destroy()
        return pw if res == Gtk.ResponseType.OK and pw else None

    def log(self, msg, level="info"):
        end = self.logbuf.get_end_iter()
        stamp = datetime.datetime.now().strftime("%H:%M:%S ")
        tag = level if level in ("warning", "error", "ok") else None
        if tag:
            self.logbuf.insert_with_tags_by_name(end, stamp + msg + "\n", tag)
        else:
            self.logbuf.insert(end, stamp + msg + "\n")
        self.logview.scroll_to_mark(self.logbuf.get_insert(), 0, False, 0, 0)

    def device(self, path):
        return next((d for d in self.devices if d["path"] == path), None)

    def pick_folder(self, entry):
        d = Gtk.FileChooserDialog(title="Scegli una cartella", transient_for=self,
                                  action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Scegli", Gtk.ResponseType.OK)
        if entry.get_text() and os.path.isdir(entry.get_text()):
            d.set_current_folder(entry.get_text())
        if d.run() == Gtk.ResponseType.OK:
            entry.set_text(d.get_filename())
        d.destroy()

    def open_path(self, path):
        if path and os.path.isdir(path):
            subprocess.Popen(["xdg-open", path], start_new_session=True)

    def refresh_devices(self):
        self.devices = list_block_devices()
        cur_src = self.src_combo.get_active_id()
        cur_dst = self.dst_combo.get_active_id()
        cur_usb = self.usb_combo.get_active_id()
        cur_disc = self.disc_combo.get_active_id()
        for combo in (self.src_combo, self.dst_combo, self.usb_combo, self.disc_combo):
            combo.remove_all()
        for d in self.devices:
            mark = "  [in uso]" if d["mounts"] else ""
            if d["type"] == "rom":
                self.disc_combo.append(d["path"], d["text"])
                continue
            self.src_combo.append(d["path"], ("" if d["type"] == "disk" else "    ") + d["text"] + mark)
            if d["type"] == "disk":
                self.dst_combo.append(d["path"], d["text"] + mark)
                if d["removable"]:
                    self.usb_combo.append(d["path"], d["text"] + mark)
        for path, text in self.extra_sources:
            self.src_combo.append(path, text)
        if not (cur_src and self.src_combo.set_active_id(cur_src)):
            free = next((d["path"] for d in self.devices if d["type"] == "disk" and not d["mounts"]
                         and not d["removable"]), None) or next(
                (d["path"] for d in self.devices if d["type"] == "disk" and not d["mounts"]), None)
            if not (free and self.src_combo.set_active_id(free)):     # preferisce un disco non in uso
                self.src_combo.set_active(0)
        if cur_dst and self.device(cur_dst):
            self.dst_combo.set_active_id(cur_dst)
        if not (cur_usb and self.usb_combo.set_active_id(cur_usb)):
            self.usb_combo.set_active(0)
        if not (cur_disc and self.disc_combo.set_active_id(cur_disc)):
            self.disc_combo.set_active(0)
        self.update_live_state()

    def refresh_remotes(self):
        if not self.rclone:
            return

        def work():
            try:
                remotes, err = eng.Rclone(self.rclone).listremotes(), None
            except eng.EngineError as e:
                remotes, err = [], str(e)
            GLib.idle_add(done, remotes, err)

        def done(remotes, err):
            for combo in (self.b_remote, self.r_remote):
                text = combo.get_child().get_text()
                combo.remove_all()
                for rm in remotes:
                    combo.append_text(rm)
                if text:
                    combo.get_child().set_text(text)
                elif remotes:
                    combo.set_active(0)
            if err:
                self.log(err, "error")
            elif not remotes:
                self.show_infobar("Nessun cloud configurato: premi «Configura cloud…» per collegarne uno "
                                  "(Google Drive, OneDrive, Dropbox, Mega, S3, …) oppure salva su un disco locale.")
            elif self.rclone:
                self.infobar.hide()
            return False

        threading.Thread(target=work, daemon=True).start()

    def selected_source_size(self):
        path = self.src_combo.get_active_id()
        if not path:
            return 0
        d = self.device(path)
        if d:
            return d["size"]
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def chunk_kib(self):
        pid = self.preset_combo.get_active_id()
        if pid and pid != "custom":
            return int(pid)
        kib = int(round(self.chunk_spin.get_value() * 1024))
        kib = max(eng.MIN_CHUNK_KIB, min(eng.MAX_CHUNK_KIB, kib))
        return kib - kib % 4

    def dest_is_local(self, page="backup"):
        return (self.b_rb_local if page == "backup" else self.r_rb_local).get_active()

    def update_dest_mode(self):
        for rb_local, cloud_box, local_box in ((self.b_rb_local, self.b_cloud_box, self.b_local_box),
                                               (self.r_rb_local, self.r_cloud_box, self.r_local_box)):
            local = rb_local.get_active()
            cloud_box.set_sensitive(not local)
            local_box.set_sensitive(local)
        self.update_ram_info()

    def update_ram_info(self):
        size = self.selected_source_size()
        chunk = self.chunk_kib() * 1024
        n = -(-size // chunk) if size else 0
        exact = f" = {fmt_num(chunk // 1024)} KB" if chunk < 10 * eng.MIB else ""
        self.size_note.set_text(f"MB{exact}  (da 1,2 MB a 8 GB)")
        if self.dest_is_local():
            text = "Destinazione locale: i pacchetti vengono scritti direttamente, la RAM non serve"
            ok = True
        else:
            need = (min(chunk, size) if size else chunk) + eng.HEADER_SIZE + eng.TMPFS_SLACK + eng.RAM_MARGIN
            avail = eng.mem_available()
            text = f"RAM disponibile {eng.human(avail)} — necessaria {eng.human(need)}"
            ok = avail >= need
        if n:
            text += f" — {fmt_num(n)} pacchetti .bkhd da {fmt_chunk(chunk)}"
        if not ok:
            text += "  →  RAM insufficiente: scegli pacchetti più piccoli"
        markup = f"<span foreground='{C['ok'] if ok else C['err']}'>{GLib.markup_escape_text(text)}</span>"
        self.n_chunks = n
        if n > TOO_MANY_CHUNKS:
            markup += (f"\n<span foreground='{C['warn']}'>⚠ Pacchetti molto piccoli per questo disco: ogni pacchetto è un "
                       "trasferimento separato e il backup sarà molto lento (anche solo 1 secondo per pacchetto = "
                       f"{fmt_dur(n)}). I formati floppy sono adatti a dischi e partizioni piccoli.</span>")
        elif n > MANY_CHUNKS:
            markup += (f"\n<span foreground='{C['warn']}'>Molti pacchetti: il backup sarà più lento che con pacchetti "
                       "grandi; i file verranno divisi in sottocartelle da 1000.</span>")
        self.ram_label.set_markup(markup)
        self.ram_ok = ok
        return True

    def update_pw_note(self):
        a, b = self.pw1.get_text(), self.pw2.get_text()
        if a and len(a) < eng.MIN_PASSWORD:
            txt, cls = f"Troppo corta: servono almeno {eng.MIN_PASSWORD} caratteri.", "err-text"
        elif b and a != b:
            txt, cls = "Le due password non coincidono.", "err-text"
        else:
            txt, cls = ("La password non viene salvata da nessuna parte: <b>se la dimentichi il backup non sarà "
                        "più recuperabile</b>."), "warn-text"
        ctx = self.pw_note.get_style_context()
        for c in ("err-text", "warn-text"):
            ctx.remove_class(c)
        ctx.add_class(cls)
        self.pw_note.set_markup(f"<small>{txt}</small>")

    def default_name(self):
        path = self.src_combo.get_active_id() or "disco"
        host = socket.gethostname().split(".")[0]
        base = f"{host}-{os.path.basename(path)}-{datetime.datetime.now():%Y%m%d-%H%M}"
        return re.sub(r"[^A-Za-z0-9._-]", "_", base)

    def set_busy(self, busy):
        self.stack.set_sensitive(not busy)
        self.switcher.set_sensitive(not busy)
        self.btn_cancel.set_sensitive(busy)

    def dest_base(self, page="backup"):
        """(remote o cartella locale, cartella) per la pagina indicata, oppure messaggio d'errore."""
        if self.dest_is_local(page):
            folder = (self.b_local if page == "backup" else self.r_local).get_text().strip()
            if not folder or not os.path.isabs(folder):
                return None, "Scegli il disco o la cartella locale (percorso completo, es. /media/utente/Disco)."
            if not os.path.isdir(folder):
                return None, f"La cartella {folder} non esiste o il disco non è collegato."
            return folder, None
        remote = (self.b_remote if page == "backup" else self.r_remote).get_child().get_text().strip()
        if not self.rclone:
            return None, "rclone non è installato: sudo apt install rclone (oppure usa un disco locale)."
        if not remote:
            return None, "Scegli il cloud. Se non ne hai ancora configurato uno premi «Configura cloud…»."
        return remote, None

    # ------------------------------------------------------------------ gestori: generale
    def on_guide(self, *_):
        guide = first_file(GUIDE_FILES)
        if not guide:
            return self.message(Gtk.MessageType.WARNING, "Guida non trovata",
                                "Il file Guida-BKHD-Backup.pdf non è installato.")
        subprocess.Popen(["xdg-open", guide], start_new_session=True)

    def on_about(self, *_):
        d = Gtk.AboutDialog(transient_for=self, modal=True)
        d.set_program_name(APP_NAME)
        d.set_version(eng.VERSION)
        d.set_comments("Backup settore per settore di interi dischi sul cloud o su un disco locale, a pacchetti "
                       ".bkhd da 1,2 MB (floppy) a 8 GB, con copia dei soli settori usati, protezione con "
                       "password AES-256, gestione dei settori danneggiati e pennetta avviabile BKHD Live.")
        d.set_copyright(f"{eng.COPYRIGHT}\n{eng.AUTHOR_EMAIL}")
        d.set_authors([f"{eng.AUTHOR} <{eng.AUTHOR_EMAIL}>"])
        d.set_license_type(Gtk.License.GPL_3_0)
        d.set_website(f"mailto:{eng.AUTHOR_EMAIL}")
        d.set_website_label("Contatta l'autore")
        d.add_credit_section("Basato su", ["rclone https://rclone.org", "Zstandard https://facebook.github.io/zstd/",
                                           "cryptography https://cryptography.io", "partclone https://partclone.org",
                                           "GTK https://www.gtk.org", "Python https://www.python.org",
                                           "Ubuntu https://ubuntu.com"])
        logo = self.load_icon(128)
        if logo:
            d.set_logo(logo)
        else:
            d.set_logo_icon_name(ICON_NAME)
        d.run()
        d.destroy()

    def on_configure_cloud(self, *_):
        if not self.rclone:
            return self.message(Gtk.MessageType.ERROR, "rclone non è installato",
                                "Apri un terminale ed esegui:  sudo apt install rclone")
        cmd = find_terminal(["bash", "-c", "rclone config; echo; read -rp 'Premi Invio per chiudere...'"])
        if not cmd:
            return self.message(Gtk.MessageType.INFO, "Nessun terminale trovato",
                                "Apri un terminale ed esegui:  rclone config")
        subprocess.Popen(cmd, start_new_session=True)
        self.log("Configurazione cloud aperta nel terminale. Al termine premi il pulsante di aggiornamento.")

    # ------------------------------------------------------------------ gestori: backup
    def on_source_changed(self, *_):
        path = self.src_combo.get_active_id()
        d = self.device(path) if path else None
        if d and d["mounts"]:
            mounts = ", ".join(m for m in d["mounts"] if m)
            self.src_warn.set_markup(f"<span foreground='{C['warn']}'>⚠ In uso (montato su "
                                     f"{GLib.markup_escape_text(mounts)}): la copia di un disco in uso può "
                                     "risultare incoerente. Per il disco di sistema usa la pennetta avviabile.</span>")
        else:
            self.src_warn.set_text("")
        if not self.name_edited:
            self.b_name.set_text(self.default_name())
        self.update_ram_info()

    def on_pick_source_image(self, *_):
        d = Gtk.FileChooserDialog(title="Scegli un file immagine da salvare", transient_for=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Scegli", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK:
            path = d.get_filename()
            text = f"{path}  —  file immagine  —  {eng.human(os.path.getsize(path))}"
            self.extra_sources.append((path, text))
            self.src_combo.append(path, text)
            self.src_combo.set_active_id(path)
        d.destroy()

    def on_preset_changed(self, *_):
        pid = self.preset_combo.get_active_id()
        if pid and pid != "custom":
            self.syncing_size = True
            self.chunk_spin.set_value(int(pid) / 1024)
            self.syncing_size = False
        self.update_spin_digits()
        self.update_ram_info()

    def update_spin_digits(self):
        """Decimali solo per i formati piccoli (floppy): 1,41 MB ma 2048 MB."""
        value = self.chunk_spin.get_value()
        digits = 2 if value < 10 else 0
        if self.chunk_spin.get_digits() != digits:
            self.chunk_spin.set_digits(digits)
            self.chunk_spin.set_value(value)

    def on_chunk_changed(self, *_):
        if self.syncing_size:
            return
        kib = self.chunk_spin.get_value() * 1024
        match = next((str(k) for _, k in PRESETS if abs(k - kib) <= 6), "custom")
        if self.preset_combo.get_active_id() != match:
            self.syncing_size = True
            self.preset_combo.set_active_id(match)
            self.syncing_size = False
        self.update_spin_digits()
        self.update_ram_info()

    def on_scan(self, *_):
        src = self.src_combo.get_active_id()
        if not src:
            return self.message(Gtk.MessageType.WARNING, "Scegli il disco da controllare")
        d = self.device(src)
        size = d["size"] if d else self.selected_source_size()
        if not self.ask("Controllare tutti i settori?",
                        f"Verranno letti tutti i {eng.human(size)} di {src} (solo lettura, nessuna modifica) e "
                        "verrà letto lo stato SMART. Con dischi grandi può richiedere ore; puoi annullare in "
                        "qualsiasi momento.", "Avvia il controllo"):
            return
        self.scan_result = None
        self.run_engine("scan", ["scan", "--source", src, "--bad-sectors", "skip"])

    def on_search_interrupted(self, *_):
        base, err = self.dest_base("backup")
        if err:
            return self.message(Gtk.MessageType.WARNING, "Destinazione non valida", err)
        root = eng.remote_join(base, self.b_path.get_text()) if not os.path.isabs(base) \
            else os.path.join(base, self.b_path.get_text().strip("/"))
        self.btn_find_int.set_sensitive(False)
        self.lbl_status.set_text(f"Ricerca dei backup interrotti in {root}…")

        def work():
            try:
                items, err = eng.list_backups(eng.Rclone(self.rclone) if self.rclone else None, root), None
            except (eng.EngineError, OSError) as e:
                items, err = [], str(e)
            GLib.idle_add(done, [m for m in items if m.get("status") != "complete"], err)

        def done(items, err):
            self.btn_find_int.set_sensitive(True)
            self.interrupted = items
            self.note_protected(items)
            self.int_store.clear()
            for i, m in enumerate(items):
                src = m["source"]
                opts = [f"pacchetti da {fmt_chunk(m['chunk_size'])}"]
                if m.get("compression") == "zstd":
                    opts.append("zstd")
                if m.get("sparse"):
                    opts.append("solo settori usati")
                self.int_store.append([m["name"], m["created"][:16].replace("T", " "),
                                       f"{src.get('model') or ''} {src['path']} {eng.human(src['size'])}".strip(),
                                       f"{fmt_num(m['_chunks_done'])} / {fmt_num(m['chunk_count'])}",
                                       "sì" if m.get("encryption") else "", ", ".join(opts), i])
            self.int_revealer.set_reveal_child(True)
            if err:
                self.lbl_status.set_text("Errore nella ricerca.")
                self.message(Gtk.MessageType.ERROR, "Impossibile leggere la destinazione", err)
            else:
                self.lbl_status.set_text(f"{len(items)} backup interrotti trovati." if items
                                         else "Nessun backup interrotto in questa destinazione.")
            if items:
                self.int_tree.get_selection().select_path(Gtk.TreePath(len(items) - 1))
            self.update_resume_state()
            return False

        threading.Thread(target=work, daemon=True).start()

    def selected_interrupted(self):
        model, it = self.int_tree.get_selection().get_selected()
        return self.interrupted[model[it][6]] if it else None

    def match_source(self, m):
        """Disco attuale corrispondente alla sorgente del backup interrotto (numero di serie, poi percorso)."""
        src = m["source"]
        disks = [d for d in self.devices if d["size"] == src["size"]]
        for d in disks:
            if src.get("serial") and d.get("serial") == src.get("serial"):
                return d["path"]
        for d in disks:
            if d["path"] == src.get("path") and (not src.get("model") or d["model"] == src.get("model")):
                return d["path"]
        if src.get("kind") == "file" and os.path.isfile(src["path"]) and os.path.getsize(src["path"]) == src["size"]:
            return src["path"]
        return None

    def update_resume_state(self):
        m = self.selected_interrupted()
        self.btn_resume.set_sensitive(m is not None)
        if not m:
            self.int_info.set_text("Seleziona un backup interrotto e premi «Riprendi backup interrotto»." if
                                   self.interrupted else "")
            return
        path = self.match_source(m)
        pct = 100 * m["_chunks_done"] / max(m["chunk_count"], 1)
        if path:
            self.int_info.set_text(f"Completato al {pct:.0f}%. Disco originale trovato: {path}. "
                                   f"La copia riprenderà dal pacchetto {m['_chunks_done'] + 1}.")
        else:
            self.int_info.set_text(f"Completato al {pct:.0f}%. Collega il disco originale "
                                   f"({m['source'].get('model') or m['source']['path']}, "
                                   f"{eng.human(m['source']['size'])}) e aggiorna l'elenco dei dischi.")

    def on_resume_interrupted(self):
        m = self.selected_interrupted()
        if not m:
            return
        path = self.match_source(m)
        if not path:
            return self.message(Gtk.MessageType.WARNING, "Disco originale non trovato",
                                f"Collega il disco {m['source'].get('model') or ''} {m['source']['path']} "
                                f"({eng.human(m['source']['size'])}) e premi il pulsante di aggiornamento dei "
                                "dischi.")
        if path not in [d["path"] for d in self.devices] and path not in [p for p, _ in self.extra_sources]:
            text = f"{path}  —  file immagine  —  {eng.human(os.path.getsize(path))}"
            self.extra_sources.append((path, text))
            self.src_combo.append(path, text)
        self.src_combo.set_active_id(path)
        self.b_name.set_text(m["name"])
        self.name_edited = True
        kib = m["chunk_size"] // 1024
        if not self.preset_combo.set_active_id(str(kib)):
            self.preset_combo.set_active_id("custom")
            self.chunk_spin.set_value(kib / 1024)
        self.zstd_check.set_active(m.get("compression") == "zstd")
        if m.get("compression_level"):
            self.zstd_level.set_value(m["compression_level"])
        self.used_check.set_active(bool(m.get("sparse")))
        self.bad_combo.set_active_id(m.get("bad_sector_mode") or "retry")
        self.enc_check.set_active(False)
        self.resume_check.set_active(True)
        self.resume_expect_encrypted = self.was_protected(m)
        password = None
        if m.get("encryption"):
            password = self.ask_password("Backup protetto da password",
                                         f"Inserisci la password del backup «{m['name']}» per riprenderlo.")
            if not password:
                return
        if not self.ask("Riprendere il backup interrotto?",
                        f"«{m['name']}»: {fmt_num(m['_chunks_done'])} pacchetti su {fmt_num(m['chunk_count'])} già "
                        f"salvati. La copia di {path} riprenderà dal pacchetto {m['_chunks_done'] + 1}.",
                        "Riprendi"):
            return
        self.on_start_backup(resume_password=password)

    def on_start_backup(self, resume_password=None):
        src = self.src_combo.get_active_id()
        name = self.b_name.get_text().strip()
        if not src:
            return self.message(Gtk.MessageType.WARNING, "Scegli il disco da copiare")
        base, err = self.dest_base("backup")
        if err:
            return self.message(Gtk.MessageType.WARNING, "Destinazione non valida", err)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", name):
            return self.message(Gtk.MessageType.WARNING, "Nome del backup non valido",
                                "Usa solo lettere, numeri, punto, trattino e trattino basso.")
        self.update_ram_info()
        if not self.ram_ok:
            return self.message(Gtk.MessageType.ERROR, "RAM insufficiente",
                                "Riduci la dimensione dei pacchetti o chiudi altri programmi.")
        password = resume_password
        if self.enc_check.get_active():
            a, b = self.pw1.get_text(), self.pw2.get_text()
            if len(a) < eng.MIN_PASSWORD:
                return self.message(Gtk.MessageType.WARNING, "Password troppo corta",
                                    f"La password deve avere almeno {eng.MIN_PASSWORD} caratteri.")
            if a != b:
                return self.message(Gtk.MessageType.WARNING, "Le password non coincidono")
            password = a
        if self.n_chunks > TOO_MANY_CHUNKS and not self.ask(
                f"{fmt_num(self.n_chunks)} pacchetti",
                "Con pacchetti così piccoli il backup di questo disco richiederà moltissimo tempo e creerà "
                "moltissimi file (alcuni servizi cloud limitano il numero di file). Continuare comunque?"):
            return
        d = self.device(src)
        if d and d["mounts"] and not self.ask(
                "Il disco è in uso", "Alcune partizioni sono montate: i file modificati durante la copia possono "
                "risultare incoerenti nel backup. Continuare comunque?"):
            return
        args = []
        if self.rclone:
            args += ["--rclone", self.rclone]
        args += ["backup", "--source", src, "--remote", base, "--path", self.b_path.get_text().strip(),
                 "--name", name, "--chunk-size-kib", str(self.chunk_kib()),
                 "--bad-sectors", self.bad_combo.get_active_id() or "retry"]
        if self.zstd_check.get_active():
            args += ["--compression", "zstd", "--level", str(int(self.zstd_level.get_value()))]
        if self.used_check.get_active():
            args.append("--used-only")
        if self.enc_check.get_active():
            args.append("--encrypt")
        if self.resume_check.get_active():
            args.append("--resume")
            if getattr(self, "resume_expect_encrypted", False):
                args.append("--expect-encrypted")
        self.resume_expect_encrypted = False
        self.remember(dest_mode="local" if self.dest_is_local() else "cloud", path=self.b_path.get_text().strip(),
                      local_dest=self.b_local.get_text().strip(),
                      remote=self.b_remote.get_child().get_text().strip())
        self.run_engine("backup", args, password)

    # ------------------------------------------------------------------ gestori: ripristino
    def on_search_backups(self, *_):
        base, err = self.dest_base("restore")
        if err:
            return self.message(Gtk.MessageType.WARNING, "Origine non valida", err)
        root = eng.remote_join(base, self.r_path.get_text()) if not os.path.isabs(base) \
            else os.path.join(base, self.r_path.get_text().strip("/"))
        self.btn_search.set_sensitive(False)
        self.lbl_status.set_text(f"Ricerca dei backup in {root}…")
        self.restore_base = base

        def work():
            try:
                items, err = eng.list_backups(eng.Rclone(self.rclone) if self.rclone else None, root), None
            except (eng.EngineError, OSError) as e:
                items, err = [], str(e)
            GLib.idle_add(done, items, err)

        def done(items, err):
            self.btn_search.set_sensitive(True)
            self.backups = items
            self.note_protected(items)
            self.store.clear()
            for i, m in enumerate(items):
                src = m["source"]
                status = {"complete": "Completo", "in_progress": "Interrotto"}.get(m.get("status"), m.get("status"))
                self.store.append([m["name"], m["created"][:16].replace("T", " "),
                                   f"{src.get('model') or ''} {src['path']}".strip(), eng.human(src["size"]),
                                   f"{fmt_num(m['_chunks_done'])}/{fmt_num(m['chunk_count'])}",
                                   eng.human(m["_stored_bytes"]), "🔒" if m.get("encryption") else "", status, i])
            self.lbl_status.set_text(f"{len(items)} backup trovati." if not err else "Errore.")
            if err:
                self.message(Gtk.MessageType.ERROR, "Impossibile leggere la destinazione", err)
            if items:
                self.tree.get_selection().select_path(Gtk.TreePath(len(items) - 1))
            self.remember(dest_mode="local" if self.dest_is_local("restore") else "cloud",
                          path=self.r_path.get_text().strip())
            return False

        threading.Thread(target=work, daemon=True).start()

    def selected_backup(self):
        model, it = self.tree.get_selection().get_selected()
        return self.backups[model[it][8]] if it else None

    def on_backup_selected(self, *_):
        m = self.selected_backup()
        if m:
            src = m["source"]
            extra = []
            if m.get("sparse"):
                extra.append(f"solo settori usati ({eng.human(m.get('used_bytes', 0))})")
            if m.get("encryption"):
                extra.append("protetto da password (AES-256-GCM)")
            if m.get("bad_sectors_total"):
                extra.append(f"<span foreground='{C['err']}'>{m['bad_sectors_total']} settori illeggibili</span>")
            self.backup_info.set_markup(
                f"<small><b>{GLib.markup_escape_text(m['_dir'])}</b>\n"
                f"PC {GLib.markup_escape_text(m.get('host', ''))} — disco {GLib.markup_escape_text(src['path'])} "
                f"{eng.human(src['size'])}, settore {src.get('logical_sector')} B, tabella {src.get('pttype') or '-'}"
                f" — pacchetti da {fmt_chunk(m['chunk_size'])}, compressione {m['compression']}"
                + ("".join(" — " + e for e in extra)) + "</small>")
        enc = bool(m and m.get("encryption"))
        self.r_pw_label.set_visible(enc)
        self.r_pw.set_visible(enc)
        self.update_restore_state()

    def on_pick_target_file(self, *_):
        d = Gtk.FileChooserDialog(title="File immagine di destinazione", transient_for=self,
                                  action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Scegli", Gtk.ResponseType.OK)
        m = self.selected_backup()
        d.set_current_name((m["name"] if m else "ripristino") + ".img")
        if d.run() == Gtk.ResponseType.OK:
            self.dst_file.set_text(d.get_filename())
            self.rb_file.set_active(True)
        d.destroy()

    def update_restore_state(self):
        if not hasattr(self, "tree"):
            return
        m = self.selected_backup()
        warn = ""
        ok = m is not None
        if self.rb_disk.get_active():
            d = self.device(self.dst_combo.get_active_id() or "")
            if not d:
                ok = False
            elif d["mounts"]:
                warn, ok = "Il disco è in uso (partizioni montate): smontale o usa la pennetta avviabile.", False
            elif m and d["size"] < m["source"]["size"]:
                warn = f"Disco troppo piccolo: {eng.human(d['size'])} < {eng.human(m['source']['size'])} del backup."
                ok = False
        elif self.rb_file.get_active():
            ok = ok and bool(self.dst_file.get_text().strip())
        if m and m.get("encryption") and not self.r_pw.get_text():
            ok = False
            warn = (warn + " " if warn else "") + "Inserisci la password del backup."
        if m and m.get("status") != "complete":
            warn = (warn + " " if warn else "") + "Backup interrotto: saranno ripristinati solo i pacchetti presenti."
        if m and not m.get("encryption") and self.was_protected(m):
            ok = False
            warn = ("ATTENZIONE: questo backup era protetto da password ma ora risulta non protetto: "
                    "possibile manomissione. Il ripristino è bloccato.")
        self.dst_warn.set_markup(f"<span foreground='{C['err']}'>{GLib.markup_escape_text(warn)}</span>" if warn else "")
        self.btn_restore.set_sensitive(ok)
        self.btn_restore.set_label("Avvia verifica" if self.rb_verify.get_active() else "Avvia ripristino")

    def on_start_restore(self, *_):
        m = self.selected_backup()
        if not m:
            return
        base = getattr(self, "restore_base", None) or self.dest_base("restore")[0]
        if os.path.isabs(base):
            rel = os.path.relpath(m["_dir"], base)
        else:
            rel = m["_dir"][len(base):] if m["_dir"].startswith(base) else m["_dir"]
        args = (["--rclone", self.rclone] if self.rclone else []) + ["restore", "--remote", base, "--path", rel]
        if m.get("status") != "complete":
            args.append("--allow-incomplete")
        if self.was_protected(m):
            args.append("--expect-encrypted")
        password = self.r_pw.get_text() if m.get("encryption") else None
        if self.rb_verify.get_active():
            args.append("--verify-only")
            return self.run_engine("verify", args, password)
        if self.rb_disk.get_active():
            target = self.dst_combo.get_active_id()
            if not self.confirm_destroy(self.device(target), "Cancella e ripristina"):
                return
        else:
            target = self.dst_file.get_text().strip()
            if os.path.exists(target) and not self.ask("Il file esiste già", f"{target} verrà sovrascritto.",
                                                       "Sovrascrivi"):
                return
        args += ["--target", target, "--yes"]
        self.run_engine("restore", args, password)

    def confirm_destroy(self, d, ok_label):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE,
                                text=f"Tutti i dati su {d['path']} verranno cancellati")
        dlg.format_secondary_markup(
            f"<b>{GLib.markup_escape_text(d['text'])}</b>\n\nL'operazione non è reversibile. "
            f"Per confermare scrivi il nome del dispositivo: <b>{d['name']}</b>")
        entry = Gtk.Entry(activates_default=True)
        dlg.get_message_area().pack_start(entry, False, False, 0)
        dlg.add_buttons("Annulla", Gtk.ResponseType.CANCEL, ok_label, Gtk.ResponseType.OK)
        btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        btn.get_style_context().add_class("destructive-action")
        btn.set_sensitive(False)
        entry.connect("changed", lambda e: btn.set_sensitive(e.get_text().strip() == d["name"]))
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        res = dlg.run()
        ok = res == Gtk.ResponseType.OK and entry.get_text().strip() == d["name"]
        dlg.destroy()
        return ok

    # ------------------------------------------------------------------ gestori: pennetta e DVD
    def on_pick_iso(self, *_):
        d = Gtk.FileChooserDialog(title="Immagine ISO di BKHD Live", transient_for=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Scegli", Gtk.ResponseType.OK)
        cur = self.live_iso.get_text()
        if os.path.isdir(os.path.dirname(cur)):
            d.set_current_folder(os.path.dirname(cur))
        f = Gtk.FileFilter()
        f.set_name("Immagini ISO")
        f.add_pattern("*.iso")
        d.add_filter(f)
        if d.run() == Gtk.ResponseType.OK:
            self.live_iso.set_text(d.get_filename())
            self.remember(live_iso=d.get_filename())
        d.destroy()

    def usb_partitions(self, disk):
        return [p for p in self.devices if p.get("parent") == disk]

    def usb_data_partition(self, disk):
        """Partizione della chiavetta utilizzabile per i backup (esclusi sistema live ed EFI)."""
        cands = [p for p in self.usb_partitions(disk)
                 if p["fstype"] in ("exfat", "ext4", "vfat", "ntfs", "btrfs", "xfs") and p["size"] >= 100 * eng.MIB]
        cands.sort(key=lambda p: (p["label"] != eng.DATA_LABEL, -p["size"]))
        return cands[0] if cands else None

    def update_live_state(self):
        if not hasattr(self, "disc_combo"):
            return
        iso = self.live_iso.get_text().strip()
        has_iso = bool(iso) and os.path.isfile(iso)
        iso_size = os.path.getsize(iso) if has_iso else 0
        if has_iso:
            st = os.stat(iso)
            when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y")
            src = "inclusa nel programma" if iso.startswith(eng.SHIPPED_ISO_DIR) else f"creata il {when}"
            self.live_status.set_markup(f"<span foreground='{C['ok']}'>✔ Immagine pronta: "
                                        f"{eng.human(iso_size)}, {src}.</span>")
        else:
            self.live_status.set_markup(f"<span foreground='{C['warn']}'>Immagine non trovata: scegline una o "
                                        "creala (opzioni avanzate).</span>")
        self.btn_save_iso.set_sensitive(has_iso)
        usb = self.device(self.usb_combo.get_active_id() or "")
        warn, info = "", ""
        if not usb:
            warn = "Nessuna chiavetta USB collegata: inseriscila e premi il pulsante di aggiornamento."
        else:
            parts = self.usb_partitions(usb["path"])
            if parts:
                info = "Contenuto attuale: " + "; ".join(
                    f"{os.path.basename(p['path'])} {p['fstype'] or 'senza file system'}"
                    + (f" «{p['label']}»" if p["label"] else "") + f" {eng.human(p['size'])}"
                    + (" (montata)" if p["mounts"] else "") for p in parts)
            else:
                info = "La chiavetta non contiene partizioni."
            need = iso_size + (512 * eng.MIB if self.data_check.get_active() else 0)
            if has_iso and usb["size"] < need:
                warn = f"La chiavetta è troppo piccola: servono almeno {eng.human(need)}."
        self.usb_info.set_markup(f"<small>{GLib.markup_escape_text(info)}</small>")
        self.usb_warn.set_markup(f"<span foreground='{C['warn']}'>{GLib.markup_escape_text(warn)}</span>"
                                 if warn else "")
        self.data_fs.set_sensitive(self.data_check.get_active())
        self.btn_live_write.set_sensitive(has_iso and bool(usb) and not warn)
        self.btn_format.set_sensitive(bool(usb))
        self.btn_eject.set_sensitive(bool(usb))
        self.btn_use_dest.set_sensitive(bool(usb) and self.usb_data_partition(usb["path"]) is not None)
        disc = self.disc_combo.get_active_id()
        self.disc_info.set_markup("" if disc else f"<span foreground='{C['dim']}'>Nessuna unità CD/DVD trovata."
                                                  "</span>")
        self.btn_burn.set_sensitive(has_iso and bool(disc))

    def on_save_iso(self, *_):
        src = self.live_iso.get_text().strip()
        if not os.path.isfile(src):
            return
        d = Gtk.FileChooserDialog(title="Salva l'immagine ISO di BKHD Live", transient_for=self,
                                  action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
        d.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Salva", Gtk.ResponseType.OK)
        d.set_current_folder(os.path.expanduser("~"))
        d.set_current_name(os.path.basename(src))
        dest = d.get_filename() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        if not dest:
            return
        if os.path.realpath(dest) == os.path.realpath(src):
            return self.message(Gtk.MessageType.INFO, "L'immagine è già in quella posizione")
        self.copying = threading.Event()
        cancel = self.copying
        self.set_busy(True)
        self.lbl_status.set_text(f"Copia dell'immagine in {dest}…")
        size = os.path.getsize(src)

        def work():
            import hashlib
            err = None
            h = hashlib.sha256()
            try:
                with open(src, "rb") as fi, open(dest + ".parziale", "wb") as fo:
                    done = 0
                    for data in iter(lambda: fi.read(4 * eng.MIB), b""):
                        if cancel.is_set():
                            raise InterruptedError("copia annullata")
                        fo.write(data)
                        h.update(data)
                        done += len(data)
                        GLib.idle_add(self.bar_total.set_fraction, done / max(size, 1))
                    fo.flush()
                    os.fsync(fo.fileno())
                os.replace(dest + ".parziale", dest)
                with open(dest + ".sha256", "w") as f:
                    f.write(f"{h.hexdigest()}  {os.path.basename(dest)}\n")
            except (OSError, InterruptedError) as e:
                err = str(e)
                try:
                    os.remove(dest + ".parziale")
                except OSError:
                    pass
            GLib.idle_add(done_cb, err)

        def done_cb(err):
            self.copying = None
            self.set_busy(False)
            if err:
                self.lbl_status.set_text("Copia non riuscita.")
                self.message(Gtk.MessageType.ERROR, "Copia non riuscita", err)
            else:
                self.lbl_status.set_text(f"Immagine salvata in {dest}.")
                self.log(f"Immagine BKHD Live salvata in {dest} (con il file di controllo .sha256).", "ok")
                self.message(Gtk.MessageType.INFO, "Immagine salvata",
                             f"{dest}\n\nPuoi usarla con una macchina virtuale, con Ventoy, con un altro programma "
                             "di scrittura o masterizzarla più tardi su DVD.")
            return False

        threading.Thread(target=work, daemon=True).start()

    def on_live_build(self, *_):
        iso = self.build_iso
        work = self.live_work.get_text().strip() or "/var/cache/bkhd-live"
        if not os.path.isabs(work):
            return self.message(Gtk.MessageType.WARNING, "Cartella di lavoro non valida")
        probe = work
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        st = os.statvfs(probe)
        free = st.f_bavail * st.f_frsize
        if free < 10 * 1024 ** 3 and not os.path.isdir(os.path.join(work, "chroot")):
            return self.message(Gtk.MessageType.WARNING, "Spazio insufficiente",
                                f"Nella cartella di lavoro ci sono {eng.human(free)} liberi: ne servono almeno "
                                "10 GB. Scegli una cartella su un altro disco.")
        if not self.ask("Creare una nuova immagine BKHD Live?",
                        "Verranno scaricati circa 2 GB da Internet (server ufficiali Ubuntu e Mozilla) e la "
                        "costruzione richiederà da 20 a 60 minuti. L'immagine inclusa nel programma funziona già: "
                        "serve solo per avere i pacchetti più aggiornati.", "Crea"):
            return
        os.makedirs(os.path.dirname(iso), exist_ok=True)
        self.remember(live_work=work)
        self.run_engine("live-build", ["live-build", "--output", iso, "--workdir", work])

    def on_live_write(self, *_):
        iso = self.live_iso.get_text().strip()
        d = self.device(self.usb_combo.get_active_id() or "")
        if not d or not os.path.isfile(iso):
            return
        if not self.confirm_destroy(d, "Cancella e crea la pennetta"):
            return
        args = ["live-write", "--image", iso, "--target", d["path"], "--yes"]
        if self.data_check.get_active():
            args += ["--data-fs", self.data_fs.get_active_id() or "exfat", "--data-label", eng.DATA_LABEL]
        self.run_engine("live-write", args)

    def on_usb_format(self, *_):
        d = self.device(self.usb_combo.get_active_id() or "")
        if not d:
            return
        fs = self.fmt_fs.get_active_id() or "exfat"
        label = self.fmt_label.get_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,11}", label):
            return self.message(Gtk.MessageType.WARNING, "Etichetta non valida",
                                "Usa da 1 a 11 caratteri tra lettere, numeri, - e _.")
        if not shutil.which(eng.FS_TOOLS[fs]):
            pkg = {"exfat": "exfatprogs", "ntfs": "ntfs-3g", "vfat": "dosfstools", "ext4": "e2fsprogs"}[fs]
            return self.message(Gtk.MessageType.WARNING, "Programma mancante",
                                f"Per formattare in questo formato installa: sudo apt install {pkg}")
        if fs == "vfat" and self.chunk_kib() * 1024 >= 4 * 1024 ** 3:
            self.log("Nota: con FAT32 i file non possono superare 4 GB: usa pacchetti da 2 GB o meno.", "warning")
        if not self.confirm_destroy(d, "Cancella e formatta"):
            return
        self.run_engine("usb-format", ["usb-format", "--target", d["path"], "--fs", fs, "--label", label, "--yes"])

    def on_use_as_dest(self, *_):
        d = self.device(self.usb_combo.get_active_id() or "")
        part = self.usb_data_partition(d["path"]) if d else None
        if not part:
            return
        mount = next((m for m in part["mounts"] if m), None)
        if not mount:
            r = subprocess.run(["udisksctl", "mount", "-b", part["path"], "--no-user-interaction"],
                               capture_output=True, text=True, timeout=60)
            self.refresh_devices()
            part = self.device(part["path"]) or part
            mount = next((m for m in part["mounts"] if m), None)
            if not mount:
                return self.message(Gtk.MessageType.ERROR, "Impossibile montare la chiavetta",
                                    (r.stderr or r.stdout).strip())
        self.b_rb_local.set_active(True)
        self.b_local.set_text(mount)
        self.r_local.set_text(mount)
        self.remember(dest_mode="local", local_dest=mount)
        self.stack.set_visible_child_name("backup")
        self.log(f"Destinazione dei backup: chiavetta montata in {mount}.", "ok")

    def on_eject(self, *_):
        d = self.device(self.usb_combo.get_active_id() or "")
        if not d:
            return
        errors = []
        for p in self.usb_partitions(d["path"]):
            if p["mounts"]:
                r = subprocess.run(["udisksctl", "unmount", "-b", p["path"], "--no-user-interaction"],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    errors.append((r.stderr or r.stdout).strip())
        if not errors:
            r = subprocess.run(["udisksctl", "power-off", "-b", d["path"], "--no-user-interaction"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                errors.append((r.stderr or r.stdout).strip())
        self.refresh_devices()
        if errors:
            self.message(Gtk.MessageType.WARNING, "Impossibile espellere la chiavetta", "\n".join(errors))
        else:
            self.message(Gtk.MessageType.INFO, "Chiavetta espulsa", "Ora puoi toglierla in sicurezza.")

    def on_disc_burn(self, *_):
        iso = self.live_iso.get_text().strip()
        dev = self.disc_combo.get_active_id()
        if not dev or not os.path.isfile(iso):
            return
        if not self.ask("Masterizzare BKHD Live su DVD?",
                        f"Inserisci un DVD vergine o riscrivibile in {dev}: un disco riscrivibile verrà cancellato. "
                        "Al termine il disco viene riletto e verificato.", "Masterizza"):
            return
        self.run_engine("disc-burn", ["disc-burn", "--image", iso, "--device", dev, "--yes"])

    # ------------------------------------------------------------------ motore
    def run_engine(self, mode, args, password=None):
        base = [ENGINE, "--json"] + (["--password-stdin"] if password is not None else []) + args
        # BKHD_NO_PKEXEC=1: solo per sviluppo/test con file immagine leggibili dall'utente
        direct = os.geteuid() == 0 or os.environ.get("BKHD_NO_PKEXEC") == "1"
        cmd = [sys.executable] + base if direct else ["pkexec"] + base
        self.op_mode = mode
        shown = [a for a in args if a != self.rclone and a != "--rclone"]
        self.log(f"Avvio {mode}: " + " ".join(shown))
        self.lbl_status.set_text("Richiesta dei permessi di amministratore…")
        self.bar_chunk.set_fraction(0)
        self.bar_total.set_fraction(0)
        self.lbl_eta.set_text("")
        self.lbl_detail.set_text("")
        self.overall_start = None
        self.t_start = time.monotonic()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1)
        except OSError as e:
            return self.message(Gtk.MessageType.ERROR, "Impossibile avviare il motore", str(e))
        if password is not None:            # mai sulla riga di comando: solo sullo standard input
            try:
                self.proc.stdin.write(password + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass
        self.set_busy(True)
        proc = self.proc

        def read_out():
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    GLib.idle_add(self.log, line.rstrip())
                    continue
                GLib.idle_add(self.on_event, ev)

        def read_err():
            for line in proc.stderr:
                if line.strip():
                    GLib.idle_add(self.log, line.rstrip(), "warning")

        def waiter():
            t1 = threading.Thread(target=read_out, daemon=True)
            t2 = threading.Thread(target=read_err, daemon=True)
            t1.start()
            t2.start()
            rc = proc.wait()
            t1.join(2)
            t2.join(2)
            GLib.idle_add(self.on_finished, rc)

        threading.Thread(target=waiter, daemon=True).start()

    def on_event(self, ev):
        t = ev.get("type")
        if t == "log":
            self.log(ev["msg"], ev.get("level", "info"))
        elif t == "start":
            self.t_start = time.monotonic()
            if ev.get("encrypted") and ev.get("backup_id"):
                self.note_protected([{"backup_id": ev["backup_id"], "encryption": True}])
            if ev.get("mode") in ("backup", "restore", "verify"):
                self.log(f"{eng.human(ev['total'])} in {ev['chunks']} pacchetti da {fmt_chunk(ev['chunk_size'])}")
        elif t == "phase":
            unit = "Fase" if ev.get("phase") in ("live", "scan") or ev.get("unit") == "Fase" else "Pacchetto"
            self.lbl_status.set_text(f"{unit} {ev['chunk']} di {ev['chunks']} — {ev['label']}")
            self.bar_chunk.set_fraction(0)
        elif t == "progress":
            frac = ev.get("frac", 0)
            phase = ev.get("phase")
            if phase == "live":
                self.bar_chunk.pulse()
                self.bar_chunk.set_text(PHASES["live"])
            else:
                self.bar_chunk.set_fraction(min(frac, 1))
                self.bar_chunk.set_text(f"{PHASES.get(phase, phase)}: {frac * 100:.1f}%"
                                        f"  —  {eng.human(ev.get('speed', 0))}/s")
            overall = ev.get("overall", 0)
            self.bar_total.set_fraction(min(overall, 1))
            self.bar_total.set_text(f"Totale: {overall * 100:.1f}%")
            now = time.monotonic()
            if self.overall_start is None:
                self.overall_start = (overall, now)
            o0, t0 = self.overall_start
            text = f"Tempo trascorso {fmt_dur(now - self.t_start)}"
            if overall - o0 > 0.001 and now - t0 > 5 and phase != "live":
                text += f"  —  tempo stimato rimanente {fmt_dur((now - t0) * (1 - overall) / (overall - o0))}"
            self.lbl_eta.set_text(text)
        elif t == "detail":
            self.lbl_detail.set_text(ev.get("text", ""))
            self.bar_chunk.pulse()
        elif t == "scan_result":
            self.scan_result = ev
        elif t == "chunk_done":
            if ev.get("msg"):
                self.log(ev["msg"], "ok")
        elif t == "done":
            self.log(ev["msg"], "ok" if ev.get("ok") else "warning")
            self.final_event = ev
        elif t == "error":
            self.log(ev["msg"], "error")
            self.final_event = ev
        return False

    def on_finished(self, rc):
        ev = self.final_event
        self.final_event = None
        self.proc = None
        self.set_busy(False)
        self.lbl_detail.set_text("")
        if rc in (126, 127) and not ev:
            self.lbl_status.set_text("Autorizzazione negata.")
            self.message(Gtk.MessageType.WARNING, "Autorizzazione non concessa",
                         "Per leggere o scrivere un disco servono i permessi di amministratore.")
            return False
        if ev and ev.get("type") == "done" and ev.get("ok"):
            self.bar_total.set_fraction(1)
            self.bar_chunk.set_fraction(1)
            self.lbl_status.set_text(ev["msg"])
            if shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "-i", ICON_NAME, APP_NAME, ev["msg"]])
            if self.op_mode == "scan" and self.scan_result:
                self.show_scan_result(ev["msg"])
            else:
                self.message(Gtk.MessageType.INFO, "Operazione completata", ev["msg"])
        elif ev and ev.get("cancelled"):
            self.lbl_status.set_text("Operazione annullata.")
        else:
            msg = ev["msg"] if ev else f"Il motore è terminato con codice {rc}. Vedi il registro."
            self.lbl_status.set_text("Errore.")
            self.message(Gtk.MessageType.ERROR, "Operazione non riuscita", msg)
        if self.op_mode == "backup":
            self.name_edited = False
            self.resume_check.set_active(False)
        if self.op_mode == "live-build" and ev and ev.get("ok") and os.path.isfile(self.build_iso):
            self.live_iso.set_text(self.build_iso)
            self.remember(live_iso=self.build_iso)
        if self.op_mode in ("live-build", "live-write", "usb-format", "disc-burn"):
            self.refresh_devices()
        return False

    def show_scan_result(self, msg):
        r = self.scan_result
        bad = r.get("bad_sectors", 0)
        kind = Gtk.MessageType.WARNING if bad or r.get("smart_ok") is False else Gtk.MessageType.INFO
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind, buttons=Gtk.ButtonsType.OK,
                              text="Risultato del controllo del disco")
        lines = [msg, ""] + r.get("smart", [])
        if bad:
            lines += ["", "Per il backup scegli in «Settori danneggiati»:",
                      "• «Riprova e copia» per recuperare il più possibile;",
                      "• «Salta subito» se il disco peggiora a ogni lettura."]
            self.bad_combo.set_active_id("retry")
        d.format_secondary_text("\n".join(lines))
        d.run()
        d.destroy()

    def on_cancel(self, *_):
        if self.copying:
            self.copying.set()
            return
        if not self.proc:
            return
        extra = {"backup": " Potrai riprenderlo con «Cerca backup interrotti».",
                 "restore": " Il disco di destinazione resterà parzialmente scritto e non utilizzabile.",
                 "live-write": " La chiavetta resterà parzialmente scritta.",
                 "usb-format": " La chiavetta potrebbe restare senza file system.",
                 "disc-burn": " Un DVD non riscrivibile resterà inutilizzabile.",
                 "live-build": " La parte già scaricata verrà riutilizzata la prossima volta."}.get(self.op_mode, "")
        if self.ask("Annullare l'operazione?", "L'operazione in corso verrà interrotta." + extra, "Interrompi"):
            self.send_cancel()

    def send_cancel(self):
        try:
            self.proc.stdin.write("cancel\n")
            self.proc.stdin.flush()
        except (OSError, AttributeError, ValueError):
            pass
        self.lbl_status.set_text("Annullamento in corso…")

    def on_close(self, *_):
        if self.proc:
            if not self.ask("Operazione in corso", "Chiudendo il programma l'operazione verrà interrotta.",
                            "Interrompi ed esci"):
                return True
            self.send_cancel()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                pass
        Gtk.main_quit()
        return False


def main():
    GLib.set_prgname(ICON_NAME)          # WM_CLASS = bkhd-backup: la dock associa la finestra al lanciatore
    GLib.set_application_name(APP_NAME)
    win = BkhdWindow()
    win.show_all()
    win.r_pw_label.hide()
    win.r_pw.hide()
    Gtk.main()


if __name__ == "__main__":
    main()
