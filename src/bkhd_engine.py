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
"""
bkhd-engine - motore di backup / ripristino settore per settore verso il cloud.

Flusso del backup (per ogni pacchetto):
  1. legge N byte dal disco (settore per settore) e li scrive in una cartella
     temporanea montata in RAM (tmpfs, "noswap" quando il kernel lo supporta):
     nulla viene scritto sui dischi locali;
  2. carica il pacchetto .bkhd sul cloud tramite rclone e ATTENDE la fine
     dell'upload, poi verifica la dimensione remota;
  3. cancella il pacchetto dalla RAM: da quel momento esiste solo sul cloud;
  4. aggiorna il manifest remoto (permette di riprendere un backup interrotto)
     e passa al pacchetto successivo.

Il ripristino fa il percorso inverso: scarica un pacchetto in RAM, ne verifica
l'integrita' (SHA-256), lo scrive sul disco di destinazione, svuota la RAM e
passa al successivo.

Puo' essere usato da riga di comando (sudo bkhd-engine backup ...) oppure
dall'interfaccia grafica, che lo avvia tramite pkexec con l'opzione --json.
"""

import argparse
import bisect
import contextlib
import datetime
import errno
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "1.3.0"
AUTHOR = "fabionet"
AUTHOR_EMAIL = "fabionet666@gmail.com"
COPYRIGHT = f"© 2026 {AUTHOR}"
LICENSE = "GPL-3.0-or-later"

MIB = 1024 * 1024
GIB = 1024 * MIB
KIB = 1024
MIN_CHUNK_KIB = 1200            # floppy 5,25" HD da 1,2 MB
MAX_CHUNK_KIB = 8192 * 1024     # 8 GB
INDEX_BATCH = int(os.environ.get("BKHD_INDEX_BATCH", "1000") if os.geteuid() != 0 else 1000)    # pacchetti descritti in ogni file indice
INDEX_DIR = "index"
SAVE_INTERVAL = float(os.environ.get("BKHD_SAVE_INTERVAL", "30") if os.geteuid() != 0 else 30)  # secondi tra due salvataggi dell'indice
_TEST = os.geteuid() != 0                 # le soglie regolabili per i test valgono solo senza privilegi
GROUP_THRESHOLD = int(os.environ.get("BKHD_GROUP_THRESHOLD", "10000") if _TEST else 10000)  # oltre: sottocartelle da 1000 file
VERIFY_MIN_SIZE = 64 * MIB      # sotto questa dimensione basta la verifica fatta da rclone
QUIET_THRESHOLD = 2000          # oltre questo numero di pacchetti il registro diventa sintetico
IO_BLOCK = 4 * MIB              # blocco di lettura/scrittura
HEADER_SIZE = 4096              # intestazione di ogni file .bkhd
MAGIC = b"BKHD\x01\n"
CHUNK_EXT = ".bkhd"
MANIFEST_NAME = "manifest.json"
USEDMAP_NAME = "usedmap.json"
RAM_MARGIN = 512 * MIB          # RAM che deve restare libera oltre al pacchetto
TMPFS_SLACK = 64 * MIB          # spazio extra nel tmpfs (intestazione, manifest)

BLKSSZGET = 0x1268              # dimensione settore logico
BLKPBSZGET = 0x127B             # dimensione settore fisico
BLKRRPART = 0x125F              # rilettura tabella partizioni
BAD_READ_ERRNOS = {errno.EIO, errno.ENODATA, errno.EBADMSG, errno.EILSEQ}

CANCEL = threading.Event()


class Cancelled(Exception):
    pass


class EngineError(Exception):
    pass


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# ---------------------------------------------------------------------------
# Output (testo leggibile oppure eventi JSON per la GUI)
# ---------------------------------------------------------------------------

class Reporter:
    def __init__(self, json_mode):
        self.json = json_mode
        self.lock = threading.Lock()
        self._progress_shown = False
        self._last_progress = 0.0

    def emit(self, etype, **kw):
        kw["type"] = etype
        with self.lock:
            if self.json:
                sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                return
            if etype == "progress":
                line = (f"\r[{kw.get('chunk')}/{kw.get('chunks')}] {kw.get('phase'):<9} "
                        f"{100 * kw.get('frac', 0):5.1f}%  {human(kw.get('speed', 0))}/s  "
                        f"totale {100 * kw.get('overall', 0):5.1f}%   ")
                sys.stderr.write(line)
                sys.stderr.flush()
                self._progress_shown = True
                return
            if self._progress_shown:
                sys.stderr.write("\n")
                self._progress_shown = False
            if etype == "log":
                prefix = {"warning": "ATTENZIONE: ", "error": "ERRORE: "}.get(kw.get("level"), "")
                print(prefix + kw.get("msg", ""), flush=True)
            elif etype == "phase":
                unit = "Fase" if kw.get("phase") in ("live", "scan") or kw.get("unit") == "Fase" else "Pacchetto"
                print(f"{unit} {kw['chunk']}/{kw['chunks']}: {kw['label']}", flush=True)
            elif etype == "detail":
                print("   · " + kw.get("text", ""), flush=True)
            elif etype == "chunk_done" and kw.get("msg"):
                print(kw["msg"], flush=True)
            elif etype == "done":
                print(kw.get("msg", "Completato."), flush=True)
            elif etype == "error":
                print("ERRORE: " + kw.get("msg", ""), file=sys.stderr, flush=True)

    def log(self, msg, level="info"):
        self.emit("log", level=level, msg=msg)

    def progress(self, force=False, **kw):
        t = time.monotonic()
        if not force and t - self._last_progress < 0.4:
            return
        self._last_progress = t
        self.emit("progress", **kw)


# ---------------------------------------------------------------------------
# Utilita' di sistema
# ---------------------------------------------------------------------------

def mem_available():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def write_all(fd, data, offset=None):
    mv = memoryview(data)
    done = 0
    while done < len(mv):
        if offset is None:
            n = os.write(fd, mv[done:])
        else:
            n = os.pwrite(fd, mv[done:], offset + done)
        if n <= 0:
            raise EngineError("scrittura non riuscita")
        done += n


# solo per i test automatici: simula settori illeggibili agli offset indicati (byte, separati da virgole)
_TEST_BAD = ({int(x) for x in os.environ.get("BKHD_TEST_BAD_SECTORS", "").split(",") if x.strip().isdigit()}
             if os.geteuid() != 0 else set())


def read_exact(fd, mv, offset):
    if _TEST_BAD and any(offset <= b < offset + len(mv) for b in _TEST_BAD):
        raise OSError(errno.EIO, "settore illeggibile (simulato)")
    got = 0
    while got < len(mv):
        n = os.preadv(fd, [mv[got:]], offset + got)
        if n == 0:
            raise EngineError(f"fine del dispositivo inattesa all'offset {offset + got}")
        got += n


def sha256_of_fd(fd, start, reporter=None, ctx=None):
    """SHA-256 del contenuto di un file aperto, dall'offset 'start' (nessuna riapertura per percorso)."""
    h = hashlib.sha256()
    if True:
        size = os.fstat(fd).st_size - start
        pos = 0
        t0 = time.monotonic()
        while pos < size:
            check_cancel()
            data = os.pread(fd, IO_BLOCK, start + pos)
            if not data:
                break
            h.update(data)
            pos += len(data)
            if reporter and ctx:
                reporter.progress(phase="verify", frac=pos / max(size, 1),
                                  speed=pos / max(time.monotonic() - t0, 1e-6), **ctx(pos / max(size, 1)))
    return h.hexdigest()


def c_env():
    """Ambiente con messaggi in inglese: l'output degli strumenti di sistema viene interpretato dal programma."""
    env = dict(os.environ)
    env.update(LC_ALL="C", LANG="C", LANGUAGE="C")
    return env


def is_block(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def device_info(path):
    """Informazioni sulla sorgente (disco, partizione o file immagine)."""
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        kind = "block"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        raise EngineError(f"{path} non e' un disco, una partizione o un file immagine")
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        lsec, psec = 512, 512
        if kind == "block":
            buf = bytearray(4)
            try:
                fcntl.ioctl(fd, BLKSSZGET, buf)
                lsec = struct.unpack("i", buf)[0]
                fcntl.ioctl(fd, BLKPBSZGET, buf)
                psec = struct.unpack("I", buf)[0]
            except OSError:
                pass
    finally:
        os.close(fd)
    info = {"path": path, "kind": kind, "size": size,
            "logical_sector": lsec, "physical_sector": psec}
    if kind == "block":
        try:
            out = subprocess.run(["lsblk", "-J", "-b", "-o",
                                  "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,PTTYPE,PTUUID,FSTYPE,LABEL,UUID",
                                  path], capture_output=True, text=True, timeout=20)
            dev = json.loads(out.stdout)["blockdevices"][0]
            info.update(model=(dev.get("model") or "").strip(), serial=dev.get("serial"),
                        pttype=dev.get("pttype"), ptuuid=dev.get("ptuuid"), lsblk=dev)
        except Exception:
            pass
        if shutil.which("sfdisk"):
            out = subprocess.run(["sfdisk", "-d", path], capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                info["partition_table"] = out.stdout
    return info


def block_in_use(path):
    """Restituisce i motivi per cui un disco (o una sua partizione) e' in uso."""
    reasons = []
    real = os.path.realpath(path)
    name = os.path.basename(real)
    sysdir = f"/sys/class/block/{name}"
    names = {name}
    if os.path.isdir(sysdir):
        for entry in os.listdir(sysdir):
            if entry.startswith(name) and os.path.exists(f"{sysdir}/{entry}/partition"):
                names.add(entry)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt = line.split()[:2]
                if dev.startswith("/dev/") and os.path.basename(os.path.realpath(dev)) in names:
                    reasons.append(f"{dev} montato su {mnt}")
    except OSError:
        pass
    try:
        with open("/proc/swaps") as f:
            for line in list(f)[1:]:
                dev = line.split()[0]
                if os.path.basename(os.path.realpath(dev)) in names:
                    reasons.append(f"{dev} usato come swap")
    except OSError:
        pass
    for n in names:
        holders = f"/sys/class/block/{n}/holders"
        if os.path.isdir(holders) and os.listdir(holders):
            reasons.append(f"/dev/{n} usato da {', '.join(os.listdir(holders))} (LVM/RAID/cifratura)")
    return reasons


class SleepInhibitor:
    """Impedisce la sospensione del PC durante l'operazione (systemd-inhibit)."""

    def __init__(self, why):
        self.proc = None
        self.why = why

    def __enter__(self):
        exe = shutil.which("systemd-inhibit")
        if exe:
            try:
                self.proc = subprocess.Popen(
                    [exe, "--what=sleep:idle:handle-lid-switch", "--who=BKHD Backup",
                     f"--why={self.why}", "--mode=block", "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.proc = None
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# Accesso sicuro ai file: il motore gira come root, quindi non segue mai link simbolici
# nelle cartelle che altri utenti possono modificare (O_NOFOLLOW, O_EXCL, operazioni via descrittore).
# ---------------------------------------------------------------------------

O_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_META = 256 * MIB            # dimensione massima di manifest, indici e mappe letti da una destinazione


def walk_dir(fd, comps, create=False, owner=None):
    """Scende nelle sottocartelle 'comps' partendo da 'fd' senza seguire link; restituisce un nuovo fd.
    Con create=True crea le cartelle mancanti (quelle nuove vengono assegnate a 'owner')."""
    cur = os.dup(fd)
    try:
        for comp in comps:
            if comp in ("", ".", "..") or "/" in comp:
                raise EngineError(f"percorso non valido: '{comp}'")
            try:
                nfd = os.open(comp, O_NOFOLLOW_DIR, dir_fd=cur)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(comp, 0o755, dir_fd=cur)
                except FileExistsError:
                    pass
                nfd = os.open(comp, O_NOFOLLOW_DIR, dir_fd=cur)
                if owner and os.geteuid() == 0:
                    os.fchown(nfd, owner.pw_uid, owner.pw_gid)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise EngineError(f"'{comp}' e' un collegamento simbolico o non e' una cartella: "
                                      "per sicurezza non viene usato")
                raise
            os.close(cur)
            cur = nfd
        return cur
    except BaseException:
        os.close(cur)
        raise


def open_dir_nofollow(path, create=False, owner=None):
    """Apre una cartella (percorso assoluto, gia' risolto) componente per componente senza seguire link."""
    path = os.path.normpath(path)
    if not os.path.isabs(path):
        raise EngineError(f"percorso non assoluto: {path}")
    root = os.open("/", O_NOFOLLOW_DIR)
    try:
        return walk_dir(root, [c for c in path.split("/") if c], create, owner)
    finally:
        os.close(root)


def create_exclusive(name, dir_fd, mode=0o600):
    """Crea un file nuovo nella cartella 'dir_fd' (sostituendo l'eventuale voce omonima) senza seguire link."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except (IsADirectoryError, PermissionError):
        raise EngineError(f"impossibile sostituire '{name}'")
    return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)


def owner_uid(owner):
    return owner.pw_uid if owner else os.geteuid()


def open_user_file(path, owner):
    """File immagine di destinazione scelto dall'utente: mai seguire link, mai riscrivere file non suoi."""
    path = os.path.abspath(path)
    name = os.path.basename(path)
    dfd = open_dir_nofollow(os.path.realpath(os.path.dirname(path)))
    try:
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=dfd)
            if owner and os.geteuid() == 0:
                os.fchown(fd, owner.pw_uid, owner.pw_gid)
            return fd
        except FileExistsError:
            pass
        try:
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise EngineError(f"{path} e' un collegamento simbolico: scegli un altro nome")
            raise
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid(owner) or st.st_nlink != 1:
            os.close(fd)
            raise EngineError(f"{path} esiste gia' e non e' un file immagine tuo: scegli un altro nome")
        return fd
    finally:
        os.close(dfd)


def secure_run_dir():
    """/run/bkhd: cartella dell'amministratore, inaccessibile agli altri utenti."""
    base = "/run/bkhd"
    try:
        os.mkdir(base, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(base)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
        raise EngineError(f"{base} non e' una cartella dell'amministratore: rimuovila e riprova")
    os.chmod(base, 0o700)
    return base


# ---------------------------------------------------------------------------
# Cartella temporanea in RAM (solo per l'amministratore)
# ---------------------------------------------------------------------------

class RamStore:
    """
    Da root monta un tmpfs dedicato, accessibile solo all'amministratore (con 'noswap' se supportato, cosi'
    i dati non finiscono nemmeno nella partizione di swap). rclone, che gira come utente, non vi accede:
    riceve e restituisce i dati attraverso un flusso. Senza privilegi (test) usa una cartella privata in /dev/shm.
    """

    def __init__(self, size, reporter):
        self.size = size
        self.rep = reporter
        self.dir = None
        self.dfd = None
        self.mounted = False

    def __enter__(self):
        if os.geteuid() == 0:
            self.dir = tempfile.mkdtemp(prefix="ram-", dir=secure_run_dir())
            opts = f"size={self.size},mode=0700,uid=0,gid=0"
            r = None
            for extra, desc in ((",noswap", "tmpfs senza swap"), ("", "tmpfs")):
                r = subprocess.run(["mount", "-t", "tmpfs", "-o", opts + extra, "bkhd-ram", self.dir],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    self.mounted = True
                    self.rep.log(f"Cartella temporanea in RAM: {self.dir} ({desc}, {human(self.size)})")
                    if not extra:
                        self.rep.log("Il kernel non supporta 'noswap': se il sistema ha una swap attiva, "
                                     "parte dei dati potrebbe finirvi in caso di poca RAM.", "warning")
                    break
            if not self.mounted:
                os.rmdir(self.dir)
                raise EngineError("impossibile montare il tmpfs in RAM: " + (r.stderr.strip() if r else ""))
        else:
            self.dir = tempfile.mkdtemp(prefix="bkhd-", dir="/dev/shm")
            st = os.statvfs(self.dir)
            if st.f_bavail * st.f_frsize < self.size:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise EngineError(f"/dev/shm non ha {human(self.size)} liberi")
            self.rep.log(f"Cartella temporanea in RAM: {self.dir} (/dev/shm)")
        self.dfd = os.open(self.dir, O_NOFOLLOW_DIR)
        return self

    def path(self, name):
        return os.path.join(self.dir, os.path.basename(name))

    def create(self, name):
        return create_exclusive(os.path.basename(name), self.dfd)

    def open_read(self, name):
        return os.open(os.path.basename(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.dfd)

    def remove(self, name):
        try:
            os.unlink(os.path.basename(name), dir_fd=self.dfd)
        except FileNotFoundError:
            pass

    def clear(self):
        for entry in os.listdir(self.dfd):
            try:
                os.unlink(entry, dir_fd=self.dfd)
            except OSError:
                pass

    def __exit__(self, *exc):
        if not self.dir:
            return
        if self.dfd is not None:
            self.clear()
            os.close(self.dfd)
            self.dfd = None
        if self.mounted:
            for args in (["umount", self.dir], ["umount", "-l", self.dir]):
                if subprocess.run(args, capture_output=True).returncode == 0:
                    break
        try:
            os.rmdir(self.dir)
        except OSError:
            pass
        self.rep.log("RAM temporanea liberata.")


class ChunkHandle:
    """Pacchetto aperto per la verifica e la scrittura: sempre lo stesso descrittore (niente scambi a meta')."""

    def __init__(self, fd, cleanup=None):
        self.fd = fd
        self.cleanup = cleanup

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.cleanup:
            self.cleanup()
            self.cleanup = None


# ---------------------------------------------------------------------------
# rclone
# ---------------------------------------------------------------------------

RCLONE_LOG = ["--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE", "--low-level-retries", "20"]


def remote_join(remote, *parts):
    remote = remote.strip()
    path = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    if not path:
        return remote
    if remote.endswith(":"):
        return remote + path
    return remote.rstrip("/") + "/" + path


def resolve_run_as(explicit):
    """Utente con cui eseguire rclone (quello che ha lanciato pkexec/sudo): mai l'amministratore."""
    if explicit:
        try:
            pw = pwd.getpwnam(explicit)
        except KeyError:
            raise EngineError(f"utente sconosciuto: {explicit}")
        if pw.pw_uid == 0:
            raise EngineError("--run-as non puo' essere l'amministratore")
        return pw
    if os.geteuid() != 0:
        return None
    for var in ("PKEXEC_UID", "SUDO_UID"):
        uid = os.environ.get(var)
        if uid and uid.isdigit() and int(uid) != 0:
            try:
                return pwd.getpwuid(int(uid))
            except KeyError:
                pass
    return None


def check_trusted_binary(path):
    """Da root si esegue solo un rclone di sistema (non modificabile da utenti normali)."""
    found = path if os.path.isabs(path) else shutil.which(path)
    if not found:
        raise EngineError("rclone non trovato: installalo con 'sudo apt install rclone'")
    real = os.path.realpath(found)
    for p in (real, os.path.dirname(real)):
        st = os.stat(p)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise EngineError(f"{path}: per sicurezza rclone deve essere un programma di sistema "
                              "(di proprieta' dell'amministratore e non modificabile dagli utenti)")
    return real


def with_retries(rep, attempts, what, fn):
    """Ripete 'fn' (che restituisce (ok, errore)) con attese crescenti."""
    delay = 15
    for attempt in range(1, attempts + 1):
        check_cancel()
        ok, err = fn()
        if ok:
            return
        if attempt == attempts:
            raise EngineError(f"{what} non riuscito dopo {attempts} tentativi: {err}")
        if rep:
            rep.log(f"{what}: tentativo {attempt}/{attempts} fallito ({err}). Nuovo tentativo tra {delay} s...",
                    "warning")
        if CANCEL.wait(delay):
            raise Cancelled()
        delay = min(delay * 2, 300)


class Rclone:
    def __init__(self, binary="rclone", config=None, run_as=None):
        self.binary = binary
        self.config = config
        self.run_as = run_as
        self._checked = False

    def available(self):
        return bool(shutil.which(self.binary) or os.path.isfile(self.binary))

    def _cmd(self, args):
        if os.geteuid() == 0 and not self._checked:
            self.binary = check_trusted_binary(self.binary)
            self._checked = True
        cmd = [self.binary]
        if self.config:
            cmd += ["--config", self.config]
        return cmd + args

    def _kwargs(self):
        env = {k: v for k, v in os.environ.items() if k.startswith("RCLONE_")}
        if os.geteuid() != 0:                    # solo per i test automatici
            env.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_RCLONE")})
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
        if os.geteuid() != 0:
            env["PATH"] = os.environ.get("PATH") or env["PATH"]
        env["LANG"] = "C.UTF-8"
        kw = {"env": env}
        if self.run_as and os.geteuid() == 0 and self.run_as.pw_uid != 0:
            env.update(HOME=self.run_as.pw_dir, USER=self.run_as.pw_name, LOGNAME=self.run_as.pw_name)
            kw.update(user=self.run_as.pw_uid, group=self.run_as.pw_gid,
                      extra_groups=os.getgrouplist(self.run_as.pw_name, self.run_as.pw_gid), cwd="/")
        else:
            env["HOME"] = os.environ.get("HOME") or pwd.getpwuid(os.geteuid()).pw_dir
        return kw

    def run(self, args, timeout=600):
        try:
            return subprocess.run(self._cmd(args), capture_output=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, **self._kwargs())
        except FileNotFoundError:
            raise EngineError("rclone non trovato: installalo con 'sudo apt install rclone'")
        except subprocess.TimeoutExpired:
            raise EngineError("rclone non ha risposto in tempo: " + " ".join(args[:2]))

    @staticmethod
    def _err(res):
        lines = [l for l in res.stderr.decode(errors="replace").splitlines() if l.strip()]
        return " | ".join(lines[-3:]) or f"codice di uscita {res.returncode}"

    def listremotes(self):
        res = self.run(["listremotes"], timeout=30)
        if res.returncode != 0:
            raise EngineError("rclone listremotes: " + self._err(res))
        return [l.strip() for l in res.stdout.decode().splitlines() if l.strip()]

    def list_names(self, remote_dir, dirs=False):
        """Nomi contenuti in una cartella remota ([] se non esiste)."""
        res = self.run(["lsf", "--dirs-only" if dirs else "--files-only", remote_dir], timeout=300)
        if res.returncode == 3:      # directory not found
            return []
        if res.returncode != 0:
            raise EngineError(f"impossibile elencare {remote_dir}: " + self._err(res))
        return [l.rstrip("/") for l in res.stdout.decode().splitlines() if l.strip()]

    def remote_size(self, remote_file):
        res = self.run(["lsjson", "--no-mimetype", "--no-modtime", remote_file], timeout=300)
        if res.returncode != 0:
            return None
        try:
            items = json.loads(res.stdout.decode() or "[]")
        except ValueError:
            return None
        name = remote_file.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        for it in items:
            if isinstance(it, dict) and (it.get("Name") == name or len(items) == 1):
                return it.get("Size")
        return None

    def delete_backup_files(self, remote_dir):
        self.run(["delete", remote_dir, "--include", "*" + CHUNK_EXT, "--include", MANIFEST_NAME,
                  "--include", f"/{INDEX_DIR}/index-*.json", "--include", USEDMAP_NAME], timeout=3600)

    def _popen(self, args, stdin, stdout):
        try:
            return subprocess.Popen(self._cmd(args), stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                                    **self._kwargs())
        except FileNotFoundError:
            raise EngineError("rclone non trovato: installalo con 'sudo apt install rclone'")

    def _watch(self, proc, on_progress=None, reader=None):
        """Attende rclone leggendo le statistiche JSON; 'reader' consuma lo standard output (download)."""
        finished = threading.Event()
        errors = []

        def killer():
            while not finished.wait(0.3):
                if CANCEL.is_set():
                    proc.terminate()
                    return

        def stderr_reader():
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    errors.append(line[:300])
                    continue
                if not isinstance(d, dict):
                    continue
                st = d.get("stats")
                if isinstance(st, dict):
                    if on_progress:
                        on_progress(st.get("bytes", 0) or 0, st.get("totalBytes", 0) or 0, st.get("speed", 0) or 0)
                elif d.get("level") in ("error", "critical", "fatal"):
                    errors.append(str(d.get("msg", line))[:300])

        threading.Thread(target=killer, daemon=True).start()
        t = threading.Thread(target=stderr_reader, daemon=True)
        t.start()
        try:
            if reader:
                reader(proc.stdout)
            rc = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            finished.set()
            t.join(5)
        check_cancel()
        if rc == 0:
            return True, ""
        return False, " | ".join(errors[-3:]) or f"rclone ha restituito il codice {rc}"

    def upload_fd(self, fd, remote, on_progress=None):
        """Carica il contenuto di un file aperto da root: rclone lo riceve come flusso (rcat)."""
        size = os.fstat(fd).st_size
        os.lseek(fd, 0, os.SEEK_SET)
        proc = self._popen(["rcat", "--size", str(size)] + RCLONE_LOG + [remote], stdin=fd,
                           stdout=subprocess.DEVNULL)
        return self._watch(proc, on_progress)

    def upload_bytes(self, data, remote):
        proc = self._popen(["rcat", "--size", str(len(data))] + RCLONE_LOG + [remote], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL)

        def feed():
            try:
                proc.stdin.write(data)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=feed, daemon=True).start()
        return self._watch(proc)

    def download_to_fd(self, remote, fd, max_bytes, on_progress=None):
        """Scarica 'remote' nel file di root 'fd' (rclone cat): rclone non tocca mai i file del motore."""
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        proc = self._popen(["cat"] + RCLONE_LOG + [remote], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        t0 = time.monotonic()

        def reader(out):
            got = 0
            for data in iter(lambda: out.read(MIB), b""):
                got += len(data)
                if got > max_bytes:
                    raise EngineError(f"{remote}: dati piu' grandi del previsto")
                write_all(fd, data)
                if on_progress:
                    on_progress(got, max_bytes, got / max(time.monotonic() - t0, 1e-6))
                check_cancel()

        return self._watch(proc, None, reader)

    def cat(self, remote_file, max_bytes=MAX_META):
        buf = bytearray()

        def reader(out):
            for data in iter(lambda: out.read(MIB), b""):
                buf.extend(data)
                if len(buf) > max_bytes:
                    raise EngineError(f"{remote_file}: file troppo grande")

        proc = self._popen(["cat", "--use-json-log", remote_file], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE)
        ok, err = self._watch(proc, None, reader)
        if not ok:
            raise EngineError(f"impossibile leggere {remote_file}: {err}")
        return bytes(buf)


# ---------------------------------------------------------------------------
# Protezione con password (AES-256-GCM, chiave ricavata con scrypt)
# ---------------------------------------------------------------------------

ENC_ALG = "aes-256-gcm"
SCRYPT_PARAMS = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSWORD = 8
_PW_CHECK = b"BKHD password check v1"


class Crypto:
    """Chiavi di un backup protetto: 'enc' cifra i dati, 'mac' firma i checksum (che cosi' non rivelano nulla)."""

    def __init__(self, password, meta):
        if not isinstance(meta, dict) or meta.get("alg") != ENC_ALG or meta.get("kdf") != "scrypt":
            raise EngineError("cifratura del backup non supportata da questa versione")
        n, r, par, salt = meta.get("n"), meta.get("r"), meta.get("p"), meta.get("salt")
        if (type(n) is not int or n < 2 ** 14 or n > 2 ** 20 or n & (n - 1) or r != 8 or type(par) is not int
                or not 1 <= par <= 4 or not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{32}", salt)):
            raise EngineError("parametri di cifratura del backup non validi")
        try:
            key = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(meta["salt"]), n=meta["n"],
                                 r=meta["r"], p=meta["p"], maxmem=256 * MIB, dklen=64)
        except (ValueError, KeyError) as e:
            raise EngineError(f"parametri di cifratura non validi: {e}")
        self.enc_key, self.mac_key = key[:32], key[32:]
        self.meta = meta

    @classmethod
    def create(cls, password):
        meta = dict(alg=ENC_ALG, kdf="scrypt", salt=os.urandom(16).hex(), **SCRYPT_PARAMS)
        c = cls(password, meta)
        meta["check"] = c.check_value()
        return c

    @classmethod
    def open(cls, password, meta):
        if not password:
            raise EngineError("questo backup e' protetto da password: inseriscila per continuare")
        c = cls(password, meta)
        if not hmac.compare_digest(c.check_value(), meta.get("check", "")):
            raise EngineError("password errata")
        return c

    def check_value(self):
        return hmac.new(self.mac_key, _PW_CHECK, hashlib.sha256).hexdigest()

    def stream_hash(self):
        return hmac.new(self.mac_key, digestmod=hashlib.sha256)

    @staticmethod
    def aad(backup_id, index, offset, length, compression, sparse):
        # lega ogni pacchetto al suo backup, alla sua posizione e al suo formato: non si possono scambiare
        # pacchetti ne' cambiarne l'interpretazione
        return f"bkhd2:{backup_id}:{index}:{offset}:{length}:{compression}:{int(bool(sparse))}".encode()

    def sign(self, obj):
        """Firma (HMAC) di un manifest o di un file indice: i metadati non si possono alterare."""
        body = {k: v for k, v in obj.items() if k != "mac" and not k.startswith("_")}
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hmac.new(self.mac_key, b"bkhd-meta:" + data, hashlib.sha256).hexdigest()

    def verify(self, obj, what):
        if not hmac.compare_digest(str(obj.get("mac", "")), self.sign(obj)):
            raise EngineError(f"{what} del backup alterato o danneggiato (firma non valida)")

    def encryptor(self, nonce, aad):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        enc = Cipher(algorithms.AES(self.enc_key), modes.GCM(nonce)).encryptor()
        enc.authenticate_additional_data(aad)
        return enc

    def decryptor(self, nonce, tag, aad):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(self.enc_key), modes.GCM(nonce, tag)).decryptor()
        dec.authenticate_additional_data(aad)
        return dec


def new_stream_hash(crypto):
    return crypto.stream_hash() if crypto else hashlib.sha256()


def crypto_available():
    try:
        import cryptography.hazmat.primitives.ciphers  # noqa: F401
        return True
    except ImportError:
        return False


def read_password(args, confirm=False):
    """Password da --password-stdin (interfaccia grafica) o richiesta sul terminale."""
    pw = getattr(args, "password", None)
    if pw is not None:
        return pw
    if sys.stdin.isatty() and not args.json:
        pw = getpass.getpass("Password del backup: ")
        if confirm and getpass.getpass("Ripeti la password: ") != pw:
            raise EngineError("le password non coincidono")
        return pw
    return None


# ---------------------------------------------------------------------------
# Formato .bkhd
# ---------------------------------------------------------------------------

def build_header(meta):
    blob = MAGIC + json.dumps(meta, separators=(",", ":")).encode()
    if len(blob) >= HEADER_SIZE:
        raise EngineError("intestazione .bkhd troppo grande")
    return blob + b"\0" * (HEADER_SIZE - len(blob))


def parse_header(fd):
    blob = os.pread(fd, HEADER_SIZE, 0)
    if len(blob) < HEADER_SIZE or not blob.startswith(MAGIC):
        raise EngineError("il file non e' un pacchetto .bkhd valido")
    return json.loads(blob[len(MAGIC):].split(b"\0", 1)[0].decode())


NAME_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}"
HEX64 = re.compile(r"[0-9a-f]{64}")


def _ival(v, lo, hi, what):
    if type(v) is not int or not lo <= v <= hi:
        raise EngineError(f"backup non valido: {what}")
    return v


def validate_manifest(m):
    """Controlla tipi e limiti di tutti i campi usati: un manifest alterato non deve far danni."""
    if not isinstance(m, dict) or m.get("format") != "bkhd-manifest":
        raise EngineError("manifest non valido")
    _ival(m.get("version"), 1, 2, "versione")
    if not isinstance(m.get("name"), str) or not re.fullmatch(NAME_RE, m["name"]):
        raise EngineError("backup non valido: nome")
    if not isinstance(m.get("backup_id"), str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", m["backup_id"]):
        raise EngineError("backup non valido: identificativo")
    src = m.get("source")
    if not isinstance(src, dict):
        raise EngineError("backup non valido: sorgente")
    size = _ival(src.get("size"), 1, 1 << 60, "dimensione del disco")
    cs = _ival(m.get("chunk_size"), 512, MAX_CHUNK_KIB * KIB, "dimensione dei pacchetti")
    _ival(m.get("chunk_count"), (size + cs - 1) // cs, (size + cs - 1) // cs, "numero di pacchetti")
    if m.get("compression") not in ("none", "zstd"):
        raise EngineError("backup non valido: compressione")
    if m.get("status") not in ("in_progress", "complete"):
        raise EngineError("backup non valido: stato")
    if not isinstance(m.get("sparse", False), bool):
        raise EngineError("backup non valido: modalita'")
    if m["version"] >= 2:
        lay = m.get("layout") or {}
        if not isinstance(lay, dict):
            raise EngineError("backup non valido: disposizione")
        _ival(lay.get("digits", 5), 5, 12, "disposizione")
        if lay.get("group", 0) not in (0, 1000):
            raise EngineError("backup non valido: disposizione")
    elif not isinstance(m.get("chunks", []), list):
        raise EngineError("backup non valido: elenco pacchetti")
    if m.get("encryption") is not None and not isinstance(m["encryption"], dict):
        raise EngineError("backup non valido: cifratura")
    return m


def validate_chunk(c, m):
    """Ogni pacchetto deve stare esattamente nella sua posizione: nessuna scrittura fuori posto."""
    i = _ival(c.get("index"), 1, m["chunk_count"], "numero del pacchetto")
    off = (i - 1) * m["chunk_size"]
    length = min(m["chunk_size"], m["source"]["size"] - off)
    if c.get("offset") != off or c.get("length") != length:
        raise EngineError(f"backup non valido: posizione del pacchetto {i}")
    _ival(c.get("payload_size"), 0, m["chunk_size"] + m["chunk_size"] // 8 + 16 * MIB, "dimensione pacchetto")
    for k in ("raw_sha256", "payload_sha256"):
        if not isinstance(c.get(k), str) or not HEX64.fullmatch(c[k]):
            raise EngineError(f"backup non valido: checksum del pacchetto {i}")
    if c.get("sparse"):
        _ival(c.get("used", 0), 0, length, "zone usate")
    bad = c.get("bad_sectors", [])
    if not isinstance(bad, list) or len(bad) > 1_000_000:
        raise EngineError(f"backup non valido: settori illeggibili del pacchetto {i}")
    return c


def chunk_filename(name, index, digits=5, group=0):
    """Percorso relativo del pacchetto; con 'group' i file sono divisi in sottocartelle da 'group' pacchetti."""
    fname = f"{name}.part{index:0{digits}d}{CHUNK_EXT}"
    if group:
        return f"g{(index - 1) // group:05d}/{fname}"
    return fname


def manifest_layout(m):
    lay = m.get("layout") or {}
    return lay.get("digits", 5), lay.get("group", 0)


def index_filename(batch):
    return f"{INDEX_DIR}/index-{batch:05d}.json"


def compact_record(meta):
    rec = {"i": meta["index"], "s": meta["payload_size"], "r": meta["raw_sha256"]}
    if meta["payload_sha256"] != meta["raw_sha256"]:
        rec["p"] = meta["payload_sha256"]
    if meta.get("bad_sectors"):
        rec["b"] = meta["bad_sectors"]
    if meta.get("sparse"):
        rec["u"] = meta["used"]
        if meta.get("empty"):
            rec["e"] = 1
    return rec


def expand_record(m, rec):
    """Ricostruisce i dati completi di un pacchetto dal record compatto dell'indice."""
    if not isinstance(rec, dict):
        raise EngineError("backup non valido: indice")
    i = _ival(rec.get("i"), 1, m["chunk_count"], "indice")
    digits, group = manifest_layout(m)
    offset = (i - 1) * m["chunk_size"]
    c = {"index": i, "file": chunk_filename(m["name"], i, digits, group), "offset": offset,
         "length": min(m["chunk_size"], m["source"]["size"] - offset), "payload_size": rec["s"],
         "raw_sha256": rec["r"], "payload_sha256": rec.get("p", rec["r"]), "bad_sectors": rec.get("b", [])}
    if m.get("sparse"):
        c.update(sparse=True, used=rec.get("u", 0), empty=rec.get("e") == 1)
    return validate_chunk(c, m)


def parse_size_kib(text):
    """Dimensione pacchetti: numero in KB oppure con suffisso K, M, G (es. 1440K, 1.44M, 4G)."""
    t = str(text).strip().upper().replace(",", ".").rstrip("B").rstrip("I")
    mult = {"K": 1, "M": 1024, "G": 1024 * 1024}.get(t[-1:], None)
    try:
        return int(round(float(t[:-1]) * mult)) if mult else int(t)
    except ValueError:
        raise argparse.ArgumentTypeError(f"dimensione non valida: {text}")


BAD_LOG_LIMIT = 50              # settori illeggibili elencati uno a uno nel registro
SKIP_RUN = 64                   # in modalita' "salta": dopo tanti settori illeggibili di fila salta il resto del blocco


def add_bad(bad, start, length):
    """Aggiunge un intervallo illeggibile [start, start+length) alla lista compatta di intervalli."""
    if bad and bad[-1][0] + bad[-1][1] == start:
        bad[-1][1] += length
    else:
        bad.append([start, length])


def bad_bytes(bad):
    return sum(r[1] for r in bad if isinstance(r, list)) + sum(512 for r in bad if not isinstance(r, list))


def read_block(fd, mv, offset, sector, bad, rep, mode="retry"):
    """
    Legge un blocco. Se ci sono settori illeggibili:
      retry -> rilegge ogni settore fino a 5 volte e copia tutto cio' che riesce a leggere;
      skip  -> un solo tentativo per settore e salto rapido delle zone rovinate (meno stress per il disco).
    I settori illeggibili vengono salvati come zeri e annotati.
    """
    try:
        read_exact(fd, mv, offset)
        return
    except OSError as e:
        if e.errno not in BAD_READ_ERRNOS:
            raise
    attempts = 5 if mode == "retry" else 1
    run = 0
    for s in range(0, len(mv), sector):
        part = mv[s:s + sector]
        ok = False
        if not (mode == "skip" and run >= SKIP_RUN):
            for _ in range(attempts):
                try:
                    read_exact(fd, part, offset + s)
                    ok = True
                    break
                except OSError as e:
                    if e.errno not in BAD_READ_ERRNOS:
                        raise
        if ok:
            run = 0
            continue
        run += 1
        part[:] = bytes(len(part))
        add_bad(bad, offset + s, len(part))
        rep.bad_count = getattr(rep, "bad_count", 0) + 1
        if rep.bad_count <= BAD_LOG_LIMIT:
            rep.log(f"Settore illeggibile all'offset {offset + s}: salvato come zeri", "warning")
        elif rep.bad_count == BAD_LOG_LIMIT + 1:
            rep.log("Altri settori illeggibili: l'elenco completo e' nel manifest del backup.", "warning")


# ---------------------------------------------------------------------------
# Destinazioni: cloud (rclone, con passaggio in RAM) oppure disco/cartella locale (scrittura diretta)
# ---------------------------------------------------------------------------

def is_local_dest(remote):
    return os.path.isabs((remote or "").strip())


def json_bytes(data):
    return json.dumps(data, indent=1, ensure_ascii=False).encode()


class CloudStore:
    """Backup sul cloud tramite rclone: ogni pacchetto passa da una cartella in RAM accessibile solo a root."""
    local = False

    def __init__(self, rc, base, rep, retries):
        self.rc, self.base, self.rep, self.retries = rc, base, rep, retries
        self.ram = None
        self.require_trusted = False

    def where(self, *parts):
        return remote_join(self.base, *parts)

    def list_names(self, rel="", dirs=False):
        return self.rc.list_names(self.where(rel), dirs)

    def read(self, rel):
        return self.rc.cat(self.where(rel))

    def prepare(self):
        pass

    def write_json(self, rel, data, what):
        body = json_bytes(data)
        with_retries(self.rep, self.retries, what, lambda: self.rc.upload_bytes(body, self.where(rel)))

    def chunk_out(self, rel):
        return os.path.basename(rel)

    def create(self, token):
        return self.ram.create(token)

    def discard(self, token):
        self.ram.remove(token)

    def check_space(self, need):
        pass

    def commit_chunk(self, token, rel, size, on_progress, verify):
        remote_file = self.where(rel)
        fd = self.ram.open_read(token)
        try:
            for _ in range(2):
                with_retries(self.rep, self.retries, f"Upload di {rel}",
                             lambda: self.rc.upload_fd(fd, remote_file, on_progress))
                if not verify:                      # pacchetti piccoli: basta il controllo di rclone
                    break
                rsize = self.rc.remote_size(remote_file)
                if rsize is None or rsize == size:
                    break
                self.rep.log(f"Dimensione remota di {rel} errata ({rsize} invece di {size}): ricarico.", "warning")
            else:
                raise EngineError(f"il pacchetto {rel} non risulta caricato correttamente sul cloud")
        finally:
            os.close(fd)
        self.ram.remove(token)                      # libera la RAM: il pacchetto ora esiste solo sul cloud

    def fetch_chunk(self, rel, on_progress, max_bytes):
        token = os.path.basename(rel)
        fd = self.ram.create(token)
        try:
            with_retries(self.rep, self.retries, f"Download di {rel}",
                         lambda: self.rc.download_to_fd(self.where(rel), fd, max_bytes, on_progress))
        except BaseException:
            os.close(fd)
            self.ram.remove(token)
            raise
        return ChunkHandle(fd, lambda: self.ram.remove(token))

    def release(self, handle):
        handle.close()

    def delete_backup_files(self):
        self.rc.delete_backup_files(self.base)


class LocalStore:
    """
    Backup su un disco o in una cartella locale, scritto direttamente (senza RAM).
    Tutte le operazioni avvengono tramite descrittori di cartella aperti senza seguire link simbolici;
    i pacchetti restano di proprieta' dell'amministratore (l'utente li puo' leggere ma non alterare).
    """
    local = True

    def __init__(self, root, parts, owner, rep):
        self.root = os.path.realpath(root)              # cartella scelta dall'utente
        self.parts = [p for part in parts if part for p in part.strip("/").split("/") if p]
        for p in self.parts:
            if p in (".", ".."):
                raise EngineError("percorso del backup non valido")
        self.base = os.path.join(self.root, *self.parts)
        self.owner, self.rep = owner, rep
        self.require_trusted = False
        self._pending = {}
        self._warned = False

    def where(self, *parts):
        return os.path.join(self.base, *[p.strip("/") for p in parts if p and p.strip("/")])

    def _dir(self, rel="", create=False):
        rootfd = open_dir_nofollow(self.root, create, self.owner)
        try:
            return walk_dir(rootfd, self.parts + [p for p in rel.split("/") if p], create, self.owner)
        finally:
            os.close(rootfd)

    def list_names(self, rel="", dirs=False):
        try:
            dfd = self._dir(rel)
        except (FileNotFoundError, NotADirectoryError, EngineError):
            return []
        try:
            out = []
            for n in os.listdir(dfd):
                if n.startswith(".") or n.endswith((".partial", ".tmp")):
                    continue
                st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    continue
                if stat.S_ISDIR(st.st_mode) == dirs:
                    out.append(n)
            return sorted(out)
        finally:
            os.close(dfd)

    def read(self, rel, max_bytes=MAX_META):
        d, name = os.path.split(rel)
        try:
            dfd = self._dir(d)
        except FileNotFoundError:
            raise EngineError(f"impossibile leggere {self.where(rel)}: non trovato")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except OSError as e:
            raise EngineError(f"impossibile leggere {self.where(rel)}: {e.strerror}")
        finally:
            os.close(dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
                raise EngineError(f"{self.where(rel)}: file non valido o troppo grande")
            data = bytearray()
            while True:
                b = os.read(fd, MIB)
                if not b:
                    break
                data.extend(b)
                if len(data) > max_bytes:
                    raise EngineError(f"{self.where(rel)}: file troppo grande")
            return bytes(data)
        finally:
            os.close(fd)

    def prepare(self):
        os.close(self._dir(create=True))

    def _fsync_dir(self, dfd):
        try:
            os.fsync(dfd)
        except OSError:
            pass

    def write_json(self, rel, data, what):
        d, name = os.path.split(rel)
        dfd = self._dir(d, create=True)
        try:
            tmp = f".{name}.{os.getpid()}.tmp"
            fd = create_exclusive(tmp, dfd, 0o644)
            try:
                write_all(fd, json_bytes(data))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            self._fsync_dir(dfd)
        finally:
            os.close(dfd)

    def chunk_out(self, rel):
        return rel

    def create(self, token):
        d, name = os.path.split(token)
        dfd = self._dir(d, create=True)
        try:
            fd = create_exclusive(name + ".partial", dfd, 0o644)
        except BaseException:
            os.close(dfd)
            raise
        st = os.fstat(fd)
        self._pending[token] = (dfd, st.st_dev, st.st_ino)
        return fd

    def discard(self, token):
        entry = self._pending.pop(token, None)
        if entry:
            dfd = entry[0]
            try:
                os.unlink(os.path.basename(token) + ".partial", dir_fd=dfd)
            except OSError:
                pass
            os.close(dfd)

    def check_space(self, need):
        dfd = self._dir(create=True)
        try:
            st = os.fstatvfs(dfd)
        finally:
            os.close(dfd)
        free = st.f_bavail * st.f_frsize
        if free < need + 16 * MIB:
            raise EngineError(f"spazio esaurito sulla destinazione {self.base}: liberi {human(free)}, "
                              f"servono almeno {human(need)} per il prossimo pacchetto")

    def commit_chunk(self, token, rel, size, on_progress, verify):
        dfd, dev, ino = self._pending.pop(token)
        name = os.path.basename(rel)
        try:
            fd = os.open(name + ".partial", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            try:
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) != (dev, ino) or st.st_size != size:
                    raise EngineError(f"il file temporaneo di {rel} e' stato sostituito: backup interrotto")
                os.fsync(fd)                        # dati davvero sul disco prima di dichiarare il pacchetto salvato
            finally:
                os.close(fd)
            os.replace(name + ".partial", name, src_dir_fd=dfd, dst_dir_fd=dfd)
            self._fsync_dir(dfd)
        finally:
            os.close(dfd)
        if on_progress:
            on_progress(size, size, 0)

    def fetch_chunk(self, rel, on_progress, max_bytes):
        d, name = os.path.split(rel)
        try:
            dfd = self._dir(d)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            finally:
                os.close(dfd)
        except (OSError, EngineError) as e:
            raise EngineError(f"pacchetto mancante o non valido: {self.where(rel)} ({e})")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            os.close(fd)
            raise EngineError(f"pacchetto non valido: {self.where(rel)}")
        if on_progress:
            on_progress(st.st_size, st.st_size, 0)
        if st.st_uid == os.geteuid() and not st.st_mode & 0o022 and st.st_nlink == 1:
            return ChunkHandle(fd)              # nessun altro puo' modificarlo: si usa direttamente
        # file modificabile da altri (es. chiavetta FAT/exFAT): copia privata in memoria prima della verifica
        avail = mem_available()
        if avail and st.st_size + RAM_MARGIN < avail:
            mfd = os.memfd_create("bkhd-pacchetto", os.MFD_CLOEXEC)
            try:
                pos = 0
                while pos < st.st_size:
                    check_cancel()
                    data = os.pread(fd, IO_BLOCK, pos)
                    if not data:
                        break
                    write_all(mfd, data)
                    pos += len(data)
            except BaseException:
                os.close(mfd)
                raise
            finally:
                os.close(fd)
            return ChunkHandle(mfd)
        if self.require_trusted:
            os.close(fd)
            raise EngineError("il backup protetto si trova su un disco i cui file possono essere modificati da altri "
                              "programmi (es. FAT/exFAT) e la RAM non basta per una copia privata di verifica: "
                              "libera memoria o copia il backup su un disco Linux (ext4)")
        if not self._warned:
            self._warned = True
            self.rep.log("I pacchetti sono su un disco modificabile da altri programmi e la RAM non basta per una "
                         "copia privata: verranno letti direttamente.", "warning")
        return ChunkHandle(fd)

    def release(self, handle):
        handle.close()

    def delete_backup_files(self):
        def clean(dfd, top):
            for n in os.listdir(dfd):
                st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode) and top and (n == INDEX_DIR or re.fullmatch(r"g\d{5}", n)):
                    sub = os.open(n, O_NOFOLLOW_DIR, dir_fd=dfd)
                    try:
                        clean(sub, False)
                    finally:
                        os.close(sub)
                elif stat.S_ISREG(st.st_mode) and (n.endswith((CHUNK_EXT, CHUNK_EXT + ".partial"))
                                                   or n in (MANIFEST_NAME, USEDMAP_NAME)
                                                   or (n.startswith("index-") and n.endswith(".json"))):
                    os.unlink(n, dir_fd=dfd)

        try:
            dfd = self._dir()
        except FileNotFoundError:
            return
        try:
            clean(dfd, True)
        finally:
            os.close(dfd)


def open_store(remote, parts, rc, rep, retries, owner):
    if is_local_dest(remote):
        return LocalStore(remote.strip(), parts, owner, rep)
    if rc is None or not rc.available():
        raise EngineError("rclone non trovato: installalo con 'sudo apt install rclone'")
    return CloudStore(rc, remote_join(remote, *parts), rep, retries)


def block_names_of_path(path):
    """Dispositivi a blocchi (partizione, disco e dischi sottostanti) che contengono 'path'."""
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    try:
        st = os.stat(path)
    except OSError:
        return set()
    link = f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    if not os.path.exists(link):
        return set()
    names, todo = set(), [os.path.realpath(link)]
    while todo:
        real = todo.pop()
        name = os.path.basename(real)
        if name in names:
            continue
        names.add(name)
        parent = os.path.basename(os.path.dirname(real))
        if parent != "block" and os.path.exists(f"/sys/class/block/{parent}"):
            todo.append(os.path.realpath(f"/sys/class/block/{parent}"))
        slaves = f"/sys/class/block/{name}/slaves"
        if os.path.isdir(slaves):                # LVM, cifratura, RAID
            todo += [os.path.realpath(f"/sys/class/block/{s}") for s in os.listdir(slaves)]
    return names


def device_family(dev):
    """Il dispositivo e, se e' un disco intero, tutte le sue partizioni."""
    name = os.path.basename(os.path.realpath(dev))
    fam = {name}
    sysdir = f"/sys/class/block/{name}"
    if os.path.isdir(sysdir) and not os.path.exists(f"{sysdir}/partition"):
        fam |= {e for e in os.listdir(sysdir) if os.path.exists(f"{sysdir}/{e}/partition")}
    return fam


def check_not_on_device(dev, path, what):
    if not is_block(dev):
        return
    if device_family(dev) & block_names_of_path(path):
        raise EngineError(f"{what} ({path}) si trova sullo stesso disco di {dev}: scegli un altro disco")


# ---------------------------------------------------------------------------
# Mappa dei settori usati (modalita' "solo settori usati")
# ---------------------------------------------------------------------------

SPARSE_MIN_GAP = 1 * MIB            # buchi piu' piccoli vengono copiati comunque (meno frammentazione)
SPARSE_EDGE = 2 * MIB               # inizio e fine del disco sempre copiati (MBR, GPT e GPT di riserva)
SPARSE_GAP_KEEP = 16 * MIB          # spazio non partizionato piu' piccolo di cosi' viene copiato
EXT_TYPES = {"ext2", "ext3", "ext4"}
PARTCLONE_FS = {"ntfs": "ntfs", "vfat": "fat", "exfat": "exfat", "btrfs": "btrfs", "xfs": "xfs",
                "hfsplus": "hfsp", "f2fs": "f2fs", "ext2": "extfs", "ext3": "extfs", "ext4": "extfs"}
SPARSE_TABLE = struct.Struct("<QQ")


def merge_extents(ext, min_gap=SPARSE_MIN_GAP, align=4096, limit=None):
    out = []
    for start, length in sorted(ext):
        if length <= 0:
            continue
        s = start - start % align
        e = start + length
        e += (-e) % align
        if limit is not None:
            e = min(e, limit)
        if out and s - out[-1][1] < min_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e - s) for s, e in out]


def fs_type(path, offset=None):
    cmd = ["blkid", "-p", "-o", "value", "-s", "TYPE"]
    if offset:
        cmd += ["-O", str(offset)]
    try:
        r = subprocess.run(cmd + [path], capture_output=True, text=True, env=c_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


def ext_used_extents(spec):
    """Blocchi occupati di un file system ext2/3/4 secondo dumpe2fs (complemento dei blocchi liberi)."""
    r = subprocess.run(["dumpe2fs", spec], capture_output=True, text=True, env=c_env(), timeout=1800)
    if r.returncode != 0:
        raise EngineError("dumpe2fs: " + (r.stderr.strip().splitlines() or ["errore"])[-1])
    bsize = count = None
    free, in_groups = [], False
    for line in r.stdout.splitlines():
        if line.startswith("Block size:"):
            bsize = int(line.split(":")[1])
        elif line.startswith("Block count:"):
            count = int(line.split(":")[1])
        elif line.startswith("Group "):
            in_groups = True
        elif in_groups and line.strip().startswith("Free blocks:"):
            for part in line.split(":", 1)[1].split(","):
                part = part.strip()
                if part:
                    a, _, b = part.partition("-")
                    free.append((int(a), int(b or a)))
    if not bsize or not count:
        raise EngineError("dumpe2fs: formato non riconosciuto")
    used, cur = [], 0
    for a, b in sorted(free):
        if a > cur:
            used.append((cur * bsize, (a - cur) * bsize))
        cur = max(cur, b + 1)
    if cur < count:
        used.append((cur * bsize, (count - cur) * bsize))
    return used


def partclone_used_extents(fstype, dev):
    tool = shutil.which("partclone." + PARTCLONE_FS[fstype])
    if not tool:
        return None
    with tempfile.TemporaryDirectory(prefix="bkhd-map-") as tmp:
        mapfile, logfile = os.path.join(tmp, "dominio.map"), os.path.join(tmp, "partclone.log")
        r = subprocess.run([tool, "-D", "-s", dev, "-o", mapfile, "-L", logfile],
                           capture_output=True, text=True, env=c_env(), timeout=7200)
        if r.returncode != 0 or not os.path.exists(mapfile):
            raise EngineError(f"partclone.{PARTCLONE_FS[fstype]} non riuscito")
        used, first = [], True
        with open(mapfile) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if first:                       # riga "posizione stato" iniziale del formato ddrescue
                    first = False
                    if len(parts) < 3 or parts[2] not in "+-?*/":
                        continue
                if len(parts) >= 3 and parts[2] == "+":
                    used.append((int(parts[0], 0), int(parts[1], 0)))
        return used


def mounted_names():
    names = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev = line.split()[0]
                if dev.startswith("/dev/"):
                    names.add(os.path.basename(os.path.realpath(dev)))
    except OSError:
        pass
    return names


def source_used_extents(source, info, rep):
    """Zone del disco da copiare: tabella delle partizioni, spazio usato dei file system, il resto escluso."""
    total = info["size"]
    regions = []                                # (start, size, nodo o None, etichetta)
    lsblk_type = (info.get("lsblk") or {}).get("type")
    if lsblk_type != "part":
        try:
            r = subprocess.run(["sfdisk", "-J", source], capture_output=True, text=True, env=c_env(), timeout=60)
            pt = json.loads(r.stdout)["partitiontable"] if r.returncode == 0 else None
        except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            pt = None
        if pt:
            ssz = pt.get("sectorsize") or info.get("logical_sector") or 512
            for p in pt.get("partitions", []):
                node = p.get("node")
                regions.append((p["start"] * ssz, p["size"] * ssz,
                                node if info["kind"] == "block" and node and os.path.exists(node) else None,
                                os.path.basename(node or "partizione")))
    whole_fs = not regions
    if whole_fs:
        regions = [(0, total, source if info["kind"] == "block" else None, os.path.basename(source))]
    mounted = mounted_names()
    extents, details = [], []
    for start, size, node, label in sorted(regions):
        if node:
            fstype = fs_type(node)
        else:
            fstype = fs_type(source, start or None)
        used, how = None, "copiata per intero"
        is_mounted = node and os.path.basename(os.path.realpath(node)) in mounted
        try:
            if is_mounted and fstype not in (None, "swap"):
                how = "montata: copiata per intero per sicurezza"
            elif fstype in EXT_TYPES:
                used = ext_used_extents(node or (source if not start else f"{source}?offset={start}"))
                how = "dumpe2fs"
            elif fstype == "swap":
                used, how = [(0, min(size, 64 * KIB))], "swap: solo intestazione"
            elif fstype in PARTCLONE_FS and node:
                used = partclone_used_extents(fstype, node)
                how = "partclone" if used is not None else "copiata per intero (installa partclone)"
            elif fstype in PARTCLONE_FS:
                how = "copiata per intero (file system dentro un'immagine)"
            elif fstype:
                how = f"copiata per intero (tipo {fstype} non analizzabile)"
        except (EngineError, OSError, subprocess.TimeoutExpired) as e:
            used, how = None, f"copiata per intero ({e})"
        if used is None:
            used = [(0, size)]
        part_ext = [(start + s, min(ln, size - s)) for s, ln in used if s < size]
        used_bytes = sum(ln for _, ln in part_ext)
        extents += part_ext
        details.append({"label": label, "start": start, "size": size, "fstype": fstype, "method": how,
                        "used": used_bytes})
        rep.log(f"{label} ({fstype or 'sconosciuto'}): da copiare {human(used_bytes)} su {human(size)} — {how}")
    if not whole_fs:                           # spazio fuori dalle partizioni
        cur = 0
        for start, size, _, _ in sorted(regions) + [(total, 0, None, None)]:
            if start > cur:
                gap = (cur, start - cur)
                if gap[1] < SPARSE_GAP_KEEP:
                    extents.append(gap)
                else:
                    a, b = gap[0], gap[0] + gap[1]
                    if a < SPARSE_EDGE:
                        extents.append((a, min(b, SPARSE_EDGE) - a))
                    if b > total - SPARSE_EDGE:
                        s = max(a, total - SPARSE_EDGE)
                        extents.append((s, b - s))
            cur = max(cur, start + size)
    merged = merge_extents(extents, limit=total)
    return merged, details


def validate_usedmap(usedmap, total):
    ext = usedmap.get("extents") if isinstance(usedmap, dict) else None
    if not isinstance(ext, list) or len(ext) > 50_000_000:
        raise EngineError("mappa dei settori usati non valida")
    for e in ext:
        if (not isinstance(e, list) or len(e) != 2 or type(e[0]) is not int or type(e[1]) is not int
                or e[0] < 0 or e[1] < 0 or e[0] + e[1] > total):
            raise EngineError("mappa dei settori usati non valida")


def chunk_extents(extents, starts, offset, length):
    """Parti delle zone usate che cadono nel pacchetto [offset, offset+length), relative al pacchetto."""
    out = []
    i = max(bisect.bisect_right(starts, offset) - 1, 0)
    end = offset + length
    while i < len(extents):
        s, ln = extents[i]
        if s >= end:
            break
        a, b = max(s, offset), min(s + ln, end)
        if b > a:
            out.append((a - offset, b - a))
        i += 1
    return out


# ---------------------------------------------------------------------------
# BACKUP
# ---------------------------------------------------------------------------

def cmd_backup(args, rep):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", args.name):
        raise EngineError("nome del backup non valido (usa lettere, numeri, . _ -)")
    chunk_kib = args.chunk_size_kib if args.chunk_size_kib else args.chunk_size_mib * 1024
    if not (MIN_CHUNK_KIB <= chunk_kib <= MAX_CHUNK_KIB):
        raise EngineError("la dimensione dei pacchetti deve essere tra 1,2 MB (floppy) e 8 GB")
    if args.compression == "zstd" and not shutil.which("zstd"):
        raise EngineError("zstd non installato: 'sudo apt install zstd' oppure disattiva la compressione")

    info = device_info(args.source)
    total = info["size"]
    if total == 0:
        raise EngineError("la sorgente ha dimensione zero")
    sector = max(info["logical_sector"], 512)
    chunk_size = chunk_kib * KIB
    chunk_size -= chunk_size % sector
    if chunk_size <= 0:
        raise EngineError("dimensione dei pacchetti piu' piccola del settore del disco")
    nchunks = (total + chunk_size - 1) // chunk_size
    quiet = nchunks > QUIET_THRESHOLD

    if info["kind"] == "block":
        busy = block_in_use(args.source)
        if busy:
            rep.log("La sorgente e' in uso (" + "; ".join(busy) + "). Il backup proseguira', ma per una copia "
                    "coerente e' consigliato avviare il PC da una chiavetta live.", "warning")

    if args.encrypt and not crypto_available():
        raise EngineError("per la protezione con password serve python3-cryptography "
                          "('sudo apt install python3-cryptography')")
    owner = resolve_run_as(args.run_as)
    rc = None if is_local_dest(args.remote) else Rclone(args.rclone, args.rclone_config, owner)
    store = open_store(args.remote, [args.path, args.name], rc, rep, args.retries, owner)
    dest = store.where()
    if store.local:
        check_not_on_device(args.source, dest, "La cartella di destinazione")
        need = 0
    else:
        need = min(chunk_size, total) + HEADER_SIZE + TMPFS_SLACK
        avail = mem_available()
        if avail and avail < need + RAM_MARGIN:
            raise EngineError(f"RAM insufficiente: servono {human(need + RAM_MARGIN)}, disponibili "
                              f"{human(avail)}. Riduci la dimensione dei pacchetti.")

    rep.log(f"Sorgente: {args.source} ({human(total)}, settore {sector} B) -> {dest}"
            + (" (scrittura diretta su disco)" if store.local else ""))
    rep.log(f"Pacchetti: {nchunks} da {human(chunk_size)}, compressione: {args.compression}"
            + (", solo settori usati" if args.used_only else ""))
    if nchunks > 50000:
        rep.log(f"Con {nchunks} pacchetti ogni pacchetto e' un trasferimento separato: il backup sara' molto piu' "
                "lento che con pacchetti grandi.", "warning")

    manifest = None
    start = 0
    converted = False
    usedmap = None
    crypto = None
    if MANIFEST_NAME in store.list_names():
        old = load_manifest(store)
        if args.overwrite:
            rep.log("Backup esistente con lo stesso nome: verra' sovrascritto.", "warning")
            store.delete_backup_files()
        elif old.get("status") == "complete":
            raise EngineError(f"esiste gia' un backup completo chiamato '{args.name}'. Scegli un altro nome.")
        elif not args.resume:
            raise EngineError(f"esiste un backup interrotto chiamato '{args.name}': attiva 'Riprendi' "
                              "per continuarlo oppure scegli un altro nome.")
        else:
            same = (old.get("source", {}).get("size") == total and old.get("chunk_size") == chunk_size
                    and old.get("compression") == args.compression
                    and bool(old.get("sparse")) == bool(args.used_only))
            if not same:
                raise EngineError("il backup interrotto non corrisponde (disco, dimensione pacchetti, "
                                  "compressione o modalita' 'solo settori usati' diversi): impossibile riprenderlo")
            if old.get("encryption"):
                if not crypto_available():
                    raise EngineError("il backup e' protetto da password: serve python3-cryptography")
                crypto = Crypto.open(read_password(args), old["encryption"])
            elif args.encrypt:
                raise EngineError("il backup interrotto non e' protetto da password: riprendilo senza password")
            if crypto:
                crypto.verify(old, "Manifest")
            elif args.expect_encrypted:
                raise EngineError("il backup risulta NON protetto da password ma era stato creato protetto: "
                                  "possibile manomissione, ripresa annullata")
            done = load_chunks(store, old, crypto)
            converted = old.get("version", 1) < 2
            manifest = upgrade_manifest(old)
            if manifest.get("sparse"):
                usedmap = json.loads(store.read(USEDMAP_NAME).decode())
                if crypto:
                    crypto.verify(usedmap, "Mappa dei settori usati")
                validate_usedmap(usedmap, total)
            start = len(done)
            rep.log(f"Ripresa del backup interrotto dal pacchetto {start + 1}/{nchunks}.")

    if manifest is None and args.encrypt:
        pw = read_password(args, confirm=True)
        if not pw or len(pw) < MIN_PASSWORD:
            raise EngineError(f"la password deve avere almeno {MIN_PASSWORD} caratteri")
        rep.log("Calcolo della chiave di cifratura dalla password...")
        crypto = Crypto.create(pw)
        rep.log("Backup protetto da password (AES-256-GCM): senza la password non sara' possibile ripristinarlo.")
    if args.used_only and usedmap is None:
        rep.log("Analisi dei file system per individuare i settori effettivamente usati...")
        extents, details = source_used_extents(args.source, info, rep)
        usedmap = {"extents": extents, "used_bytes": sum(ln for _, ln in extents), "partitions": details}
        rep.log(f"Da copiare: {human(usedmap['used_bytes'])} su {human(total)} "
                f"({100 * usedmap['used_bytes'] / total:.1f}%); lo spazio libero viene saltato.")
    extents = [tuple(e) for e in usedmap["extents"]] if usedmap else None
    starts = [e[0] for e in extents] if extents else None

    if manifest is None:
        done = []
        manifest = {
            "format": "bkhd-manifest", "version": 2, "engine": VERSION,
            "backup_id": str(uuid.uuid4()), "name": args.name, "status": "in_progress",
            "created": now_iso(), "updated": now_iso(), "finished": None,
            "host": socket.gethostname(), "source": info,
            "chunk_size": chunk_size, "chunk_count": nchunks,
            "compression": args.compression,
            "compression_level": args.level if args.compression == "zstd" else None,
            "bad_sectors_total": 0, "chunks_done": 0, "stored_bytes": 0,
            "index_batch": INDEX_BATCH,
            "layout": {"digits": max(5, len(str(nchunks))),
                       "group": 1000 if nchunks > GROUP_THRESHOLD else 0},
            "sparse": bool(extents is not None),
            "encryption": crypto.meta if crypto else None,
            "bad_sector_mode": args.bad_sectors,
            "used_bytes": usedmap["used_bytes"] if usedmap else total,
            "destination": "local" if store.local else "cloud",
        }
    manifest["chunks_done"] = len(done)
    manifest["stored_bytes"] = sum(stored_size(c) for c in done)
    manifest["bad_sectors_total"] = sum(bad_bytes(c["bad_sectors"]) for c in done) // sector
    # record del gruppo di indice ancora aperto (gli altri sono gia' completi sulla destinazione)
    batch_records = [compact_record(c) for c in done[(len(done) // INDEX_BATCH) * INDEX_BATCH:]]

    rep.emit("start", mode="backup", total=total, chunks=nchunks, chunk_size=chunk_size, dest=dest,
             first=start + 1, backup_id=manifest["backup_id"], encrypted=bool(crypto))
    rep.bad_count = 0

    src_fd = os.open(args.source, os.O_RDONLY)
    ramctx = contextlib.nullcontext() if store.local else RamStore(need, rep)
    try:
        try:
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass
        with SleepInhibitor("Backup del disco in corso"), ramctx as ram:
            store.ram = ram
            store.prepare()

            def signed(obj):
                if crypto:
                    obj["mac"] = crypto.sign(obj)
                return obj

            def save_manifest():
                manifest["updated"] = now_iso()
                store.write_json(MANIFEST_NAME, signed(manifest), "Salvataggio del manifest")

            def save_state(batch):
                """Salva l'indice del gruppo 'batch' e poi il manifest."""
                if batch_records:
                    store.write_json(index_filename(batch), signed({"batch": batch, "chunks": batch_records}),
                                     "Salvataggio dell'indice")
                save_manifest()

            if start == 0 and usedmap is not None:
                store.write_json(USEDMAP_NAME, signed(usedmap), "Salvataggio della mappa dei settori usati")
            if converted:             # backup della versione 1.0: crea i file indice dei pacchetti gia' salvati
                for b in range(len(done) // INDEX_BATCH):
                    store.write_json(index_filename(b), signed({"batch": b, "chunks": [
                        compact_record(c) for c in done[b * INDEX_BATCH:(b + 1) * INDEX_BATCH]]}),
                        "Salvataggio dell'indice")
                save_state(len(done) // INDEX_BATCH)
            elif start == 0:
                save_state(0)
            t_start = last_save = last_log = time.monotonic()
            where = "su disco" if store.local else "sul cloud e rimosso dalla RAM"
            unsaved = False
            for i in range(start, nchunks):
                try:
                    check_cancel()
                    meta = backup_chunk(i, nchunks, src_fd, info, chunk_size, total, store, manifest, args, rep,
                                        quiet, extents, starts, crypto)
                except Cancelled:
                    if unsaved:         # annullamento: salva l'indice, la ripresa partira' dall'ultimo pacchetto
                        CANCEL.clear()
                        try:
                            save_state((i - 1) // INDEX_BATCH)
                            rep.log(f"Indice salvato: la ripresa partira' dal pacchetto {i + 1}.")
                        except (EngineError, OSError):
                            pass
                        finally:
                            CANCEL.set()
                    raise
                unsaved = True
                batch = i // INDEX_BATCH
                batch_records.append(compact_record(meta))
                manifest["chunks_done"] = i + 1
                manifest["stored_bytes"] += stored_size(meta)
                manifest["bad_sectors_total"] += bad_bytes(meta["bad_sectors"]) // sector
                now = time.monotonic()
                batch_full = (i + 1) % INDEX_BATCH == 0
                # pacchetti grandi: salva sempre (un'interruzione non deve far ricaricare GB di dati);
                # pacchetti piccoli: al massimo ogni SAVE_INTERVAL secondi
                if (batch_full or chunk_size >= VERIFY_MIN_SIZE or now - last_save >= SAVE_INTERVAL
                        or i + 1 == nchunks):
                    save_state(batch)
                    last_save = time.monotonic()
                    unsaved = False
                    if batch_full:
                        batch_records.clear()
                done_bytes = (i + 1) * chunk_size
                elapsed = now - t_start
                if meta.get("empty"):
                    msg = f"Pacchetto {i + 1}/{nchunks}: solo spazio libero, nessun dato da salvare."
                else:
                    msg = f"Pacchetto {i + 1}/{nchunks} salvato {where}."
                if quiet:
                    if now - last_log < 60 and i + 1 != nchunks:
                        msg = None
                    else:
                        last_log = now
                        msg = (f"{i + 1}/{nchunks} pacchetti salvati ({human(manifest['stored_bytes'])} "
                               "scritti sulla destinazione).")
                rep.emit("chunk_done", chunk=i + 1, chunks=nchunks, msg=msg,
                         overall=min(done_bytes, total) / total,
                         eta=elapsed / (i + 1 - start) * (nchunks - i - 1))
            manifest["status"] = "complete"
            manifest["finished"] = now_iso()
            save_manifest()
    finally:
        os.close(src_fd)

    msg = f"Backup completato: {nchunks} pacchetti in {dest} ({human(manifest['stored_bytes'])} occupati)"
    if manifest["bad_sectors_total"]:
        msg += f" ({manifest['bad_sectors_total']} settori illeggibili salvati come zeri)"
    rep.emit("done", ok=True, msg=msg)


def stored_size(c):
    return 0 if c.get("empty") else c["payload_size"] + HEADER_SIZE


def backup_chunk(i, nchunks, src_fd, info, chunk_size, total, store, manifest, args, rep, quiet=False,
                 extents=None, starts=None, crypto=None):
    index = i + 1
    offset = i * chunk_size
    length = min(chunk_size, total - offset)
    digits, group = manifest_layout(manifest)
    fname = chunk_filename(args.name, index, digits, group)
    sector = max(info["logical_sector"], 512)
    sparse = extents is not None
    parts = chunk_extents(extents, starts, offset, length) if sparse else [(0, length)]
    used = sum(ln for _, ln in parts)
    bad = []

    if sparse and used == 0:            # pacchetto fatto solo di spazio libero: nessun file da salvare
        h = new_stream_hash(crypto)
        h.update(struct.pack("<Q", 0))
        empty_sha = h.hexdigest()
        return {"index": index, "file": fname, "offset": offset, "length": length, "payload_size": 0,
                "raw_sha256": empty_sha, "payload_sha256": empty_sha, "bad_sectors": [], "sparse": True,
                "used": 0, "empty": True}

    def ctx(frac_phase, weight_start, weight):
        within = weight_start + weight * frac_phase
        return {"chunk": index, "chunks": nchunks,
                "overall": min((offset + length * within) / total, 1.0)}

    store.check_space(used + HEADER_SIZE + (16 * len(parts) + 8 if sparse else 0))
    label = (f"lettura settori {offset // sector}-{(offset + length) // sector - 1}"
             + (" e scrittura sulla destinazione" if store.local else " in RAM"))
    if sparse:
        label += f" (usati {human(used)} su {human(length)})"
    rep.emit("phase", phase="read", chunk=index, chunks=nchunks, label=label)
    local = store.chunk_out(fname)
    out = store.create(local)
    zproc = None
    pump = None
    pump_err = []
    try:
        write_all(out, bytes(HEADER_SIZE))
        raw_hash = new_stream_hash(crypto)
        nonce = os.urandom(12) if crypto else None
        enc = crypto.encryptor(nonce, Crypto.aad(manifest["backup_id"], index, offset, length, args.compression,
                                                 sparse)) if crypto else None

        def file_write(b):
            write_all(out, enc.update(b) if enc else b)

        if args.compression == "zstd":
            zproc = subprocess.Popen(["zstd", "-q", "-c", f"-{args.level}", "-T0"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE if enc else out)
            if enc:                     # i dati compressi vengono cifrati prima di arrivare sul file

                def pump_loop():
                    try:
                        for data in iter(lambda: zproc.stdout.read(MIB), b""):
                            file_write(data)
                    except BaseException as e:      # riportato al thread principale
                        pump_err.append(e)

                pump = threading.Thread(target=pump_loop, daemon=True)
                pump.start()
            write_out = zproc.stdin.write
        else:
            write_out = file_write

        def sink(b):
            raw_hash.update(b)
            write_out(b)

        if sparse:                      # flusso: numero di zone, tabella (inizio, lunghezza), dati delle zone
            sink(struct.pack("<Q", len(parts)))
            sink(b"".join(SPARSE_TABLE.pack(s, ln) for s, ln in parts))
        buf = bytearray(IO_BLOCK)
        mv = memoryview(buf)
        done = 0
        t0 = time.monotonic()
        for rel, ln in parts:
            pos = 0
            while pos < ln:
                check_cancel()
                n = min(IO_BLOCK, ln - pos)
                block = mv[:n]
                read_block(src_fd, block, offset + rel + pos, sector, bad, rep, args.bad_sectors)
                sink(block)
                try:
                    os.posix_fadvise(src_fd, offset + rel + pos, n, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
                pos += n
                done += n
                rep.progress(phase="read", frac=done / used, speed=done / max(time.monotonic() - t0, 1e-6),
                             **ctx(done / used, 0.0, 0.25))
        if zproc:
            zproc.stdin.close()
            if pump:
                pump.join()
                if pump_err:
                    raise EngineError(f"cifratura non riuscita: {pump_err[0]}")
            if zproc.wait() != 0:
                raise EngineError("compressione zstd non riuscita")
            zproc = None
        if enc:
            write_all(out, enc.finalize())
        payload_size = os.lseek(out, 0, os.SEEK_END) - HEADER_SIZE
        raw_sha = raw_hash.hexdigest()
        if args.compression == "zstd" or crypto:
            payload_sha = sha256_of_fd(out, HEADER_SIZE)
        else:
            payload_sha = raw_sha
        meta = {"index": index, "file": fname, "offset": offset, "length": length,
                "payload_size": payload_size, "raw_sha256": raw_sha, "payload_sha256": payload_sha,
                "bad_sectors": bad}
        if sparse:
            meta.update(sparse=True, used=used)
        header = dict(meta, format="bkhd", version=1, backup_id=manifest["backup_id"], name=args.name,
                      chunks=nchunks, compression=args.compression, source_size=total, created=now_iso())
        header.pop("bad_sectors")
        header["bad_sector_count"] = bad_bytes(bad) // sector
        if enc:
            header.update(encrypted=True, nonce=nonce.hex(), tag=enc.tag.hex())
        os.pwrite(out, build_header(header), 0)
    except BaseException:
        if zproc:
            zproc.kill()
            zproc.wait()
            zproc = None
        if pump:
            pump.join(5)
        os.close(out)
        out = None
        store.discard(local)
        raise
    finally:
        if out is not None:
            os.close(out)

    file_size = HEADER_SIZE + payload_size
    if args.compression == "zstd" and not quiet:
        rep.log(f"Pacchetto {index}: {human(used)} di disco -> {human(file_size)} compressi")

    # 2) salvataggio sulla destinazione (attende la fine dell'upload)
    rep.emit("phase", phase="upload" if not store.local else "write", chunk=index, chunks=nchunks,
             label=(f"scrittura di {fname} ({human(file_size)})" if store.local
                    else f"upload di {fname} ({human(file_size)})"))
    t0 = time.monotonic()

    def on_progress(done_b, tot, speed):
        frac = done_b / tot if tot else 0.0
        rep.progress(phase="upload", frac=frac, speed=speed or done_b / max(time.monotonic() - t0, 1e-6),
                     **ctx(frac, 0.25, 0.75))

    store.commit_chunk(local, fname, file_size, on_progress, chunk_size >= VERIFY_MIN_SIZE)
    rep.progress(force=True, phase="upload", frac=1.0, speed=0, **ctx(1.0, 0.25, 0.75))
    return meta


# ---------------------------------------------------------------------------
# RIPRISTINO / VERIFICA
# ---------------------------------------------------------------------------

def load_manifest(store, rel=""):
    try:
        m = json.loads(store.read(remote_join(rel, MANIFEST_NAME) if rel else MANIFEST_NAME).decode())
    except (ValueError, UnicodeDecodeError):
        raise EngineError("manifest non leggibile")
    return validate_manifest(m)


def load_chunks(store, m, crypto=None):
    """Pacchetti salvati (contigui dal primo), dal manifest v1 o dai file indice v2 (firmati se protetti)."""
    if m.get("version", 1) < 2:
        chunks = [validate_chunk(dict(c, bad_sectors=c.get("bad_sectors", [])), m)
                  for c in m.get("chunks", []) if isinstance(c, dict)]
    else:
        records = {}
        for name in sorted(store.list_names(INDEX_DIR)):
            if not (name.startswith("index-") and name.endswith(".json")):
                continue
            try:
                data = json.loads(store.read(f"{INDEX_DIR}/{name}").decode())
            except (ValueError, UnicodeDecodeError):
                raise EngineError(f"indice {name} non leggibile")
            if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
                raise EngineError(f"indice {name} non valido")
            if crypto:
                crypto.verify(data, f"Indice {name}")
            for rec in data["chunks"]:
                if isinstance(rec, dict) and type(rec.get("i")) is int:
                    records[rec["i"]] = rec
        chunks = []
        while len(chunks) + 1 in records:
            chunks.append(expand_record(m, records[len(chunks) + 1]))
    out = []
    for c in chunks:                       # solo la parte contigua dal pacchetto 1
        if c["index"] != len(out) + 1:
            break
        out.append(c)
    return out


def upgrade_manifest(m):
    """Converte un manifest v1 (pacchetti nel manifest) nel formato v2 (pacchetti nei file indice)."""
    if m.get("version", 1) >= 2:
        return m
    m = {k: v for k, v in m.items() if k != "chunks"}
    m.update(version=2, index_batch=INDEX_BATCH, layout={"digits": 5, "group": 0}, engine=VERSION)
    return m


def cmd_restore(args, rep):
    owner = resolve_run_as(args.run_as)
    rc = None if is_local_dest(args.remote) else Rclone(args.rclone, args.rclone_config, owner)
    store = open_store(args.remote, [args.path], rc, rep, args.retries, owner)
    manifest = load_manifest(store)
    total = manifest["source"]["size"]
    if manifest["compression"] == "zstd" and not shutil.which("zstd"):
        raise EngineError("zstd non installato: 'sudo apt install zstd'")
    crypto = None
    if manifest.get("encryption"):
        if not crypto_available():
            raise EngineError("il backup e' protetto da password: serve python3-cryptography")
        crypto = Crypto.open(read_password(args), manifest["encryption"])
        crypto.verify(manifest, "Manifest")
        rep.log("Password corretta: i pacchetti verranno decifrati e verificati.")
    elif args.expect_encrypted:
        raise EngineError("il backup risulta NON protetto da password ma era stato creato protetto: "
                          "possibile manomissione, ripristino annullato")
    store.require_trusted = bool(crypto)
    chunks = load_chunks(store, manifest, crypto)
    if manifest.get("status") == "complete" and len(chunks) != manifest["chunk_count"]:
        raise EngineError(f"indice del backup incompleto: trovati {len(chunks)} pacchetti su "
                          f"{manifest['chunk_count']}")
    if manifest.get("status") != "complete":
        if not args.allow_incomplete:
            raise EngineError("il backup e' incompleto (interrotto). Usa --allow-incomplete per ripristinare "
                              f"solo i {len(chunks)} pacchetti disponibili.")
        rep.log(f"Backup incompleto: verranno ripristinati solo {len(chunks)} pacchetti.", "warning")
    first = max(args.from_chunk, 1)
    todo = [c for c in chunks if c["index"] >= first]
    verify_only = args.verify_only

    target_fd = None
    target_kind = None
    if not verify_only:
        if not args.target:
            raise EngineError("specifica la destinazione (--target) oppure --verify-only")
        if not args.yes:
            raise EngineError("il ripristino cancella la destinazione: conferma con --yes")
        if os.path.exists(args.target) and is_block(args.target):
            target_kind = "block"
            if store.local:
                check_not_on_device(args.target, store.where(), "Il backup da ripristinare")
            busy = block_in_use(args.target)
            if busy:
                raise EngineError("la destinazione e' in uso: " + "; ".join(busy) +
                                  ". Smonta le partizioni (o avvia da chiavetta live) e riprova.")
            try:
                target_fd = os.open(args.target, os.O_WRONLY | os.O_EXCL)
            except OSError as e:
                raise EngineError(f"impossibile aprire {args.target} in modo esclusivo: {e.strerror}")
            tsize = os.lseek(target_fd, 0, os.SEEK_END)
            if tsize < total:
                os.close(target_fd)
                raise EngineError(f"il disco di destinazione ({human(tsize)}) e' piu' piccolo del backup "
                                  f"({human(total)})")
            if tsize > total:
                rep.log(f"La destinazione e' piu' grande del disco originale ({human(tsize)} > {human(total)}): "
                        "lo spazio in eccesso restera' non allocato.", "warning")
        else:
            target_kind = "file"
            target_fd = open_user_file(args.target, owner)
            if first == 1:
                os.ftruncate(target_fd, 0)
            os.ftruncate(target_fd, total)
        if manifest.get("sparse"):
            rep.log("Backup 'solo settori usati': vengono scritti solo i settori occupati; lo spazio libero dei "
                    "file system sulla destinazione non viene sovrascritto.")

    need = 0
    if not store.local:
        max_payload = max([c["payload_size"] for c in todo] or [0])
        need = max_payload + HEADER_SIZE + TMPFS_SLACK
        avail = mem_available()
        if avail and avail < need + RAM_MARGIN:
            raise EngineError(f"RAM insufficiente: servono {human(need + RAM_MARGIN)}, disponibili {human(avail)}")

    mode = "verify" if verify_only else "restore"
    rep.log(f"Backup '{manifest['name']}' del {manifest['created']} ({human(total)}, {len(chunks)} pacchetti) "
            + ("- solo verifica" if verify_only else f"-> {args.target}"))
    rep.emit("start", mode=mode, total=total, chunks=manifest["chunk_count"], chunk_size=manifest["chunk_size"],
             dest=args.target or "", first=first)

    ramctx = contextlib.nullcontext() if store.local else RamStore(need, rep)
    try:
        with SleepInhibitor("Ripristino del disco in corso"), ramctx as ram:
            store.ram = ram
            quiet = len(todo) > QUIET_THRESHOLD
            last_log = time.monotonic()
            verb = "verificati" if verify_only else "ripristinati"
            for n, c in enumerate(todo, 1):
                check_cancel()
                restore_chunk(c, manifest, store, target_fd, target_kind, rep, crypto)
                if c.get("empty"):
                    msg = f"Pacchetto {c['index']}/{manifest['chunk_count']}: solo spazio libero."
                else:
                    msg = (f"Pacchetto {c['index']}/{manifest['chunk_count']} "
                           + ("verificato" if verify_only else "ripristinato")
                           + ("." if store.local else " e rimosso dalla RAM."))
                if quiet:
                    msg = None
                    if time.monotonic() - last_log >= 60 or n == len(todo):
                        last_log = time.monotonic()
                        msg = f"{c['index']}/{manifest['chunk_count']} pacchetti {verb}."
                rep.emit("chunk_done", chunk=c["index"], chunks=manifest["chunk_count"], msg=msg,
                         overall=min(c["offset"] + c["length"], total) / total)
        if target_fd is not None:
            os.fsync(target_fd)
            if target_kind == "block":
                try:
                    fcntl.ioctl(target_fd, BLKRRPART)
                    rep.log("Tabella delle partizioni ricaricata.")
                except OSError:
                    rep.log("Riavvia o esegui 'partprobe' per rileggere la tabella delle partizioni.")
                if os.lseek(target_fd, 0, os.SEEK_END) > total and manifest["source"].get("pttype") == "gpt":
                    rep.log("Disco GPT piu' grande dell'originale: puoi spostare l'intestazione GPT di backup "
                            "alla fine del disco con 'sudo sgdisk -e " + args.target + "'.")
    finally:
        if target_fd is not None:
            os.close(target_fd)

    if verify_only:
        rep.emit("done", ok=True, msg=f"Verifica completata: tutti i {len(todo)} pacchetti sono integri.")
    else:
        rep.emit("done", ok=True, msg=f"Ripristino completato su {args.target}.")


class _Decrypting:
    """Lettore che decifra il contenuto del pacchetto (AES-GCM e' un cifrario a flusso: stessa lunghezza)."""

    def __init__(self, f, dec):
        self.f, self.dec, self.done = f, dec, False

    def read(self, n):
        data = self.f.read(n)
        if data:
            return self.dec.update(data)
        if not self.done:
            self.done = True
            self.dec.finalize()             # controlla il sigillo di autenticita' (tag)
        return b""


class ChunkStream:
    """Legge (decifra, decomprime) i dati di un pacchetto calcolandone il checksum."""

    def __init__(self, fd, compression, crypto=None, hdr=None, aad=None):
        self.hash = new_stream_hash(crypto)
        self.count = 0
        self.proc = None
        self.feeder = None
        self.errors = []
        self.f = os.fdopen(os.dup(fd), "rb")
        self.f.seek(HEADER_SIZE)
        plain = self.f
        if crypto:
            plain = _Decrypting(self.f, crypto.decryptor(bytes.fromhex(hdr["nonce"]),
                                                         bytes.fromhex(hdr["tag"]), aad))
        if compression == "zstd":
            if crypto:
                self.proc = subprocess.Popen(["zstd", "-d", "-c", "-q"], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE)

                def feed():
                    try:
                        for data in iter(lambda: plain.read(MIB), b""):
                            self.proc.stdin.write(data)
                    except BaseException as e:
                        self.errors.append(e)
                    finally:
                        try:
                            self.proc.stdin.close()
                        except OSError:
                            pass

                self.feeder = threading.Thread(target=feed, daemon=True)
                self.feeder.start()
            else:
                self.proc = subprocess.Popen(["zstd", "-d", "-c", "-q"], stdin=self.f, stdout=subprocess.PIPE)
            self.src = self.proc.stdout
        else:
            self.src = plain

    def read(self, n):
        data = self.src.read(n)
        self.hash.update(data)
        self.count += len(data)
        return data

    def read_exact(self, n, what):
        data = self.read(n)
        if len(data) != n:
            raise EngineError(f"pacchetto troncato ({what})")
        return data

    def close(self, check=True):
        ok = True
        if self.proc:
            if not check:
                self.proc.kill()
            if self.feeder:
                self.feeder.join(30)
            ok = self.proc.wait() == 0
        self.f.close()
        if check and self.errors:
            raise EngineError(f"decifratura non riuscita: {self.errors[0] or 'sigillo non valido'}")
        if check and not ok:
            raise EngineError("decompressione non riuscita")


def gcm_check(fd, crypto, hdr, aad):
    """Verifica il sigillo di autenticita' del pacchetto cifrato senza scrivere nulla."""
    try:
        dec = crypto.decryptor(bytes.fromhex(hdr["nonce"]), bytes.fromhex(hdr["tag"]), aad)
        pos = HEADER_SIZE
        while True:
            check_cancel()
            data = os.pread(fd, IO_BLOCK, pos)
            if not data:
                break
            dec.update(data)
            pos += len(data)
        dec.finalize()
        return True
    except (KeyError, ValueError):
        return False
    except Exception as e:                  # InvalidTag
        if type(e).__name__ == "InvalidTag":
            return False
        raise


def restore_chunk(c, manifest, store, target_fd, target_kind, rep, crypto=None):
    index = c["index"]
    nchunks = manifest["chunk_count"]
    total = manifest["source"]["size"]
    if c.get("empty"):                  # solo spazio libero: niente da scaricare ne' da scrivere
        return
    fname = c["file"]
    file_size = HEADER_SIZE + c["payload_size"]
    sparse = c.get("sparse", False)
    dl_w, ver_w, wr_w = (0.6, 0.1, 0.3) if not store.local else (0.0, 0.3, 0.7)

    def ctx(within):
        return {"chunk": index, "chunks": nchunks,
                "overall": min((c["offset"] + c["length"] * within) / total, 1.0)}

    # 1) destinazione -> RAM (per il cloud) e verifica dell'integrita'
    if not store.local:
        rep.emit("phase", phase="download", chunk=index, chunks=nchunks,
                 label=f"download di {fname} ({human(file_size)}) in RAM")
    t0 = time.monotonic()

    def on_progress(done, tot, speed):
        frac = done / tot if tot else 0.0
        rep.progress(phase="download", frac=frac, speed=speed or done / max(time.monotonic() - t0, 1e-6),
                     **ctx(dl_w * frac))

    aad = Crypto.aad(manifest["backup_id"], index, c["offset"], c["length"], manifest["compression"],
                     sparse) if crypto else None
    for attempt in range(3 if not store.local else 1):
        # il pacchetto viene aperto una sola volta: verifica e scrittura leggono lo stesso descrittore
        local = store.fetch_chunk(fname, on_progress, file_size)
        rep.emit("phase", phase="verify", chunk=index, chunks=nchunks,
                 label="verifica SHA-256" + ("" if store.local else " in RAM"))
        problem = None
        hdr = {}
        try:
            hdr = parse_header(local.fd)
            if (hdr.get("backup_id") != manifest["backup_id"] or hdr.get("index") != index
                    or hdr.get("offset") != c["offset"] or hdr.get("length") != c["length"]):
                problem = "intestazione non corrispondente al manifest"
            elif os.fstat(local.fd).st_size != file_size:
                problem = "dimensione errata"
        except (EngineError, ValueError) as e:
            problem = str(e)
        if not problem and sha256_of_fd(local.fd, HEADER_SIZE, rep,
                                        lambda f: dict(ctx(dl_w + ver_w * f))) != c["payload_sha256"]:
            problem = "checksum SHA-256 non corrispondente"
        if not problem and crypto:
            if not hdr.get("encrypted"):
                problem = "pacchetto non cifrato in un backup protetto"
            elif not gcm_check(local.fd, crypto, hdr, aad):
                problem = "sigillo di autenticita' non valido (pacchetto manomesso o danneggiato)"
        if not problem:
            break
        store.release(local)
        if not store.local:
            rep.log(f"{fname}: {problem}. Lo riscarico.", "warning")
    else:
        raise EngineError(f"il pacchetto {fname} " + ("sul disco" if store.local else "sul cloud")
                          + f" e' danneggiato ({problem})")

    if target_fd is None and manifest["compression"] == "none":
        store.release(local)            # verifica: il file coincide gia' con i dati originali
        return

    # 2) scrittura sul disco (o solo decompressione per la verifica)
    rep.emit("phase", phase="write", chunk=index, chunks=nchunks,
             label="verifica dei dati decompressi" if target_fd is None else "scrittura sul disco")
    stream = ChunkStream(local.fd, manifest["compression"], crypto, hdr, aad)
    expected = c.get("used", c["length"]) if sparse else c["length"]
    written = 0
    t0 = time.monotonic()
    ok = False
    try:
        if sparse:
            n = struct.unpack("<Q", stream.read_exact(8, "tabella"))[0]
            if n > c["length"] // 512 + 1 or n > (1 << 20):
                raise EngineError(f"{fname}: tabella delle zone non valida")
            table = stream.read_exact(SPARSE_TABLE.size * n, "tabella")
            parts = [SPARSE_TABLE.unpack_from(table, k * SPARSE_TABLE.size) for k in range(n)]
        else:
            parts = [(0, c["length"])]
        for rel, ln in parts:
            if rel + ln > c["length"]:
                raise EngineError(f"{fname}: zona fuori dal pacchetto")
            pos = 0
            while pos < ln:
                check_cancel()
                data = stream.read_exact(min(IO_BLOCK, ln - pos), "dati")
                if target_fd is not None:
                    if not (target_kind == "file" and data.count(0) == len(data)):   # file: resta sparse
                        write_all(target_fd, data, c["offset"] + rel + pos)
                pos += len(data)
                written += len(data)
                rep.progress(phase="write", frac=written / max(expected, 1),
                             speed=written / max(time.monotonic() - t0, 1e-6),
                             **ctx(dl_w + ver_w + wr_w * written / max(expected, 1)))
        if stream.read(1):
            raise EngineError(f"{fname}: dati piu' lunghi del previsto")
        ok = True
    finally:
        stream.close(check=ok)
    if stream.hash.hexdigest() != c["raw_sha256"]:
        raise EngineError(f"{fname}: i dati estratti non corrispondono al checksum originale")
    if target_fd is not None:
        os.fsync(target_fd)                     # dati al sicuro sul disco prima di liberare la RAM
        try:
            os.posix_fadvise(target_fd, c["offset"], c["length"], os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
    store.release(local)


# ---------------------------------------------------------------------------
# Controllo del disco (superficie + SMART)
# ---------------------------------------------------------------------------

def smart_report(dev):
    """Stato SMART riassunto in righe di testo (richiede smartmontools)."""
    exe = shutil.which("smartctl")
    if not exe or not is_block(dev):
        return None, ["SMART non disponibile (installa smartmontools)" if not exe else "SMART non applicabile"]
    try:
        r = subprocess.run([exe, "-H", "-A", "-j", dev], capture_output=True, text=True, env=c_env(), timeout=120)
        d = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, ["SMART: lettura non riuscita"]
    lines = []
    passed = (d.get("smart_status") or {}).get("passed")
    if passed is not None:
        lines.append("Stato SMART: " + ("BUONO" if passed else "GUASTO IMMINENTE: copia subito i dati"))
    names = {5: "Settori riallocati", 187: "Errori non correggibili", 197: "Settori in attesa di riallocazione",
             198: "Settori non correggibili offline"}
    for a in (d.get("ata_smart_attributes") or {}).get("table", []):
        if a.get("id") in names:
            raw = (a.get("raw") or {}).get("value", 0)
            lines.append(f"{names[a['id']]}: {raw}" + ("  ⚠" if raw else ""))
    nv = d.get("nvme_smart_health_information_log")
    if nv:
        lines.append(f"Errori del supporto (NVMe): {nv.get('media_errors', 0)}"
                     + ("  ⚠" if nv.get("media_errors") else ""))
        lines.append(f"Usura stimata: {nv.get('percentage_used', 0)}%")
        if nv.get("critical_warning"):
            lines.append(f"Avviso critico NVMe: {nv['critical_warning']}  ⚠")
    if not lines:
        lines.append("SMART: nessuna informazione disponibile per questo disco")
    return passed, lines


def cmd_scan(args, rep):
    info = device_info(args.source)
    total = info["size"]
    sector = max(info["logical_sector"], 512)
    passed, smart_lines = smart_report(args.source)
    for line in smart_lines:
        rep.log(line, "warning" if "⚠" in line or passed is False else "info")
    rep.emit("start", mode="scan", total=total, chunks=1, chunk_size=total, dest="", first=1)
    rep.emit("phase", phase="scan", chunk=1, chunks=1, label=f"lettura di tutti i settori di {args.source}")
    rep.bad_count = 0
    bad = []
    fd = os.open(args.source, os.O_RDONLY)
    buf = bytearray(IO_BLOCK)
    mv = memoryview(buf)
    pos = 0
    t0 = time.monotonic()
    try:
        with SleepInhibitor("Controllo del disco in corso"):
            while pos < total:
                check_cancel()
                n = min(IO_BLOCK, total - pos)
                read_block(fd, mv[:n], pos, sector, bad, rep, args.bad_sectors)
                try:
                    os.posix_fadvise(fd, pos, n, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
                pos += n
                rep.progress(phase="scan", frac=pos / total, speed=pos / max(time.monotonic() - t0, 1e-6),
                             chunk=1, chunks=1, overall=pos / total)
    finally:
        os.close(fd)
    nbad = bad_bytes(bad) // sector
    rep.emit("scan_result", bad_sectors=nbad, bad_ranges=bad[:1000], smart=smart_lines, smart_ok=passed)
    if nbad:
        msg = (f"Controllo completato: {nbad} settori illeggibili ({human(nbad * sector)}). Il backup e' "
               "possibile: scegli se riprovare a leggerli o saltarli. Sostituisci il disco al piu' presto.")
    else:
        msg = "Controllo completato: tutti i settori sono leggibili."
    if passed is False:
        msg += " ATTENZIONE: lo stato SMART indica un guasto imminente."
    rep.emit("done", ok=True, msg=msg)


# ---------------------------------------------------------------------------
# Pennetta avviabile "BKHD Live"
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.realpath(__file__))
RESOURCE_DIRS = [HERE, os.path.join(HERE, "..", "data"), os.path.join(HERE, "..", "docs"),
                 os.path.join(HERE, "..", "live"), "/usr/lib/bkhd-backup", "/usr/share/bkhd-backup",
                 "/usr/share/doc/bkhd-backup", "/usr/share/icons/hicolor/scalable/apps", "/usr/share/applications",
                 "/usr/share/polkit-1/actions"]
BLKFLSBUF = 0x1261
SHIPPED_ISO_DIR = "/usr/share/bkhd-backup/live"     # immagine BKHD Live installata con il pacchetto .deb
LIVE_SCRIPT = "bkhd-live-build.sh"


def find_resource(name):
    for d in RESOURCE_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return os.path.realpath(p)
    return None


def build_payload(dst):
    """File di BKHD Backup da copiare nel sistema live (stessa struttura del pacchetto .deb)."""
    def put(name, rel, mode=0o644, required=True):
        src = find_resource(name)
        if not src:
            if required:
                raise EngineError(f"file del programma non trovato: {name}")
            return
        path = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(src, path)
        os.chmod(path, mode)

    put("bkhd_engine.py", "usr/lib/bkhd-backup/bkhd_engine.py", 0o755)
    put("bkhd_gui.py", "usr/lib/bkhd-backup/bkhd_gui.py", 0o755)
    put(LIVE_SCRIPT, f"usr/lib/bkhd-backup/{LIVE_SCRIPT}", 0o755)
    put("bkhd-backup.desktop", "usr/share/applications/bkhd-backup.desktop")
    put("bkhd-backup.svg", "usr/share/icons/hicolor/scalable/apps/bkhd-backup.svg")
    put("org.bkhd.backup.policy", "usr/share/polkit-1/actions/org.bkhd.backup.policy")
    put("bkhd-wallpaper.svg", "usr/share/bkhd-backup/bkhd-wallpaper.svg", required=False)
    put("bkhd-banner.svg", "usr/share/bkhd-backup/bkhd-banner.svg", required=False)
    put("bkhd-banner-notte.svg", "usr/share/bkhd-backup/bkhd-banner-notte.svg", required=False)
    put("Guida-BKHD-Backup.pdf", "usr/share/doc/bkhd-backup/Guida-BKHD-Backup.pdf", required=False)
    for size in (16, 22, 24, 32, 48, 64, 128, 256, 512):
        png = f"/usr/share/icons/hicolor/{size}x{size}/apps/bkhd-backup.png"
        if os.path.isfile(png):
            path = os.path.join(dst, png.lstrip("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(png, path)
    for link, target in (("usr/bin/bkhd-backup", "../lib/bkhd-backup/bkhd_gui.py"),
                         ("usr/sbin/bkhd-engine", "../lib/bkhd-backup/bkhd_engine.py")):
        os.makedirs(os.path.join(dst, os.path.dirname(link)), exist_ok=True)
        os.symlink(target, os.path.join(dst, link))


FORBIDDEN_WORKDIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/media", "/mnt",
                      "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/tmp",
                      "/var/cache", "/var/lib")


def secure_workdir(path):
    """
    Cartella di lavoro per la pennetta: deve appartenere all'amministratore e non essere scrivibile da altri.
    Viene poi "bloccata" con un montaggio bind in /run/bkhd (raggiungibile solo da root): anche se qualcuno
    rinominasse le cartelle superiori, lo script lavorerebbe sempre sulla cartella verificata.
    """
    real = os.path.realpath(path)
    if real in FORBIDDEN_WORKDIRS or real.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/boot/", "/etc/",
                                                      "/usr/", "/bin/", "/sbin/", "/lib")):
        raise EngineError(f"cartella di lavoro non ammessa: {real}")
    parent, name = os.path.split(real)
    pfd = open_dir_nofollow(parent)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
        except FileExistsError:
            pass
        fd = os.open(name, O_NOFOLLOW_DIR, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        st = os.fstat(fd)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise EngineError(f"la cartella di lavoro {real} non appartiene all'amministratore o e' scrivibile da "
                              "altri utenti: scegli una cartella nuova (verra' creata automaticamente)")
        try:
            os.close(os.open(".bkhd-workdir", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=fd))
        except OSError as e:
            raise EngineError(f"cartella di lavoro non utilizzabile: {e.strerror}")
        target = tempfile.mkdtemp(prefix="lavoro-", dir=secure_run_dir())
        r = subprocess.run(["mount", "--no-canonicalize", "--bind", f"/proc/self/fd/{fd}", target],
                           pass_fds=(fd,), capture_output=True, text=True)
        if r.returncode != 0:
            os.rmdir(target)
            raise EngineError("impossibile preparare la cartella di lavoro: " + r.stderr.strip())
        st2 = os.stat(target)
        if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
            release_workdir(target)
            raise EngineError("la cartella di lavoro e' cambiata durante la preparazione")
        return real, target
    finally:
        os.close(fd)


def release_workdir(target):
    for args in (["umount", target], ["umount", "-l", target]):
        if subprocess.run(args, capture_output=True).returncode == 0:
            break
    try:
        os.rmdir(target)
    except OSError:
        pass


def deliver_to_user(src, dest, owner, rep):
    """Copia l'immagine nella cartella dell'utente senza seguire link e gliela assegna; crea anche il .sha256."""
    dest = os.path.abspath(dest)
    name = os.path.basename(dest)
    if not name.endswith(".iso") or "/" in name:
        raise EngineError("il nome dell'immagine deve terminare con .iso")
    dfd = open_dir_nofollow(os.path.realpath(os.path.dirname(dest)))
    uid = owner_uid(owner)
    try:
        for n in (name, name + ".sha256"):
            try:
                st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                raise EngineError(f"{os.path.join(os.path.dirname(dest), n)} esiste e non e' un tuo file: "
                                  "scegli un altro nome")
            os.unlink(n, dir_fd=dfd)
        h = hashlib.sha256()
        sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        ofd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
        try:
            if owner and os.geteuid() == 0:
                os.fchown(ofd, owner.pw_uid, owner.pw_gid)
            size, pos = os.fstat(sfd).st_size, 0
            while True:
                data = os.read(sfd, IO_BLOCK)
                if not data:
                    break
                h.update(data)
                write_all(ofd, data)
                pos += len(data)
                rep.progress(phase="write", frac=pos / max(size, 1), speed=0, chunk=9, chunks=9, overall=1.0)
            os.fsync(ofd)
        finally:
            os.close(sfd)
            os.close(ofd)
        hfd = os.open(name + ".sha256", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                      dir_fd=dfd)
        try:
            if owner and os.geteuid() == 0:
                os.fchown(hfd, owner.pw_uid, owner.pw_gid)
            write_all(hfd, f"{h.hexdigest()}  {name}\n".encode())
        finally:
            os.close(hfd)
        return size
    finally:
        os.close(dfd)


def cmd_live_build(args, rep):
    if os.geteuid() != 0:
        raise EngineError("la creazione della pennetta richiede i permessi di amministratore")
    script = find_resource(LIVE_SCRIPT)
    if not script:
        raise EngineError(f"script {LIVE_SCRIPT} non trovato")
    if args.mirror and not re.fullmatch(r"https?://[A-Za-z0-9._~:/%-]+", args.mirror):
        raise EngineError("indirizzo del mirror non valido")
    out = os.path.abspath(args.output)
    if not out.endswith(".iso"):
        raise EngineError("il nome dell'immagine deve terminare con .iso")
    owner = resolve_run_as(args.run_as)
    real_work, work = secure_workdir(args.workdir)
    try:
        _live_build(args, rep, script, out, owner, real_work, work)
    finally:
        release_workdir(work)


def _live_build(args, rep, script, out, owner, real_work, work):
    st = os.statvfs(work)
    if st.f_bavail * st.f_frsize < 10 * GIB and not os.path.isdir(os.path.join(work, "chroot")):
        raise EngineError(f"spazio insufficiente in {real_work}: servono almeno 10 GB liberi")
    payload = tempfile.mkdtemp(prefix="payload-", dir=work)
    build_payload(payload)
    iso_tmp = os.path.join(work, "risultato", "bkhd-live.iso")
    rep.emit("start", mode="live-build", total=9, chunks=9, chunk_size=1, dest=out, first=1)
    rep.log(f"Creazione di BKHD Live in {out} (cartella di lavoro {real_work}). Servono una connessione "
            "Internet e da 20 a 60 minuti.")
    cmd = ["bash", script, "--payload", payload, "--output", iso_tmp, "--workdir", work,
           "--version", VERSION]
    if args.mirror:
        cmd += ["--mirror", args.mirror]
    if args.clean:
        cmd.append("--clean")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=dict(c_env(), PATH="/usr/sbin:/usr/bin:/sbin:/bin"), start_new_session=True)
    finished = threading.Event()

    def killer():
        while not finished.wait(0.5):
            if CANCEL.is_set():
                os.killpg(proc.pid, signal.SIGTERM)
                return

    threading.Thread(target=killer, daemon=True).start()
    tail, error, result = [], None, None
    phase, nphases, last_detail = 0, 9, 0.0
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line.startswith("##BKHD-FASE "):
                num, _, text = line[12:].partition(" ")
                phase, nphases = (int(x) for x in num.split("/"))
                rep.emit("phase", phase="live", chunk=phase, chunks=nphases, label=text)
                rep.log(f"Fase {phase}/{nphases}: {text}")
                rep.progress(force=True, phase="live", frac=0.0, speed=0, chunk=phase, chunks=nphases,
                             overall=(phase - 1) / nphases)
            elif line.startswith("##BKHD-LOG "):
                rep.log(line[11:])
            elif line.startswith("##BKHD-ERRORE "):
                error = line[14:]
            elif line.startswith("##BKHD-RISULTATO "):
                result = line[17:].split()
            elif line.strip():
                tail = (tail + [line])[-25:]
                now = time.monotonic()
                if now - last_detail > 0.3:
                    last_detail = now
                    rep.emit("detail", text=line[:160])
        rc = proc.wait()
    finally:
        finished.set()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        shutil.rmtree(payload, ignore_errors=True)
    check_cancel()
    if rc != 0 or not result:
        rep.log("Ultime righe della costruzione:\n" + "\n".join(tail), "warning")
        raise EngineError(f"creazione di BKHD Live non riuscita: {error or f'codice di uscita {rc}'}")
    rep.log(f"Copia dell'immagine in {out}...")
    size = deliver_to_user(iso_tmp, out, owner, rep)
    os.remove(iso_tmp)
    rep.emit("done", ok=True, msg=f"Immagine BKHD Live pronta: {out} ({human(size)}). "
                                  "Ora puoi scriverla su una chiavetta USB.")


def removable_disk(name):
    """True se il disco e' rimovibile o collegato via USB (protezione contro la scrittura su dischi interni)."""
    try:
        with open(f"/sys/class/block/{name}/removable") as f:
            if f.read().strip() == "1":
                return True
    except OSError:
        pass
    return "/usb" in os.path.realpath(f"/sys/class/block/{name}")


DATA_LABEL = "BKHD-DATI"
GPT_BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GPT_LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
FS_TOOLS = {"exfat": "mkfs.exfat", "ext4": "mkfs.ext4", "vfat": "mkfs.vfat", "ntfs": "mkfs.ntfs"}


def check_label(fs, label):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,11}", label or ""):
        raise EngineError("etichetta non valida: da 1 a 11 caratteri tra lettere, numeri, - e _")
    return label.upper() if fs == "vfat" else label


def open_usb_target(target, rep):
    """Chiavetta di destinazione: disco intero, USB/rimovibile, partizioni smontate, dispositivo verificato.
    Restituisce (percorso reale, "maj:min", dimensione)."""
    target = os.path.realpath(target)
    if not is_block(target):
        raise EngineError(f"{target} non e' un disco")
    name = os.path.basename(target)
    if os.path.exists(f"/sys/class/block/{name}/partition"):
        raise EngineError(f"{target} e' una partizione: scegli la chiavetta intera (es. /dev/sdb)")
    try:
        with open(f"/sys/class/block/{name}/dev") as f:
            expected_dev = f.read().strip()
    except OSError:
        raise EngineError(f"{target}: dispositivo sconosciuto")
    if not removable_disk(name):
        raise EngineError(f"{target} non risulta una chiavetta USB o un disco rimovibile: per sicurezza non "
                          "viene modificato")
    for reason in block_in_use(target):     # partizioni aperte automaticamente dal desktop: si smontano
        dev = reason.split()[0]
        if " montato su " in reason:
            rep.log(f"Smonto {dev}...")
            if subprocess.run(["umount", dev], capture_output=True).returncode != 0:
                raise EngineError(f"impossibile smontare {dev}: chiudi le finestre che lo usano e riprova")
        else:
            raise EngineError(f"la chiavetta e' in uso: {reason}")
    fd = os.open(target, os.O_RDONLY | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISBLK(st.st_mode) or f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}" != expected_dev:
            raise EngineError(f"{target} non corrisponde al dispositivo verificato")
        size = os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)
    return target, expected_dev, size


def reread_partitions(target):
    if not is_block(target):
        return
    try:
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, BLKRRPART)
        finally:
            os.close(fd)
    except OSError:
        subprocess.run(["partx", "-u", target], capture_output=True)
    subprocess.run(["udevadm", "settle", "--timeout=15"], capture_output=True)


def partition_node(target, number):
    """Nodo della partizione 'number' del disco (sdb1, nvme0n1p1, mmcblk0p1), atteso fino a 15 secondi."""
    name = os.path.basename(target)
    for _ in range(30):
        for cand in (f"{name}{number}", f"{name}p{number}"):
            if os.path.exists(f"/sys/class/block/{cand}/partition") and os.path.exists(f"/dev/{cand}"):
                return f"/dev/{cand}"
        time.sleep(0.5)
    raise EngineError("la nuova partizione non e' comparsa: scollega e ricollega la chiavetta e riprova")


def run_tool(cmd, what, input_text=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input_text, env=c_env(), timeout=1800)
    if r.returncode != 0:
        raise EngineError(f"{what} non riuscito: " + ((r.stderr or r.stdout).strip().splitlines() or ["errore"])[-1])
    return r


def make_filesystem(fs, label, node, owner, offset=None, size=None):
    """Crea il file system; 'offset'/'size' (byte) solo per le prove su file immagine."""
    tool = shutil.which(FS_TOOLS[fs])
    if not tool:
        pkg = {"exfat": "exfatprogs", "ntfs": "ntfs-3g", "vfat": "dosfstools", "ext4": "e2fsprogs"}[fs]
        raise EngineError(f"manca il programma per formattare in {fs}: sudo apt install {pkg}")
    uid, gid = (owner.pw_uid, owner.pw_gid) if owner else (os.geteuid(), os.getegid())
    if fs == "ext4":
        ext = f"root_owner={uid}:{gid}" + (f",offset={offset}" if offset is not None else "")
        cmd = [tool, "-F", "-q", "-L", label, "-E", ext, node] + ([str(size // 1024)] if size else [])
    elif fs == "vfat":
        cmd = [tool, "-F", "32", "-n", label] + (["--offset", str(offset // 512)] if offset is not None else [])
        cmd += [node] + ([str(size // 1024)] if size else [])
    elif fs == "exfat":
        if offset is not None:                   # prova su file immagine: si formatta a parte e si copia
            if os.geteuid() == 0:
                raise EngineError("exFAT su file immagine non supportato")
            with tempfile.TemporaryDirectory(prefix="bkhd-exfat-") as tmp:
                part = os.path.join(tmp, "p.img")
                with open(part, "wb") as f:
                    f.truncate(size)
                run_tool([tool, "-L", label, part], "formattazione exfat")
                with open(part, "rb") as fi, open(node, "r+b") as fo:
                    fo.seek(offset)
                    shutil.copyfileobj(fi, fo, IO_BLOCK)
            return
        cmd = [tool, "-L", label, node]
    else:
        if offset is not None:
            raise EngineError("NTFS su file immagine non supportato (solo per le prove)")
        cmd = [tool, "-f", "-q", "-L", label, node]
    run_tool(cmd, f"formattazione {fs}")


def add_data_partition(target, fs, label, owner, rep):
    """Dopo la live: sposta la GPT di riserva in fondo e usa lo spazio rimanente per una partizione dati."""
    rep.emit("phase", phase="write", chunk=3, chunks=3, unit="Fase",
             label=f"creazione della partizione {label} per i backup")
    if not shutil.which("sgdisk"):
        raise EngineError("per usare lo spazio rimanente serve gdisk: sudo apt install gdisk")
    # la GPT della ISO finisce con l'immagine: si estende fino alla fine della chiavetta (cambiano solo i
    # settori 0-33 e la copia di riserva in fondo; il sistema live resta intatto)
    run_tool(["sgdisk", "-e", target], "aggiornamento della tabella delle partizioni")
    gtype = GPT_LINUX_FS if fs == "ext4" else GPT_BASIC_DATA
    run_tool(["sfdisk", "--append", "--wipe", "never", "--wipe-partitions", "never", "--no-reread", target],
             "creazione della partizione dati", input_text=f",,{gtype}\n")
    pt = json.loads(run_tool(["sfdisk", "-J", target], "lettura delle partizioni").stdout)["partitiontable"]
    last = max(pt["partitions"], key=lambda x: x["start"])
    ssz = pt.get("sectorsize", 512)
    number = len(pt["partitions"])
    run_tool(["sfdisk", "--part-label", "--no-reread", target, str(number), label], "nome della partizione dati")
    rep.log(f"Partizione {label}: {human(last['size'] * ssz)} disponibili per i backup ({fs}).")
    if is_block(target):
        reread_partitions(target)
        node = partition_node(target, number)
        make_filesystem(fs, label, node, owner)
    else:                                   # prova su file immagine
        make_filesystem(fs, label, target, owner, last["start"] * ssz, last["size"] * ssz)


def cmd_usb_format(args, rep):
    """Formatta una chiavetta per usarla come destinazione dei backup."""
    if not args.yes:
        raise EngineError("la formattazione cancella la chiavetta: conferma con --yes")
    owner = resolve_run_as(args.run_as)
    label = check_label(args.fs, args.label)
    if not shutil.which(FS_TOOLS[args.fs]):
        make_filesystem(args.fs, label, "/dev/null", owner)       # solo per il messaggio "installa ..."
    if is_block(os.path.realpath(args.target)):
        target, _, size = open_usb_target(args.target, rep)
    elif args.allow_file and os.geteuid() != 0:                  # solo per i test, mai con privilegi
        target, size = os.path.abspath(args.target), os.path.getsize(args.target)
    else:
        raise EngineError(f"{args.target} non e' un disco")
    rep.emit("start", mode="usb-format", total=size, chunks=2, chunk_size=size, dest=target, first=1)
    rep.emit("phase", phase="write", chunk=1, chunks=2, unit="Fase", label=f"nuova tabella delle partizioni su {target}")
    if is_block(target):
        run_tool(["wipefs", "-a", target], "cancellazione delle vecchie firme")
    gpt = size > 2 * 1024 ** 4
    if gpt:
        ptype = GPT_LINUX_FS if args.fs == "ext4" else GPT_BASIC_DATA
        script = f"label: gpt\n,,{ptype}\n"
    else:
        ptype = {"ext4": "83", "vfat": "c", "exfat": "7", "ntfs": "7"}[args.fs]
        script = f"label: dos\n,,{ptype}\n"
    run_tool(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", "--no-reread", target],
             "creazione della partizione", input_text=script)
    rep.emit("phase", phase="write", chunk=2, chunks=2, unit="Fase", label=f"formattazione in {args.fs} ({label})")
    if is_block(target):
        reread_partitions(target)
        make_filesystem(args.fs, label, partition_node(target, 1), owner)
    else:
        pt = json.loads(run_tool(["sfdisk", "-J", target], "lettura delle partizioni").stdout)["partitiontable"]
        part = pt["partitions"][0]
        ssz = pt.get("sectorsize", 512)
        make_filesystem(args.fs, label, target, owner, part["start"] * ssz, part["size"] * ssz)
    rep.emit("done", ok=True, msg=f"Chiavetta formattata in {args.fs} con etichetta «{label}»: ora puoi usarla "
                                  "come destinazione dei backup.")


def optical_device(dev):
    """Unita' ottica (masterizzatore) verificata: /dev/srN con tipo SCSI 5 (CD/DVD)."""
    real = os.path.realpath(dev)
    name = os.path.basename(real)
    try:
        with open(f"/sys/class/block/{name}/device/type") as f:
            if f.read().strip() != "5":
                raise OSError
    except OSError:
        raise EngineError(f"{dev} non e' un'unita' CD/DVD")
    return real


def cmd_disc_burn(args, rep):
    """Masterizza l'immagine BKHD Live su DVD (o BD) e la verifica rileggendola."""
    if not args.yes:
        raise EngineError("la masterizzazione sovrascrive il disco inserito: conferma con --yes")
    xorriso = shutil.which("xorriso")
    if not xorriso:
        raise EngineError("per masterizzare serve xorriso: sudo apt install xorriso")
    owner = resolve_run_as(args.run_as)
    ifd = os.open(os.path.abspath(args.image), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    ist = os.fstat(ifd)
    if not stat.S_ISREG(ist.st_mode) or ist.st_uid not in (0, owner_uid(owner)):
        os.close(ifd)
        raise EngineError("l'immagine deve essere un tuo file (ad esempio quella fornita con BKHD Backup)")
    size = ist.st_size
    dev = optical_device(args.device)
    r = subprocess.run([xorriso, "-outdev", dev, "-tell_media_space"], capture_output=True, text=True,
                       env=c_env(), timeout=300)
    out = r.stdout + r.stderr
    m = re.search(r"Media space\s*:\s*(\d+)s", out)
    if not m:
        os.close(ifd)
        raise EngineError("nessun disco scrivibile nell'unita': inserisci un DVD vergine o riscrivibile")
    free = int(m.group(1)) * 2048
    if free < size:
        kind = "un CD (massimo 700 MB)" if free <= 900 * MIB else "il disco inserito"
        os.close(ifd)
        raise EngineError(f"l'immagine ({human(size)}) non entra in {kind}: liberi {human(free)}. "
                          "Usa un DVD da 4,7 GB (o una pennetta USB).")
    rep.emit("start", mode="disc-burn", total=size, chunks=2, chunk_size=size, dest=dev, first=1)
    rep.emit("phase", phase="write", chunk=1, chunks=2, unit="Fase", label=f"masterizzazione su {dev}")
    proc = subprocess.Popen([xorriso, "-as", "cdrecord", "-v", f"dev={dev}", "blank=as_needed", "-"],
                            stdin=ifd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=c_env())
    os.close(ifd)
    finished = threading.Event()

    def killer():
        while not finished.wait(0.5):
            if CANCEL.is_set():
                proc.terminate()
                return

    threading.Thread(target=killer, daemon=True).start()
    tail = []
    t0 = time.monotonic()
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            tail = (tail + [line])[-10:]
            m = re.search(r"(\d+)\s+of\s+(\d+)\s+MB written", line)
            if m and int(m.group(2)):
                frac = int(m.group(1)) / int(m.group(2))
                rep.progress(phase="write", frac=frac, speed=int(m.group(1)) * MIB / max(time.monotonic() - t0, 1e-6),
                             chunk=1, chunks=2, overall=0.75 * frac)
        rc = proc.wait()
    finally:
        finished.set()
    check_cancel()
    if rc != 0:
        raise EngineError("masterizzazione non riuscita: " + " | ".join(l for l in tail[-3:] if l))
    rep.emit("phase", phase="verify", chunk=2, chunks=2, unit="Fase", label="verifica del disco (rilettura)")
    h1, h2 = hashlib.sha256(), hashlib.sha256()
    with open(os.path.abspath(args.image), "rb") as f:
        for data in iter(lambda: f.read(IO_BLOCK), b""):
            h1.update(data)
    subprocess.run(["udevadm", "settle", "--timeout=15"], capture_output=True)
    dfd = os.open(dev, os.O_RDONLY | os.O_CLOEXEC)
    try:
        pos = 0
        while pos < size:
            check_cancel()
            data = os.pread(dfd, min(IO_BLOCK, size - pos), pos)
            if not data:
                break
            h2.update(data)
            pos += len(data)
            rep.progress(phase="verify", frac=pos / size, speed=0, chunk=2, chunks=2, overall=0.75 + 0.25 * pos / size)
    finally:
        os.close(dfd)
    if h1.hexdigest() != h2.hexdigest():
        raise EngineError("la verifica del disco e' fallita: i dati riletti non coincidono")
    subprocess.run(["eject", dev], capture_output=True)
    rep.emit("done", ok=True, msg=f"DVD BKHD Live masterizzato e verificato ({human(size)}). Avvia il PC dal DVD "
                                  "(menu di avvio: di solito F12, F11, F8 o Esc).")


def cmd_live_write(args, rep):
    owner = resolve_run_as(args.run_as)
    try:
        ifd = os.open(os.path.abspath(args.image), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise EngineError(f"immagine non leggibile: {args.image} ({e.strerror})")
    ist = os.fstat(ifd)
    if not stat.S_ISREG(ist.st_mode) or ist.st_uid not in (0, owner_uid(owner)):
        os.close(ifd)
        raise EngineError("l'immagine deve essere un tuo file (ad esempio quella creata da BKHD Backup)")
    size = ist.st_size
    if not args.yes:
        os.close(ifd)
        raise EngineError("la scrittura cancella la chiavetta: conferma con --yes")
    data_label = None
    if args.data_fs != "none":
        data_label = check_label(args.data_fs, args.data_label)
        if not shutil.which(FS_TOOLS[args.data_fs]):
            make_filesystem(args.data_fs, data_label, "/dev/null", owner)
    target = os.path.realpath(args.target)
    if is_block(target):
        name = os.path.basename(target)
        if os.path.exists(f"/sys/class/block/{name}/partition"):
            raise EngineError(f"{target} e' una partizione: scegli la chiavetta intera (es. /dev/sdb)")
        try:
            with open(f"/sys/class/block/{name}/dev") as f:
                expected_dev = f.read().strip()
        except OSError:
            raise EngineError(f"{target}: dispositivo sconosciuto")
        if not removable_disk(name):
            raise EngineError(f"{target} non risulta una chiavetta USB o un disco rimovibile: per sicurezza non "
                              "viene scritto")
        for reason in block_in_use(target):     # partizioni aperte automaticamente dal desktop: si smontano
            dev = reason.split()[0]
            if " montato su " in reason:
                rep.log(f"Smonto {dev}...")
                if subprocess.run(["umount", dev], capture_output=True).returncode != 0:
                    raise EngineError(f"impossibile smontare {dev}: chiudi le finestre che lo usano e riprova")
            else:
                raise EngineError(f"la chiavetta e' in uso: {reason}")
        fd = os.open(target, os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
        st = os.fstat(fd)
        if not stat.S_ISBLK(st.st_mode) or f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}" != expected_dev:
            os.close(fd)
            raise EngineError(f"{target} non corrisponde al dispositivo verificato")
        need = size + (512 * MIB if data_label else 0)
        if os.lseek(fd, 0, os.SEEK_END) < need:
            os.close(fd)
            raise EngineError(f"la chiavetta e' troppo piccola: servono almeno {human(need)}")
    elif args.allow_file and os.geteuid() != 0:     # solo per i test, mai con privilegi
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)   # come una chiavetta: dimensione fissa
    else:
        raise EngineError(f"{target} non e' un disco")
    nph = 3 if data_label else 2
    rep.emit("start", mode="live-write", total=size, chunks=nph, chunk_size=size, dest=target, first=1)
    rep.emit("phase", phase="write", chunk=1, chunks=nph, unit="Fase", label=f"scrittura di BKHD Live su {target}")
    h = hashlib.sha256()
    pos = 0
    t0 = time.monotonic()
    try:
        with SleepInhibitor("Scrittura della pennetta in corso"), os.fdopen(ifd, "rb") as f:
            while True:
                check_cancel()
                data = f.read(IO_BLOCK)
                if not data:
                    break
                h.update(data)
                write_all(fd, data, pos)
                pos += len(data)
                rep.progress(phase="write", frac=pos / size, speed=pos / max(time.monotonic() - t0, 1e-6),
                             chunk=1, chunks=nph, overall=0.7 * pos / size)
            rep.log("Svuotamento delle cache: attendi che i dati arrivino davvero sulla chiavetta...")
            os.fsync(fd)
            if is_block(target):
                try:
                    fcntl.ioctl(fd, BLKFLSBUF)
                except OSError:
                    pass
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)
        fd = None
        rep.emit("phase", phase="verify", chunk=2, chunks=nph, unit="Fase", label="verifica della chiavetta (rilettura)")
        h2 = hashlib.sha256()
        rfd = os.open(target, os.O_RDONLY)
        try:
            os.posix_fadvise(rfd, 0, 0, os.POSIX_FADV_DONTNEED)
            done = 0
            t0 = time.monotonic()
            while done < size:
                check_cancel()
                data = os.pread(rfd, min(IO_BLOCK, size - done), done)
                if not data:
                    break
                h2.update(data)
                done += len(data)
                rep.progress(phase="verify", frac=done / size, speed=done / max(time.monotonic() - t0, 1e-6),
                             chunk=2, chunks=nph, overall=0.7 + 0.25 * done / size)
            if is_block(target):
                try:
                    fcntl.ioctl(rfd, BLKRRPART)
                except OSError:
                    pass
        finally:
            os.close(rfd)
    finally:
        if fd is not None:
            os.close(fd)
    if h.hexdigest() != h2.hexdigest():
        raise EngineError("la verifica della chiavetta e' fallita: i dati riletti non coincidono. "
                          "La chiavetta potrebbe essere difettosa.")
    extra = ""
    if data_label:
        add_data_partition(target, args.data_fs, data_label, owner, rep)
        extra = (f" Lo spazio rimanente e' la partizione «{data_label}» ({args.data_fs}): in BKHD Live viene "
                 "proposta automaticamente come destinazione dei backup.")
    rep.emit("done", ok=True, msg=f"Pennetta BKHD Live pronta e verificata su {target}.{extra} "
                                  "Avvia il PC da questa chiavetta (menu di avvio: di solito F12, F11, F8 o Esc).")


# ---------------------------------------------------------------------------
# Elenco dei backup
# ---------------------------------------------------------------------------

def list_backups_in(store):
    found = []
    for d in [""] + store.list_names("", dirs=True):
        try:
            if MANIFEST_NAME not in store.list_names(d):
                continue
            m = load_manifest(store, d)
        except (EngineError, ValueError, OSError):
            continue
        m["_dir"] = store.where(d) if d else store.where()
        if m.get("version", 1) >= 2:
            m["_chunks_done"] = m.get("chunks_done", 0)
            m["_stored_bytes"] = m.get("stored_bytes", 0)
        else:
            m["_chunks_done"] = len(m.get("chunks", []))
            m["_stored_bytes"] = sum(c.get("payload_size", 0) + HEADER_SIZE for c in m.get("chunks", []))
        m.pop("chunks", None)
        found.append(m)
    return found


def list_backups(rc, base):
    """Compatibilita': elenco su un remote rclone o, se 'base' e' un percorso assoluto, in una cartella."""
    if is_local_dest(base):
        return list_backups_in(LocalStore(base, [], None, None))
    return list_backups_in(CloudStore(rc, base, None, 1))


def cmd_list(args, rep):
    owner = resolve_run_as(args.run_as)
    rc = None if is_local_dest(args.remote) else Rclone(args.rclone, args.rclone_config, owner)
    items = list_backups_in(open_store(args.remote, [args.path], rc, rep, args.retries, owner))
    if args.interrupted:
        items = [m for m in items if m.get("status") != "complete"]
    if rep.json:
        rep.emit("list", items=items)
        return
    if not items:
        print("Nessun backup trovato.")
    for m in items:
        print(f"{m['_dir']}\n   {m['status']:<11} {m['created']}  sorgente {human(m['source']['size'])} "
              f"({m['source'].get('model') or m['source']['path']}), {m['_chunks_done']}/{m['chunk_count']} "
              f"pacchetti, {human(m['_stored_bytes'])} occupati, compressione {m['compression']}"
              + (", solo settori usati" if m.get("sparse") else ""))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="bkhd-engine",
                                description="Backup settore per settore di dischi direttamente sul cloud (via rclone).")
    p.add_argument("--json", action="store_true", help="eventi JSON su stdout (usato dall'interfaccia grafica)")
    p.add_argument("--rclone", default="rclone", help="percorso dell'eseguibile rclone")
    p.add_argument("--rclone-config", default=None, help="file di configurazione rclone")
    p.add_argument("--run-as", default=None, help="utente con cui eseguire rclone (predefinito: chi ha usato sudo/pkexec)")
    p.add_argument("--retries", type=int, default=10, help="tentativi per ogni trasferimento (predefinito 10)")
    p.add_argument("--password-stdin", action="store_true",
                   help="legge la password del backup dalla prima riga dello standard input")
    p.add_argument("--version", action="version",
                   version=f"bkhd-engine {VERSION}  {COPYRIGHT} <{AUTHOR_EMAIL}>  licenza {LICENSE}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="copia un disco settore per settore sul cloud")
    b.add_argument("--source", required=True, help="disco, partizione o file immagine (es. /dev/sda)")
    b.add_argument("--remote", required=True,
                   help="remote rclone (es. gdrive:) oppure cartella locale assoluta (es. /media/utente/USB)")
    b.add_argument("--path", default="BKHD", help="cartella sul cloud (predefinito BKHD)")
    b.add_argument("--name", required=True, help="nome del backup")
    size = b.add_mutually_exclusive_group()
    size.add_argument("--chunk-size", dest="chunk_size_kib", type=parse_size_kib, default=None,
                      help="dimensione pacchetti da 1,2 MB a 8 GB, es. 1440K (floppy 3,5\"), 100M, 4G")
    size.add_argument("--chunk-size-kib", dest="chunk_size_kib", type=int, help="dimensione pacchetti in KB")
    size.add_argument("--chunk-size-mib", type=int, default=4096, help="dimensione pacchetti in MB (predefinito 4096)")
    b.add_argument("--compression", choices=["none", "zstd"], default="none")
    b.add_argument("--level", type=int, default=3, choices=range(1, 20), metavar="1-19", help="livello zstd")
    b.add_argument("--used-only", action="store_true",
                   help="copia solo i settori usati dai file system (salta lo spazio libero)")
    b.add_argument("--encrypt", action="store_true", help="protegge il backup con una password (AES-256-GCM)")
    b.add_argument("--bad-sectors", choices=["retry", "skip"], default="retry",
                   help="settori illeggibili: retry = riprova e copia cio' che si legge, skip = salta subito")
    b.add_argument("--resume", action="store_true", help="riprende un backup interrotto con lo stesso nome")
    b.add_argument("--expect-encrypted", action="store_true",
                   help="rifiuta di riprendere se il backup non risulta protetto (protezione da manomissioni)")
    b.add_argument("--overwrite", action="store_true", help="sovrascrive un backup esistente con lo stesso nome")

    r = sub.add_parser("restore", help="ripristina (o verifica) un backup dal cloud")
    r.add_argument("--remote", required=True)
    r.add_argument("--path", required=True, help="cartella del backup sul cloud, es. BKHD/pc-sda-20261009")
    r.add_argument("--target", help="disco o file immagine di destinazione")
    r.add_argument("--verify-only", action="store_true", help="scarica e verifica senza scrivere nulla")
    r.add_argument("--from-chunk", type=int, default=1, help="riprende il ripristino da questo pacchetto")
    r.add_argument("--allow-incomplete", action="store_true")
    r.add_argument("--expect-encrypted", action="store_true",
                   help="rifiuta il ripristino se il backup non risulta protetto (protezione da manomissioni)")
    r.add_argument("--yes", action="store_true", help="conferma la sovrascrittura della destinazione")

    sc = sub.add_parser("scan", help="controlla tutti i settori di un disco e lo stato SMART")
    sc.add_argument("--source", required=True)
    sc.add_argument("--bad-sectors", choices=["retry", "skip"], default="skip",
                    help="skip (predefinito) = controllo rapido, retry = insiste sui settori difettosi")

    lb = sub.add_parser("live-build", help="crea l'immagine ISO della pennetta avviabile BKHD Live")
    lb.add_argument("--output", required=True, help="file ISO da creare")
    lb.add_argument("--workdir", default="/var/cache/bkhd-live",
                    help="cartella di lavoro dell'amministratore (servono 10 GB liberi)")
    lb.add_argument("--mirror", default=None, help="mirror Ubuntu (predefinito archive.ubuntu.com)")
    lb.add_argument("--clean", action="store_true", help="cancella il sistema di lavoro alla fine")

    lw = sub.add_parser("live-write", help="scrive l'immagine BKHD Live su una chiavetta USB e la verifica")
    lw.add_argument("--image", required=True)
    lw.add_argument("--target", required=True, help="chiavetta USB intera, es. /dev/sdb")
    lw.add_argument("--yes", action="store_true")
    lw.add_argument("--data-fs", choices=["none", "exfat", "ext4"], default="none",
                    help="usa lo spazio rimanente per una partizione dati per i backup")
    lw.add_argument("--data-label", default=DATA_LABEL, help="etichetta della partizione dati (BKHD-DATI)")
    lw.add_argument("--allow-file", action="store_true", help=argparse.SUPPRESS)

    db = sub.add_parser("disc-burn", help="masterizza l'immagine BKHD Live su DVD e la verifica")
    db.add_argument("--image", required=True)
    db.add_argument("--device", required=True, help="unita' ottica, es. /dev/sr0")
    db.add_argument("--yes", action="store_true")

    uf = sub.add_parser("usb-format", help="formatta una chiavetta USB per usarla come destinazione dei backup")
    uf.add_argument("--target", required=True, help="chiavetta USB intera, es. /dev/sdb")
    uf.add_argument("--fs", choices=["exfat", "ext4", "vfat", "ntfs"], default="exfat")
    uf.add_argument("--label", default=DATA_LABEL)
    uf.add_argument("--yes", action="store_true")
    uf.add_argument("--allow-file", action="store_true", help=argparse.SUPPRESS)

    l = sub.add_parser("list", help="elenca i backup presenti sul cloud")
    l.add_argument("--remote", required=True)
    l.add_argument("--path", default="BKHD")
    l.add_argument("--interrupted", action="store_true", help="mostra solo i backup interrotti")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rep = Reporter(args.json)

    def on_signal(signum, frame):
        CANCEL.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    args.password = None
    if args.password_stdin:                 # prima riga di stdin: la password (mai sulla riga di comando)
        args.password = sys.stdin.readline().rstrip("\n")
    if args.json:
        # la GUI invia "cancel" su stdin; se la GUI si chiude (EOF) l'operazione viene annullata
        def stdin_watcher():
            for line in sys.stdin:
                if line.strip() == "cancel":
                    break
            CANCEL.set()
        threading.Thread(target=stdin_watcher, daemon=True).start()

    try:
        {"backup": cmd_backup, "restore": cmd_restore, "list": cmd_list, "scan": cmd_scan,
         "live-build": cmd_live_build, "live-write": cmd_live_write, "usb-format": cmd_usb_format,
         "disc-burn": cmd_disc_burn}[args.cmd](args, rep)
        return 0
    except Cancelled:
        rep.emit("done", ok=False, cancelled=True,
                 msg="Operazione annullata. Il backup puo' essere ripreso con l'opzione 'Riprendi'."
                 if args.cmd == "backup" else "Operazione annullata.")
        return 2
    except PermissionError as e:
        rep.emit("error", msg=f"permesso negato: {e.filename or e}. Serve l'accesso da amministratore.")
        return 1
    except (EngineError, OSError, ValueError, KeyError) as e:
        rep.emit("error", msg=str(e))
        return 1
    except Exception as e:                      # dati inattesi: errore chiaro invece di un crash
        rep.emit("error", msg=f"errore inatteso ({type(e).__name__}): {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
