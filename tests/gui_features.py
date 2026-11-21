#!/usr/bin/python3
"""Test della GUI per le funzioni 1.2: destinazione locale, password, backup interrotto -> «Cerca backup
interrotti» -> «Riprendi», ripristino di un backup protetto.

Uso: BKHD_NO_PKEXEC=1 XDG_CONFIG_HOME=<cartella di prova> gui_features.py IMMAGINE CARTELLA_LAVORO
"""
import hashlib
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "src"))
import bkhd_gui as G  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

img, work = sys.argv[1], sys.argv[2]
dest = os.path.join(work, "disco-esterno")
shutil.rmtree(dest, ignore_errors=True)
os.makedirs(dest)
PASSWORD = "nuvola-azzurra-2026"
w = G.BkhdWindow()
w.show_all()
w.r_pw_label.hide()
w.r_pw.hide()
w.message = lambda kind, title, text="": w.log(f"[messaggio] {title} {text}")
w.ask = lambda *a, **k: True
w.ask_password = lambda *a, **k: PASSWORD
results = []


def check(cond, what):
    results.append((bool(cond), what))
    print(("  OK  " if cond else "  FALLITO  ") + what, flush=True)


def shot(name):
    while Gtk.events_pending():
        Gtk.main_iteration()
    subprocess.run(["import", "-window", str(w.get_window().get_xid()), os.path.join(work, name + ".png")])


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def log_text():
    a, b = w.logbuf.get_bounds()
    return w.logbuf.get_text(a, b, False)


def steps():
    yield 1200
    # backup locale protetto da password, interrotto al secondo pacchetto
    w.extra_sources.append((img, f"{img} — file immagine"))
    w.refresh_devices()
    w.src_combo.set_active_id(img)
    w.b_rb_local.set_active(True)
    w.b_local.set_text(dest)
    w.b_name.set_text("gui-cifrato")
    w.preset_combo.set_active_id(str(2880))
    w.zstd_check.set_active(True)
    w.enc_check.set_active(True)
    w.pw1.set_text(PASSWORD)
    w.pw2.set_text(PASSWORD)
    w.on_start_backup()
    while "Pacchetto 2/" not in log_text():
        yield 50
    w.send_cancel()
    while w.proc:
        yield 200
    check("annullata" in w.lbl_status.get_text().lower(), "backup interrotto con «Annulla»")
    # nuovo avvio: cerca i backup interrotti e riprende
    w.enc_check.set_active(False)
    w.b_name.set_text("nome-diverso")
    w.on_search_interrupted()
    while not w.btn_find_int.get_sensitive():
        yield 200
    yield 300
    check(len(w.int_store) == 1 and w.int_store[0][0] == "gui-cifrato", "«Cerca backup interrotti» lo trova")
    check(w.int_store[0][4] == "sì", "segnalato come protetto da password")
    shot("interrotti")
    w.on_resume_interrupted()
    while w.proc:
        yield 300
    check(w.b_name.get_text() == "gui-cifrato", "nome del backup ripreso impostato")
    check("Ripresa del backup interrotto" in log_text(), "la copia riprende dal punto di interruzione")
    check("Backup completato" in log_text(), "backup ripreso fino alla fine")
    # ripristino dalla scheda Ripristino con la password
    w.stack.set_visible_child_name("restore")
    w.r_rb_local.set_active(True)
    w.r_local.set_text(dest)
    w.on_search_backups()
    while not w.btn_search.get_sensitive():
        yield 200
    yield 300
    check(len(w.store) == 1 and w.store[0][6] == "🔒", "backup protetto elencato con il lucchetto")
    w.tree.get_selection().select_path(Gtk.TreePath(0))
    w.rb_file.set_active(True)
    out = os.path.join(work, "gui-ripristinato.img")
    w.dst_file.set_text(out)
    check(not w.btn_restore.get_sensitive(), "senza password il ripristino non si avvia")
    w.r_pw.set_text(PASSWORD)
    check(w.btn_restore.get_sensitive(), "con la password il pulsante si attiva")
    shot("ripristino-protetto")
    w.on_start_restore()
    while w.proc:
        yield 300
    check(os.path.exists(out) and sha(out) == sha(img), "immagine ripristinata identica all'originale")
    ok = all(r for r, _ in results)
    print("TEST GUI 1.2:", "TUTTI SUPERATI" if ok else "CI SONO ERRORI", flush=True)
    if not ok:
        print(log_text())
    Gtk.main_quit()
    yield None


gen = steps()


def tick():
    try:
        d = next(gen)
    except Exception:
        Gtk.main_quit()
        raise
    if d:
        GLib.timeout_add(d, tick)
    return False


GLib.idle_add(tick)
Gtk.main()
