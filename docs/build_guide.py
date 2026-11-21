#!/usr/bin/python3
"""Crea docs/Guida-BKHD-Backup.pdf da docs/guida.html (schermate in docs/img, generate da make_screenshots.py).

Usa Chromium/Chrome in modalità headless per la stampa in PDF.
"""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import bkhd_engine  # noqa: E402

rects = json.load(open(os.path.join(HERE, "img", "rects.json")))


def cropped(name, info):
    """Ritaglia la schermata attorno ai widget indicati (piu' 40 px di margine sopra e sotto)."""
    iw, ih = info["size"]
    top = max(min(m[2] for m in info["marks"]) - 40, 0)
    bottom = min(max(m[2] + m[4] for m in info["marks"]) + 40, ih)
    out = f"{name}-ritaglio"
    subprocess.run(["convert", os.path.join(HERE, "img", name + ".png"), "-crop", f"{iw}x{bottom - top}+0+{top}",
                    "+repage", os.path.join(HERE, "img", out + ".png")], check=True)
    marks = [[lbl, x, y - top, w, h] for lbl, x, y, w, h in info["marks"]]
    return out, {"size": [iw, bottom - top], "marks": marks}


def figure(spec):
    """Schermata con i numeri a margine e linee guida; numeri vicini vengono sfalsati."""
    name, caption, width, *opts = spec.split("|")
    info = rects.get(name, {"size": [1, 1], "marks": []})
    if "ritaglia" in opts:
        name, info = cropped(name, info)
    iw, ih = info["size"]
    min_gap = 7.5 / (178 * float(width) / 100) * iw / ih * 100     # ~7,5 mm in % dell'altezza
    placed = {"L": [], "R": []}
    parts = []
    for lbl, x, y, w, h in info["marks"]:
        cy = (y + h / 2) / ih * 100
        side = "R" if any(abs(cy - c) < min_gap * 0.25 for c, _ in placed["L"]) else "L"
        level = 0
        while any(abs(cy - c) < min_gap and lv == level for c, lv in placed[side]):
            level += 1
        placed[side].append((cy, level))
        off = 3.5 + level * 5.5
        if side == "L":
            x0 = x / iw * 100
            parts.append(f'<span class="lead" style="left:{-off:.2f}%;width:{x0 + off:.2f}%;top:{cy:.2f}%"></span>'
                         f'<span class="badge" style="left:{-off:.2f}%;top:{cy:.2f}%">{lbl}</span>')
        else:
            x1 = (x + w) / iw * 100
            parts.append(f'<span class="lead" style="left:{x1:.2f}%;width:{100 - x1 + off:.2f}%;top:{cy:.2f}%"></span>'
                         f'<span class="badge" style="left:{100 + off:.2f}%;top:{cy:.2f}%">{lbl}</span>')
    return (f'<figure><div class="shot" style="width:{width}%"><img src="img/{name}.png" alt="">{"".join(parts)}'
            f'</div><figcaption>{caption}</figcaption></figure>')


html = open(os.path.join(HERE, "guida.html"), encoding="utf-8").read()
html = html.replace("{{VERSION}}", bkhd_engine.VERSION)
html = html.replace("{{STRIPE}}", "".join('<i class="on"></i>' if i == 0 else "<i></i>" for i in range(39)))
while "{{FIG:" in html:
    a = html.index("{{FIG:")
    b = html.index("}}", a)
    html = html[:a] + figure(html[a + 6:b]) + html[b + 2:]
built = os.path.join(HERE, "_guida_build.html")
open(built, "w", encoding="utf-8").write(html)

# server locale: Chromium in snap non puo' leggere i file nelle cartelle nascoste
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


handler = functools.partial(QuietHandler, directory=ROOT)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/docs/_guida_build.html"

browser = next((shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                if shutil.which(b)), None)
if not browser:
    sys.exit("Serve Chromium o Google Chrome per creare il PDF")
is_snap = browser.startswith("/snap/") or os.path.basename(os.path.realpath(browser)) == "snap"
tmp_dir = os.path.expanduser("~/snap/chromium/common") if is_snap else HERE
tmp_pdf = os.path.join(tmp_dir, "bkhd-guida-tmp.pdf")
subprocess.run([browser, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=5000",
                f"--print-to-pdf={tmp_pdf}", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
srv.shutdown()
out = os.path.join(HERE, "Guida-BKHD-Backup.pdf")
shutil.move(tmp_pdf, out)
os.remove(built)
print("Creato:", out)
