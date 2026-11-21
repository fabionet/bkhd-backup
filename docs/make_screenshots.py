#!/usr/bin/python3
"""Genera le schermate per la guida pilotando la GUI con dischi d'esempio e un cloud simulato.

Uso: docs/make_screenshots.py CARTELLA_LAVORO
(richiede una sessione grafica X11 e ImageMagick 'import')
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.join(HERE, "..")
WORK = os.path.abspath(sys.argv[1])
OUT = os.path.join(HERE, "img")
os.makedirs(OUT, exist_ok=True)
os.makedirs(WORK, exist_ok=True)

# cloud simulato con un remote chiamato "gdrive", inizialmente non configurato
fakebin = os.path.join(WORK, "bin")
os.makedirs(fakebin, exist_ok=True)
if not os.path.exists(os.path.join(fakebin, "rclone")):
    os.symlink(os.path.join(ROOT, "tests", "fake_rclone.py"), os.path.join(fakebin, "rclone"))
cloud = os.path.join(WORK, "cloud")
shutil.rmtree(cloud, ignore_errors=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(WORK, "config")
shutil.rmtree(os.path.join(WORK, "config"), ignore_errors=True)
os.makedirs(os.path.join(WORK, "config", "bkhd-backup"))
with open(os.path.join(WORK, "config", "bkhd-backup", "impostazioni.json"), "w") as f:
    json.dump({"tema": "giorno"}, f)            # schermate della guida con il tema di giorno   # non tocca le impostazioni dell'utente
os.environ.update(PATH=fakebin + ":" + os.environ["PATH"], FAKE_RCLONE_ROOT=cloud, FAKE_RCLONE_REMOTES="",
                  FAKE_RCLONE_BPS="45000000", BKHD_NO_PKEXEC="1")

# "chiavetta" d'esempio: 14,41 GB (sparse) con un po' di dati
img = os.path.join(WORK, "kingston.img")
SIZE = 15472047104
if not os.path.exists(img) or os.path.getsize(img) != SIZE:
    with open(img, "wb") as f:
        f.truncate(SIZE)
        f.write(os.urandom(380 << 20))
        f.seek(5 << 30)
        f.write(os.urandom(160 << 20))

sys.path.insert(0, os.path.join(ROOT, "src"))
import bkhd_gui as G  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

FAKE_DEVICES = [
    {"path": "/dev/sda", "name": "sda", "type": "disk", "size": 1000204886016, "model": "Samsung SSD 870 EVO 1TB",
     "serial": "S6PNNS0T123456", "removable": False,
     "mounts": ["/", "/boot/efi"], "text": "/dev/sda  —  Samsung SSD 870 EVO 1TB  —  931.51 GB"},
    {"path": img, "name": "sdb", "type": "disk", "size": SIZE, "model": "Kingston DataTraveler 3.0",
     "serial": "60A44C3FAE21", "removable": True,
     "mounts": [], "text": "/dev/sdb  —  Kingston DataTraveler 3.0  —  14.41 GB"},
    {"path": "/dev/sdc", "name": "sdc", "type": "disk", "size": 2000398934016, "model": "WD Elements 25A2",
     "serial": "WX12A3456789", "removable": True,
     "mounts": [], "text": "/dev/sdc  —  WD Elements 25A2  —  1.82 TB"},
    {"path": "/dev/sdd", "name": "sdd", "type": "disk", "size": 32015679488, "model": "SanDisk Ultra",
     "serial": "4C530001230811", "removable": True,
     "mounts": [], "text": "/dev/sdd  —  SanDisk Ultra  —  29.82 GB"},
    {"path": "/dev/sdd1", "name": "sdd1", "type": "part", "size": 32014630912, "model": "SanDisk Ultra",
     "serial": "", "removable": True, "parent": "/dev/sdd", "fstype": "vfat", "label": "SANDISK",
     "mounts": [], "text": "/dev/sdd1  —  SanDisk Ultra  —  29.82 GB  (SANDISK)"},
    {"path": "/dev/sr0", "name": "sr0", "type": "rom", "size": 0, "model": "HL-DT-ST DVDRAM GP57EB40",
     "serial": "", "removable": True, "mounts": [], "text": "/dev/sr0  —  HL-DT-ST DVDRAM GP57EB40"},
]
for _d in FAKE_DEVICES:
    _d.setdefault("parent", None)
    _d.setdefault("fstype", "")
    _d.setdefault("label", "")
ISO = os.path.join(WORK, "bkhd-live-esempio.iso")
if not os.path.exists(ISO):
    with open(ISO, "wb") as f:
        f.truncate(1310529536)
G.list_block_devices = lambda: [dict(d) for d in FAKE_DEVICES]
_which = G.shutil.which
G.shutil.which = lambda n, *a, **k: f"/usr/sbin/{n}" if n.startswith("mkfs.") else _which(n, *a, **k)  # come dopo l'installazione

w = G.BkhdWindow()
w.show_all()
dialog_queue = []          # nomi delle prossime finestre di dialogo da fotografare
handled = set()
confirm_mode = [False]     # la conferma del ripristino viene gestita a parte


def pump():
    while Gtk.events_pending():
        Gtk.main_iteration()


RECTS = {}


def shot(name, win=None, marks=()):
    """Cattura la finestra, toglie l'ombra nera e registra la posizione dei widget indicati."""
    pump()
    win = win or w
    raw = os.path.join(WORK, name + "-raw.png")
    out = os.path.join(OUT, name + ".png")
    subprocess.run(["import", "-window", str(win.get_window().get_xid()), raw], check=True)
    res = subprocess.run(["convert", raw, "-fuzz", "6%", "-fill", "white", "-draw", "color 0,0 floodfill",
                          "-trim", "-format", "%X %Y %w %h", "-write", "info:-", "+repage", out],
                         capture_output=True, text=True, check=True)
    ox, oy, iw, ih = [int(v) for v in res.stdout.replace("+", " ").split()]
    rects = []
    for label, widget in marks:
        x, y = widget.translate_coordinates(win, 0, 0)
        rects.append([label, x - ox, y - oy, widget.get_allocated_width(), widget.get_allocated_height()])
    RECTS[name] = {"size": [iw, ih], "marks": rects}
    with open(os.path.join(OUT, "rects.json"), "w") as f:
        json.dump(RECTS, f, indent=1)
    print("schermata", name, iw, ih)


def button(label):
    def walk(wd):
        if isinstance(wd, Gtk.Button) and wd.get_label() == label and wd.is_drawable():
            return wd
        if isinstance(wd, Gtk.Container):
            for c in wd.get_children():
                r = walk(c)
                if r:
                    return r
        return None
    return walk(w)


def watch_dialogs():
    if confirm_mode[0]:
        return True
    for top in Gtk.Window.list_toplevels():
        if isinstance(top, Gtk.Dialog) and top.get_visible() and id(top) not in handled:
            handled.add(id(top))
            name = dialog_queue.pop(0) if dialog_queue else None

            def capture(top=top, name=name):
                if name:
                    shot(name, top)
                top.response(Gtk.ResponseType.OK if isinstance(top, Gtk.MessageDialog) else Gtk.ResponseType.CLOSE)
                return False
            GLib.timeout_add(900, capture)
    return True


def find(widget, cls):
    if isinstance(widget, cls):
        return widget
    if isinstance(widget, Gtk.Container):
        for c in widget.get_children():
            r = find(c, cls)
            if r:
                return r
    return None


def steps():
    yield 1500
    shot("01-primo-avvio", marks=[("A", w.infobar), ("B", button("Configura cloud…"))])
    os.environ["FAKE_RCLONE_REMOTES"] = "gdrive"
    w.refresh_remotes()
    yield 1200
    w.src_combo.set_active_id(img)
    w.b_name.set_text("pc-casa-sdb-20261011-2015")
    w.preset_combo.set_active_id(str(2048 * 1024))
    w.zstd_check.set_active(True)
    yield 500
    shot("02-backup-impostazioni", marks=[
        ("1", w.src_combo), ("2", button("Controlla disco…")), ("3", w.b_rb_cloud), ("4", w.b_remote),
        ("5", w.b_local), ("6", w.b_path), ("7", w.b_name), ("8", w.btn_find_int)])
    page = w.stack.get_child_by_name("backup")
    w.used_check.set_active(True)
    w.enc_check.set_active(True)
    w.pw1.set_text("nuvola-azzurra-2026")
    w.pw2.set_text("nuvola-azzurra-2026")
    yield 600
    adj = page.get_vadjustment()
    adj.set_value(adj.get_upper() - adj.get_page_size())
    yield 600
    shot("12-opzioni", marks=[
        ("1", w.preset_combo), ("2", w.ram_label), ("3", w.zstd_check), ("4", w.used_check), ("5", w.enc_check),
        ("6", w.pw1), ("7", w.bad_combo), ("8", w.btn_backup)])
    w.used_check.set_active(False)
    w.enc_check.set_active(False)
    adj.set_value(0)
    yield 300
    w.on_start_backup()
    while "upload" not in w.lbl_status.get_text():
        yield 200
    yield 4000
    shot("03-backup-upload", marks=[("1", w.lbl_status), ("2", w.bar_chunk), ("3", w.bar_total),
                                    ("4", w.lbl_eta), ("5", w.btn_cancel)])
    dialog_queue.append("05-backup-completato")
    while w.proc:
        yield 300
    yield 2500
    exp = find(w, Gtk.Expander)
    exp.set_expanded(True)
    yield 600
    shot("04-backup-registro")
    exp.set_expanded(False)

    # manifest con i dati del disco d'esempio + un secondo backup d'esempio per l'elenco
    bdir = os.path.join(cloud, "BKHD", "pc-casa-sdb-20261011-2015")
    m = json.load(open(os.path.join(bdir, "manifest.json")))
    m["host"] = "pc-casa"
    m["source"].update(path="/dev/sdb", model="Kingston DataTraveler 3.0", pttype="dos")
    json.dump(m, open(os.path.join(bdir, "manifest.json"), "w"))
    old = dict(m, name="pc-casa-sda-20261004-2210", created="2026-10-04T22:10:31+02:00", status="complete",
               chunk_size=4 << 30, chunk_count=233, bad_sectors_total=0,
               source=dict(m["source"], path="/dev/sda", model="Samsung SSD 870 EVO 1TB", size=1000204886016,
                           pttype="gpt"),
               chunks=[{"index": i + 1, "payload_size": 1180 << 20} for i in range(233)])
    os.makedirs(os.path.join(cloud, "BKHD", old["name"]))
    json.dump(old, open(os.path.join(cloud, "BKHD", old["name"], "manifest.json"), "w"))

    w.stack.set_visible_child_name("restore")
    w.on_search_backups()
    yield 2500
    for row in w.store:
        if row[0] == "pc-casa-sdb-20261011-2015":
            w.tree.get_selection().select_iter(row.iter)
    w.rb_disk.set_active(True)
    w.dst_combo.set_active_id("/dev/sdc")
    yield 600
    shot("06-ripristino", marks=[("1", w.r_remote), ("2", w.btn_search), ("3", w.tree), ("4", w.backup_info),
                                 ("5", w.rb_disk), ("6", w.rb_file), ("7", w.rb_verify), ("8", w.btn_restore)])

    def confirm_shot():
        for top in Gtk.Window.list_toplevels():
            if isinstance(top, Gtk.MessageDialog) and top.get_visible():
                handled.add(id(top))
                find(top, Gtk.Entry).set_text("sdc")
                GLib.timeout_add(700, lambda: (shot("07-ripristino-conferma", top),
                                               top.response(Gtk.ResponseType.CANCEL), False)[-1])
                return False
        return True
    GLib.timeout_add(500, confirm_shot)
    confirm_mode[0] = True
    w.confirm_destroy(FAKE_DEVICES[2], "Cancella e ripristina")
    confirm_mode[0] = False
    yield 500

    w.rb_verify.set_active(True)
    yield 400
    dialog_queue.append("09-verifica-completata")
    w.on_start_restore()
    while "verifica" not in w.lbl_status.get_text().lower() and "download" not in w.lbl_status.get_text():
        yield 200
    yield 3500
    shot("08-verifica-in-corso")
    while w.proc:
        yield 300
    yield 2500

    dialog_queue.append("10-informazioni")
    w.on_about()
    yield 1500

    # backup interrotto d'esempio e ricerca con «Cerca backup interrotti»
    idir = os.path.join(cloud, "BKHD", "pc-casa-sdb-20261012-0930")
    shutil.copytree(bdir, idir)
    mi = json.load(open(os.path.join(idir, "manifest.json")))
    mi.update(name="pc-casa-sdb-20261012-0930", status="in_progress", chunks_done=3,
              created="2026-10-12T09:30:12+02:00", stored_bytes=180 << 20)
    mi["source"]["serial"] = "60A44C3FAE21"
    json.dump(mi, open(os.path.join(idir, "manifest.json"), "w"))
    w.stack.set_visible_child_name("backup")
    w.on_search_interrupted()
    yield 2500
    w.int_tree.get_selection().select_path(Gtk.TreePath(0))
    yield 400
    shot("13-interrotti", marks=[("1", w.btn_find_int), ("2", w.int_tree), ("3", w.int_info), ("4", w.btn_resume)])

    # controllo del disco con due settori illeggibili simulati
    os.environ["BKHD_TEST_BAD_SECTORS"] = "1048576,734003200"
    dialog_queue.append("14-controllo-disco")
    w.on_scan()
    while w.proc:
        yield 300
    yield 2500
    os.environ.pop("BKHD_TEST_BAD_SECTORS")

    # scheda Pennetta avviabile
    w.live_iso.set_text(ISO)
    w.refresh_devices()
    w.usb_combo.set_active_id("/dev/sdd")
    w.disc_combo.set_active_id("/dev/sr0")
    w.stack.set_visible_child_name("live")
    yield 800
    page = w.stack.get_child_by_name("live")
    adj = page.get_vadjustment()
    adj.set_value(0)
    yield 600
    shot("15-pennetta", marks=[("1", w.live_iso), ("2", w.btn_save_iso), ("3", w.usb_combo), ("4", w.usb_info),
                                ("5", w.data_check), ("6", w.btn_live_write)])
    adj.set_value(adj.get_upper() - adj.get_page_size())
    yield 600
    shot("17-formatta-dvd", marks=[("1", w.fmt_fs), ("2", w.fmt_label), ("3", w.btn_use_dest), ("4", w.btn_format),
                                    ("5", w.disc_combo), ("6", w.btn_burn)])
    adj.set_value(0)

    w.stack.set_visible_child_name("backup")
    w.set_zoom(1.25)
    yield 800
    w.menu_btn.set_active(True)
    yield 900
    shot("11-zoom-menu")
    w.menu_btn.set_active(False)
    w.set_zoom(1.0)
    yield 300
    w.set_theme("notte")
    w.stack.set_visible_child_name("restore")
    yield 900
    shot("16-tema-notte")
    w.set_theme("giorno")
    yield 300
    Gtk.main_quit()
    yield None


gen = steps()


def tick():
    d = next(gen)
    if d:
        GLib.timeout_add(d, tick)
    return False


GLib.timeout_add(250, watch_dialogs)
GLib.idle_add(tick)
Gtk.main()
