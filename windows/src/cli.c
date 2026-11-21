/* BKHD Backup per Windows - riga di comando (bkhd-cli.exe), stesse opzioni di bkhd-engine su Linux.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "disk.h"
#include "engine.h"
#include "report.h"
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_json;

static void out_write(DWORD which, const char *text)
{
    HANDLE h = GetStdHandle(which);
    DWORD mode, w;
    if (GetConsoleMode(h, &mode)) {               /* console: testo UTF-16, accenti corretti anche su XP */
        wchar_t *t = wide(text);
        WriteConsoleW(h, t, (DWORD)wcslen(t), &w, NULL);
        free(t);
    } else {
        WriteFile(h, text, (DWORD)strlen(text), &w, NULL);
    }
}

static void outf(DWORD which, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    char *s = xvasprintf(fmt, ap);
    va_end(ap);
    out_write(which, s);
    free(s);
}

static int g_progress_shown;

static void cli_emit(Reporter *r, JVal *ev)
{
    (void)r;
    const char *type = jget_str(ev, "type");
    if (g_json) {
        char *s = jdump(ev, J_CANONICAL);
        outf(STD_OUTPUT_HANDLE, "%s\n", s);
        free(s);
        return;
    }
    if (!strcmp(type, "progress")) {
        JVal *f = jobj_get(ev, "frac"), *sp = jobj_get(ev, "speed"), *ov = jobj_get(ev, "overall");
        int64_t c = 0, n = 0;
        jget_int(ev, "chunk", &c);
        jget_int(ev, "chunks", &n);
        char *hs = human(sp ? sp->d : 0);
        outf(STD_ERROR_HANDLE, "\r[%lld/%lld] %-9s %5.1f%%  %s/s  totale %5.1f%%   ", (long long)c, (long long)n,
             jget_str(ev, "phase"), 100 * (f ? f->d : 0), hs, 100 * (ov ? ov->d : 0));
        free(hs);
        g_progress_shown = 1;
        return;
    }
    if (g_progress_shown) {
        out_write(STD_ERROR_HANDLE, "\n");
        g_progress_shown = 0;
    }
    if (!strcmp(type, "log")) {
        const char *lv = jget_str(ev, "level");
        const char *pre = lv && !strcmp(lv, "warning") ? "ATTENZIONE: " : lv && !strcmp(lv, "error") ? "ERRORE: " : "";
        outf(STD_OUTPUT_HANDLE, "%s%s\n", pre, jget_str(ev, "msg"));
    } else if (!strcmp(type, "phase")) {
        int64_t c = 0, n = 0;
        jget_int(ev, "chunk", &c);
        jget_int(ev, "chunks", &n);
        outf(STD_OUTPUT_HANDLE, "Pacchetto %lld/%lld: %s\n", (long long)c, (long long)n, jget_str(ev, "label"));
    } else if (!strcmp(type, "chunk_done")) {
        const char *m = jget_str(ev, "msg");
        if (m)
            outf(STD_OUTPUT_HANDLE, "%s\n", m);
    }
}

static BOOL WINAPI on_ctrl(DWORD t)
{
    (void)t;
    InterlockedExchange(&g_cancel, 1);
    return TRUE;
}

static char *read_password(int from_stdin, int confirm)
{
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    DWORD mode;
    if (from_stdin || !GetConsoleMode(in, &mode)) {
        StrBuf b;
        sb_init(&b);
        char c;
        DWORD n;
        while (ReadFile(in, &c, 1, &n, NULL) && n && c != '\n')
            sb_putc(&b, c);
        if (b.n && b.s[b.n - 1] == '\r')
            b.s[--b.n] = 0;
        return sb_steal(&b);
    }
    char *pw = NULL;
    for (int round = 0; round < (confirm ? 2 : 1); round++) {
        out_write(STD_ERROR_HANDLE, round ? "Ripeti la password: " : "Password del backup: ");
        SetConsoleMode(in, mode & ~(DWORD)ENABLE_ECHO_INPUT);
        wchar_t buf[512];
        DWORD n = 0;
        ReadConsoleW(in, buf, 511, &n, NULL);
        SetConsoleMode(in, mode);
        out_write(STD_ERROR_HANDLE, "\n");
        buf[n] = 0;
        while (n && (buf[n - 1] == L'\n' || buf[n - 1] == L'\r'))
            buf[--n] = 0;
        char *p = utf8(buf);
        SecureZeroMemory(buf, sizeof buf);
        if (round == 0) {
            pw = p;
        } else {
            int same = !strcmp(pw, p);
            free(p);
            if (!same) {
                free(pw);
                return NULL;
            }
        }
    }
    return pw;
}

static void usage(void)
{
    out_write(STD_OUTPUT_HANDLE,
              "BKHD Backup " BKHD_VERSION " per Windows - copia settore per settore di dischi sul cloud o su disco\n\n"
              "uso: bkhd-cli [opzioni] comando ...\n\n"
              "comandi:\n"
              "  disks                              elenca i dischi\n"
              "  backup  --source S --remote R --name N [--path BKHD] [--chunk-size 650M] [--compression zstd]\n"
              "          [--level 3] [--encrypt] [--bad-sectors retry|skip] [--resume] [--overwrite]\n"
              "  restore --remote R --path BKHD/N (--target T --yes | --verify-only) [--from-chunk N]\n"
              "          [--allow-incomplete]\n"
              "  list    --remote R [--path BKHD] [--interrupted]\n\n"
              "S: \\\\.\\PhysicalDrive1, \\\\.\\D: oppure un file immagine.  R: remote rclone (es. gdrive:) oppure una\n"
              "cartella (es. E:\\Backup, \\\\server\\condivisa).\n"
              "opzioni generali: --rclone PERCORSO  --rclone-config FILE  --retries N  --password-stdin  --json\n");
}

int main(void)
{
    int argc;
    wchar_t **wargv = CommandLineToArgvW(GetCommandLineW(), &argc);
    char **argv = xcalloc((size_t)argc + 1, sizeof(char *));
    for (int i = 0; i < argc; i++)
        argv[i] = utf8(wargv[i]);
    LocalFree(wargv);
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX);
    SetConsoleCtrlHandler(on_ctrl, TRUE);

    BackupOpts bo;
    RestoreOpts ro;
    ListOpts lo;
    backup_opts_default(&bo);
    restore_opts_default(&ro);
    memset(&lo, 0, sizeof lo);
    lo.path = "BKHD";
    const char *cmd = NULL, *rclone = NULL, *rconf = NULL;
    int retries = 10, pw_stdin = 0, chunk_set = 0;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : NULL;
#define TAKE(dst)                                                                                                 \
    do {                                                                                                          \
        if (!v) {                                                                                                 \
            outf(STD_ERROR_HANDLE, "ERRORE: manca il valore di %s\n", a);                                         \
            return 1;                                                                                             \
        }                                                                                                         \
        dst = v;                                                                                                  \
        i++;                                                                                                      \
    } while (0)
        const char *tmp = NULL;
        if (!strcmp(a, "--help") || !strcmp(a, "-h") || !strcmp(a, "/?")) {
            usage();
            return 0;
        } else if (!strcmp(a, "--version")) {
            out_write(STD_OUTPUT_HANDLE, "bkhd-cli " BKHD_VERSION "  (c) 2026 " BKHD_AUTHOR "  licenza " BKHD_LICENSE
                                         "\n");
            return 0;
        } else if (!strcmp(a, "--json")) {
            g_json = 1;
        } else if (!strcmp(a, "--rclone")) {
            TAKE(rclone);
        } else if (!strcmp(a, "--rclone-config")) {
            TAKE(rconf);
        } else if (!strcmp(a, "--retries")) {
            TAKE(tmp);
            retries = atoi(tmp);
        } else if (!strcmp(a, "--password-stdin")) {
            pw_stdin = 1;
        } else if (!strcmp(a, "--source")) {
            TAKE(bo.source);
        } else if (!strcmp(a, "--remote")) {
            TAKE(tmp);
            bo.remote = ro.remote = lo.remote = tmp;
        } else if (!strcmp(a, "--path")) {
            TAKE(tmp);
            bo.path = ro.path = lo.path = tmp;
        } else if (!strcmp(a, "--name")) {
            TAKE(bo.name);
        } else if (!strcmp(a, "--chunk-size")) {
            TAKE(tmp);
            bo.chunk_kib = parse_size_kib(tmp);
            chunk_set = 1;
            if (bo.chunk_kib < 0) {
                outf(STD_ERROR_HANDLE, "ERRORE: dimensione non valida: %s\n", tmp);
                return 1;
            }
        } else if (!strcmp(a, "--chunk-size-kib")) {
            TAKE(tmp);
            bo.chunk_kib = _atoi64(tmp);
            chunk_set = 1;
        } else if (!strcmp(a, "--chunk-size-mib")) {
            TAKE(tmp);
            bo.chunk_kib = _atoi64(tmp) * 1024;
            chunk_set = 1;
        } else if (!strcmp(a, "--compression")) {
            TAKE(bo.compression);
        } else if (!strcmp(a, "--level")) {
            TAKE(tmp);
            bo.level = atoi(tmp);
        } else if (!strcmp(a, "--encrypt")) {
            bo.encrypt = 1;
        } else if (!strcmp(a, "--bad-sectors")) {
            TAKE(bo.bad_sectors);
        } else if (!strcmp(a, "--resume")) {
            bo.resume = 1;
        } else if (!strcmp(a, "--overwrite")) {
            bo.overwrite = 1;
        } else if (!strcmp(a, "--expect-encrypted")) {
            bo.expect_encrypted = ro.expect_encrypted = 1;
        } else if (!strcmp(a, "--used-only")) {
            out_write(STD_ERROR_HANDLE, "ERRORE: 'solo settori usati' non e' ancora disponibile su Windows: usa la "
                                        "pennetta BKHD Live\n");
            return 1;
        } else if (!strcmp(a, "--target")) {
            TAKE(ro.target);
        } else if (!strcmp(a, "--verify-only")) {
            ro.verify_only = 1;
        } else if (!strcmp(a, "--from-chunk")) {
            TAKE(tmp);
            ro.from_chunk = _atoi64(tmp);
        } else if (!strcmp(a, "--allow-incomplete")) {
            ro.allow_incomplete = 1;
        } else if (!strcmp(a, "--yes")) {
            ro.yes = 1;
        } else if (!strcmp(a, "--interrupted")) {
            lo.interrupted = 1;
        } else if (!cmd && a[0] != '-') {
            cmd = a;
        } else {
            outf(STD_ERROR_HANDLE, "ERRORE: opzione sconosciuta: %s\n", a);
            return 1;
        }
#undef TAKE
    }
    (void)chunk_set;
    if (!cmd) {
        usage();
        return 1;
    }
    bo.rclone = ro.rclone = lo.rclone = rclone;
    bo.rclone_config = ro.rclone_config = lo.rclone_config = rconf;
    bo.retries = ro.retries = retries;
    if (!strcmp(cmd, "restore"))
        ro.path = bo.path == ro.path ? ro.path : ro.path;

    Reporter rep;
    rep_init(&rep, cli_emit, NULL);
    char *msg = NULL;
    int r;
    if (!strcmp(cmd, "disks")) {
        DiskInfo *d;
        int n;
        disk_list(&d, &n);
        if (!n)
            out_write(STD_OUTPUT_HANDLE, "Nessun disco trovato (servono i permessi di amministratore).\n");
        for (int i = 0; i < n; i++) {
            char *l = disk_label(&d[i]);
            outf(STD_OUTPUT_HANDLE, "%s\n   %s  settore %d B\n", l, d[i].path, d[i].logical_sector);
            free(l);
        }
        disk_list_free(d, n);
        return 0;
    } else if (!strcmp(cmd, "backup")) {
        if (!bo.source || !bo.remote || !bo.name) {
            out_write(STD_ERROR_HANDLE, "ERRORE: servono --source, --remote e --name\n");
            return 1;
        }
        char *pw = NULL;
        if (bo.encrypt || bo.resume) {
            if (bo.encrypt || pw_stdin)
                pw = read_password(pw_stdin, bo.encrypt && !pw_stdin);
            if (bo.encrypt && !pw) {
                out_write(STD_ERROR_HANDLE, "ERRORE: le password non coincidono\n");
                return 1;
            }
        }
        bo.password = pw;
        r = engine_backup(&bo, &rep, &msg);
        if (pw) {
            SecureZeroMemory(pw, strlen(pw));
            free(pw);
        }
    } else if (!strcmp(cmd, "restore")) {
        if (!ro.remote || !ro.path) {
            out_write(STD_ERROR_HANDLE, "ERRORE: servono --remote e --path\n");
            return 1;
        }
        char *pw = NULL;
        if (pw_stdin)
            pw = read_password(1, 0);
        ro.password = pw;
        r = engine_restore(&ro, &rep, &msg);
        if (r == FAIL && !pw && strstr(last_err(), "protetto da password")) {
            pw = read_password(0, 0);           /* richiesta interattiva solo se serve */
            if (pw && *pw) {
                ro.password = pw;
                r = engine_restore(&ro, &rep, &msg);
            }
        }
        if (pw) {
            SecureZeroMemory(pw, strlen(pw));
            free(pw);
        }
    } else if (!strcmp(cmd, "list")) {
        if (!lo.remote) {
            out_write(STD_ERROR_HANDLE, "ERRORE: serve --remote\n");
            return 1;
        }
        JVal *items = NULL;
        r = engine_list(&lo, &rep, &items);
        if (!r) {
            if (g_json) {
                JVal *ev = jobj();
                jobj_set(ev, "type", jstr("list"));
                jobj_set(ev, "items", items);
                char *s = jdump(ev, J_CANONICAL);
                outf(STD_OUTPUT_HANDLE, "%s\n", s);
                free(s);
                jfree(ev);
                return 0;
            }
            if (!items->n)
                out_write(STD_OUTPUT_HANDLE, "Nessun backup trovato.\n");
            for (size_t k = 0; k < items->n; k++) {
                JVal *m = items->items[k];
                JVal *src = jobj_get(m, "source");
                int64_t size = 0, cd = 0, cc = 0, sb = 0;
                jget_int(src, "size", &size);
                jget_int(m, "_chunks_done", &cd);
                jget_int(m, "chunk_count", &cc);
                jget_int(m, "_stored_bytes", &sb);
                const char *model = jget_str(src, "model");
                char *hs = human((double)size), *hb = human((double)sb);
                JVal *sp = jobj_get(m, "sparse");
                outf(STD_OUTPUT_HANDLE, "%s\n   %-11s %s  sorgente %s (%s), %lld/%lld pacchetti, %s occupati, "
                                        "compressione %s%s\n",
                     jget_str(m, "_dir"), jget_str(m, "status"), jget_str(m, "created"), hs,
                     model && *model ? model : jget_str(src, "path"), (long long)cd, (long long)cc, hb,
                     jget_str(m, "compression"), sp && sp->t == J_BOOL && sp->b ? ", solo settori usati" : "");
                free(hs);
                free(hb);
            }
            jfree(items);
        }
    } else {
        outf(STD_ERROR_HANDLE, "ERRORE: comando sconosciuto: %s\n", cmd);
        return 1;
    }
    if (g_progress_shown)
        out_write(STD_ERROR_HANDLE, "\n");
    if (r == OK) {
        if (msg)
            outf(STD_OUTPUT_HANDLE, "%s\n", msg);
        free(msg);
        return 0;
    }
    if (r == CANCELLED) {
        out_write(STD_OUTPUT_HANDLE, !strcmp(cmd, "backup")
                                         ? "Operazione annullata. Il backup puo' essere ripreso con l'opzione 'Riprendi'.\n"
                                         : "Operazione annullata.\n");
        return 2;
    }
    outf(STD_ERROR_HANDLE, "ERRORE: %s\n", last_err());
    return 1;
}
