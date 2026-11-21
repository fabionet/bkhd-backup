#!/usr/bin/python3
"""Finto rclone per i test: il remote 'test:' corrisponde a $FAKE_RCLONE_ROOT.

FAKE_RCLONE_FAIL_FIRST=<file>  -> il primo copyto fallisce (crea <file> come marcatore)
FAKE_RCLONE_BPS=<n>            -> limita la velocita' di copia
"""
import json, os, shutil, sys, time

ROOT = os.environ["FAKE_RCLONE_ROOT"]
REMOTES = [r for r in os.environ.get("FAKE_RCLONE_REMOTES", "test").split(",") if r]
VALUE_FLAGS = {"--config", "--stats", "--stats-log-level", "--retries", "--low-level-retries", "--include",
               "--size"}


def local(p):
    for r in REMOTES:
        if p.startswith(r + ":"):
            return os.path.join(ROOT, p[len(r) + 1:].lstrip("/"))
    return p


def main():
    argv = sys.argv[1:]
    pos, flags = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS:
            flags.setdefault(a, []).append(argv[i + 1]); i += 2
        elif a.startswith("--"):
            flags[a] = True; i += 1
        else:
            pos.append(a); i += 1
    cmd, rest = pos[0], pos[1:]
    if cmd == "listremotes":
        for r in REMOTES:
            print(r + ":")
    elif cmd == "copyto":
        marker = os.environ.get("FAKE_RCLONE_FAIL_FIRST")
        if marker and not os.path.exists(marker):
            open(marker, "w").close()
            print(json.dumps({"level": "error", "msg": "simulated network failure"}), file=sys.stderr)
            return 1
        src, dst = local(rest[0]), local(rest[1])
        if not os.path.exists(src):
            print(json.dumps({"level": "error", "msg": "file not found"}), file=sys.stderr)
            return 3
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        size = os.path.getsize(src)
        bps = int(os.environ.get("FAKE_RCLONE_BPS", "0"))
        done, last, t0 = 0, 0, time.monotonic()
        with open(src, "rb") as fi, open(dst + ".partial", "wb") as fo:
            while True:
                b = fi.read(8 << 20)
                if not b:
                    break
                fo.write(b); done += len(b)
                if bps:
                    time.sleep(len(b) / bps)
                if time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    sp = done / max(last - t0, 1e-6)
                    print(json.dumps({"level": "notice", "msg": "stats", "stats": {
                        "bytes": done, "totalBytes": size, "speed": sp}}), file=sys.stderr, flush=True)
        os.replace(dst + ".partial", dst)
    elif cmd == "rcat":
        marker = os.environ.get("FAKE_RCLONE_FAIL_FIRST")
        if marker and not os.path.exists(marker):
            open(marker, "w").close()
            sys.stdin.buffer.read()
            print(json.dumps({"level": "error", "msg": "simulated network failure"}), file=sys.stderr)
            return 1
        dst = local(rest[0])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        size = int(flags.get("--size", ["-1"])[0])
        bps = int(os.environ.get("FAKE_RCLONE_BPS", "0"))
        done, last, t0 = 0, 0, time.monotonic()
        with open(dst + ".partial", "wb") as fo:
            while True:
                b = sys.stdin.buffer.read(8 << 20)
                if not b:
                    break
                fo.write(b); done += len(b)
                if bps:
                    time.sleep(len(b) / bps)
                if time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    print(json.dumps({"level": "notice", "msg": "stats", "stats": {
                        "bytes": done, "totalBytes": size, "speed": done / max(last - t0, 1e-6)}}),
                          file=sys.stderr, flush=True)
        if size >= 0 and done != size:
            print(json.dumps({"level": "error", "msg": "size mismatch"}), file=sys.stderr)
            return 1
        os.replace(dst + ".partial", dst)
    elif cmd == "cat":
        with open(local(rest[0]), "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
    elif cmd == "lsf":
        d = local(rest[0])
        if not os.path.isdir(d):
            return 3
        for n in sorted(os.listdir(d)):
            isdir = os.path.isdir(os.path.join(d, n))
            if "--dirs-only" in flags and isdir:
                print(n + "/")
            elif "--files-only" in flags and not isdir:
                print(n)
    elif cmd == "lsjson":
        p = local(rest[0])
        if not os.path.exists(p):
            return 3
        print(json.dumps([{"Name": os.path.basename(p), "Size": os.path.getsize(p), "IsDir": False}]))
    elif cmd == "delete":
        d = local(rest[0])
        for base, _, files in os.walk(d):
            for n in files:
                rel = os.path.relpath(os.path.join(base, n), d)
                if n.endswith(".bkhd") or rel in ("manifest.json", "usedmap.json") or (rel.startswith("index/") and n.startswith("index-")):
                    os.remove(os.path.join(base, n))
    else:
        print("unsupported " + cmd, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
