#!/usr/bin/python3
"""Test automatico della GUI: backup di un file immagine sul finto cloud, ricerca e ripristino.
Uso: FAKE_RCLONE_ROOT=... BKHD_NO_PKEXEC=1 PATH=<dir con rclone finto>:$PATH gui_smoke.py IMG OUTDIR"""
import hashlib, os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "src"))
import bkhd_gui as G
from gi.repository import GLib, Gtk

img, out = sys.argv[1], sys.argv[2]
w = G.BkhdWindow()
w.show_all()
w.message = lambda kind, title, text="": w.log(f"[dialog] {title} {text}")
w.ask = lambda *a, **k: True
shots = [0]

def shot(tag):
    while Gtk.events_pending():
        Gtk.main_iteration()
    xid = w.get_window().get_xid()
    shots[0] += 1
    subprocess.run(["import", "-window", str(xid), f"{out}/{shots[0]:02d}-{tag}.png"])

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()

def step():
    yield 1500
    w.extra_sources.append((img, f"{img} — file immagine — {G.eng.human(os.path.getsize(img))}"))
    w.refresh_devices()
    w.src_combo.set_active_id(img)
    w.b_remote.get_child().set_text("test:")
    w.chunk_spin.set_value(650)
    w.b_name.set_text("gui-test")
    w.zstd_check.set_active(True)
    shot("backup-form")
    w.on_start_backup()
    yield 2500
    shot("backup-running")
    while w.proc:
        yield 300
    shot("backup-done")
    w.stack.set_visible_child_name("restore")
    w.r_remote.get_child().set_text("test:")
    w.on_search_backups()
    yield 2500
    w.rb_file.set_active(True)
    w.dst_file.set_text(out + "/gui-restored.img")
    for row in w.store:
        if row[0] == "gui-test":
            w.tree.get_selection().select_iter(row.iter)
    shot("restore-form")
    w.on_start_restore()
    yield 2000
    shot("restore-running")
    while w.proc:
        yield 300
    shot("restore-done")
    ok = sha(img) == sha(out + "/gui-restored.img")
    print("GUI ROUNDTRIP", "OK" if ok else "FALLITO")
    start, end = w.logbuf.get_bounds()
    print(w.logbuf.get_text(start, end, False))
    Gtk.main_quit()
    yield None

gen = step()
def tick():
    d = next(gen)
    if d:
        GLib.timeout_add(d, tick)
    return False
GLib.idle_add(tick)
Gtk.main()
