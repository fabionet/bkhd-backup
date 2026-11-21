/* BKHD Backup per Windows - rclone (vedi rclone.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "rclone.h"
#include "json.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *const RCLONE_LOG[] = {"--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE",
                                         "--low-level-retries", "20", NULL};

char *remote_join(const char *remote, const char *a, const char *b)
{
    StrBuf path;
    sb_init(&path);
    const char *parts[2] = {a, b};
    for (int i = 0; i < 2; i++) {
        const char *p = parts[i];
        if (!p)
            continue;
        while (*p == '/')
            p++;
        size_t n = strlen(p);
        while (n && p[n - 1] == '/')
            n--;
        if (!n)
            continue;
        if (path.n)
            sb_putc(&path, '/');
        sb_putn(&path, p, n);
    }
    while (*remote == ' ')
        remote++;
    size_t rl = strlen(remote);
    while (rl && remote[rl - 1] == ' ')
        rl--;
    char *r;
    if (!path.n)
        r = xasprintf("%.*s", (int)rl, remote);
    else if (rl && remote[rl - 1] == ':')
        r = xasprintf("%.*s%s", (int)rl, remote, path.s);
    else {
        while (rl && remote[rl - 1] == '/')
            rl--;
        r = xasprintf("%.*s/%s", (int)rl, remote, path.s);
    }
    sb_free(&path);
    return r;
}

char *rc_find(void)
{
    char *dir = exe_dir();
    char *p = xasprintf("%s\\rclone.exe", dir);
    free(dir);
    wchar_t *w = wide(p);
    DWORD a = GetFileAttributesW(w);
    free(w);
    if (a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY))
        return p;
    free(p);
    wchar_t found[MAX_PATH];
    if (SearchPathW(NULL, L"rclone.exe", NULL, MAX_PATH, found, NULL))
        return utf8(found);
    return NULL;
}

int rc_init(Rclone *rc, const char *binary, const char *config)
{
    memset(rc, 0, sizeof *rc);
    OSVERSIONINFOW vi;
    memset(&vi, 0, sizeof vi);
    vi.dwOSVersionInfoSize = sizeof vi;
    if (GetVersionExW(&vi) && vi.dwMajorVersion < 6) {
        set_err("il backup sul cloud richiede Windows 7 o successivo (rclone non funziona su Windows XP): "
                "salva su un disco o su una cartella di rete");
        return FAIL;
    }
    rc->binary = binary && *binary ? xstrdup(binary) : rc_find();
    if (!rc->binary) {
        set_err("rclone.exe non trovato: deve stare nella stessa cartella di BKHD Backup");
        return FAIL;
    }
    rc->config = config && *config ? xstrdup(config) : NULL;
    return OK;
}

void rc_free(Rclone *rc)
{
    free(rc->binary);
    free(rc->config);
    memset(rc, 0, sizeof *rc);
}

/* --- riga di comando (regole di CommandLineToArgvW) ---------------------------------------------- */
static void quote_arg(StrBuf *b, const char *a)
{
    if (*a && !strpbrk(a, " \t\n\v\"")) {
        sb_puts(b, a);
        return;
    }
    sb_putc(b, '"');
    for (const char *p = a;; p++) {
        int bs = 0;
        while (*p == '\\') {
            p++;
            bs++;
        }
        if (!*p) {
            for (int i = 0; i < bs * 2; i++)
                sb_putc(b, '\\');
            break;
        }
        if (*p == '"') {
            for (int i = 0; i < bs * 2 + 1; i++)
                sb_putc(b, '\\');
        } else {
            for (int i = 0; i < bs; i++)
                sb_putc(b, '\\');
        }
        sb_putc(b, *p);
    }
    sb_putc(b, '"');
}

static wchar_t *build_cmdline(Rclone *rc, const char *const *args)
{
    StrBuf b;
    sb_init(&b);
    quote_arg(&b, rc->binary);
    if (rc->config) {
        sb_puts(&b, " --config ");
        quote_arg(&b, rc->config);
    }
    for (int i = 0; args[i]; i++) {
        sb_putc(&b, ' ');
        quote_arg(&b, args[i]);
    }
    wchar_t *w = wide(b.s);
    sb_free(&b);
    return w;
}

/* --- esecuzione con flussi ----------------------------------------------------------------------- */
typedef struct {
    HANDLE proc;
    HANDLE out_r, err_r;
    CRITICAL_SECTION cs;
    char *errs[3];
    int nerrs;
    ProgressFn fn;
    void *ctx;
    uint8_t *buf;            /* destinazione dello standard output (download) */
    size_t max, got;
    StrBuf *sb;              /* oppure testo (cat, lsf) */
    int overflow;
    ProgressFn dl_fn;        /* avanzamento del download (byte ricevuti) */
    void *dl_ctx;
} Run;

static void add_err(Run *r, const char *msg)
{
    EnterCriticalSection(&r->cs);
    if (r->nerrs == 3) {
        free(r->errs[0]);
        memmove(r->errs, r->errs + 1, 2 * sizeof(char *));
        r->nerrs = 2;
    }
    r->errs[r->nerrs++] = xasprintf("%.300s", msg);
    LeaveCriticalSection(&r->cs);
}

static void handle_line(Run *r, char *line)
{
    size_t n = strlen(line);
    while (n && (line[n - 1] == '\r' || line[n - 1] == ' '))
        line[--n] = 0;
    if (!n)
        return;
    JVal *d = jparse(line, n);
    if (!d || d->t != J_OBJ) {
        add_err(r, line);
        jfree(d);
        return;
    }
    JVal *st = jobj_get(d, "stats");
    const char *level = jget_str(d, "level");
    if (st && st->t == J_OBJ) {
        if (r->fn) {
            int64_t done = 0, total = 0;
            jget_int(st, "bytes", &done);
            jget_int(st, "totalBytes", &total);
            JVal *sp = jobj_get(st, "speed");
            double speed = sp ? (sp->t == J_FLOAT ? sp->d : sp->t == J_INT ? (double)sp->i : 0) : 0;
            r->fn(r->ctx, done, total, speed);
        }
    } else if (level && (!strcmp(level, "error") || !strcmp(level, "critical") || !strcmp(level, "fatal"))) {
        const char *msg = jget_str(d, "msg");
        add_err(r, msg ? msg : line);
    }
    jfree(d);
}

static DWORD WINAPI stderr_thread(LPVOID p)
{
    Run *r = p;
    StrBuf line;
    sb_init(&line);
    char buf[4096];
    DWORD n;
    while (ReadFile(r->err_r, buf, sizeof buf, &n, NULL) && n) {
        for (DWORD i = 0; i < n; i++) {
            if (buf[i] == '\n') {
                if (line.s)
                    handle_line(r, line.s);
                line.n = 0;
                if (line.s)
                    line.s[0] = 0;
            } else if (line.n < 65536) {
                sb_putc(&line, buf[i]);
            }
        }
    }
    if (line.n)
        handle_line(r, line.s);
    sb_free(&line);
    return 0;
}

static DWORD WINAPI stdout_thread(LPVOID p)
{
    Run *r = p;
    char tmp[65536];
    for (;;) {
        DWORD n = 0;
        if (r->buf) {
            if (r->got >= r->max) {           /* il file e' piu' grande del previsto */
                char probe;
                if (ReadFile(r->out_r, &probe, 1, &n, NULL) && n) {
                    r->overflow = 1;
                    TerminateProcess(r->proc, 1);
                }
                break;
            }
            DWORD want = (DWORD)(r->max - r->got > (1u << 20) ? (1u << 20) : r->max - r->got);
            if (!ReadFile(r->out_r, r->buf + r->got, want, &n, NULL) || !n)
                break;
            r->got += n;
            if (r->dl_fn)
                r->dl_fn(r->dl_ctx, (int64_t)r->got, (int64_t)r->max, 0);
        } else {
            if (!ReadFile(r->out_r, tmp, sizeof tmp, &n, NULL) || !n)
                break;
            if (r->sb) {
                if (r->sb->n + n > r->max) {
                    r->overflow = 1;
                    TerminateProcess(r->proc, 1);
                    break;
                }
                sb_putn(r->sb, tmp, n);
            }
        }
    }
    return 0;
}

/* esegue rclone; stdin_data puo' essere NULL. Restituisce il codice di uscita (-1 se non avviato,
 * -2 se annullato) e mette in *err i messaggi di errore di rclone. */
static int run_rclone(Rclone *rc, const char *const *args, const uint8_t *stdin_data, size_t stdin_len, Run *r,
                      DWORD timeout_ms, char **err)
{
    SECURITY_ATTRIBUTES sa = {sizeof sa, NULL, TRUE};
    HANDLE in_r = NULL, in_w = NULL, out_r = NULL, out_w = NULL, err_r = NULL, err_w = NULL;
    if (err)
        *err = NULL;
    if (!CreatePipe(&out_r, &out_w, &sa, 1 << 20) || !CreatePipe(&err_r, &err_w, &sa, 1 << 16)) {
        set_win_err(GetLastError(), "impossibile avviare rclone");
        return -1;
    }
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);
    if (stdin_data) {
        CreatePipe(&in_r, &in_w, &sa, 1 << 20);
        SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
    } else {
        in_r = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    }
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_r;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    wchar_t *cmd = build_cmdline(rc, args);
    BOOL ok = CreateProcessW(NULL, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
    DWORD cerr = GetLastError();
    free(cmd);
    CloseHandle(in_r);
    CloseHandle(out_w);
    CloseHandle(err_w);
    if (!ok) {
        CloseHandle(out_r);
        CloseHandle(err_r);
        if (in_w)
            CloseHandle(in_w);
        set_win_err(cerr, "impossibile avviare rclone (%s)", rc->binary);
        if (err)
            *err = xstrdup(last_err());
        return -1;
    }
    CloseHandle(pi.hThread);
    r->proc = pi.hProcess;
    r->out_r = out_r;
    r->err_r = err_r;
    InitializeCriticalSection(&r->cs);
    HANDLE t_err = CreateThread(NULL, 0, stderr_thread, r, 0, NULL);
    HANDLE t_out = CreateThread(NULL, 0, stdout_thread, r, 0, NULL);
    int was_cancelled = 0;
    if (stdin_data) {
        size_t pos = 0;
        while (pos < stdin_len) {
            if (cancelled()) {
                was_cancelled = 1;
                TerminateProcess(pi.hProcess, 1);
                break;
            }
            DWORD k = (DWORD)(stdin_len - pos > (1u << 20) ? (1u << 20) : stdin_len - pos), w = 0;
            if (!WriteFile(in_w, stdin_data + pos, k, &w, NULL) || !w)
                break;                          /* rclone ha chiuso l'ingresso: l'errore arriva dal suo codice */
            pos += w;
        }
        CloseHandle(in_w);
    }
    DWORD t0 = GetTickCount();
    for (;;) {
        if (WaitForSingleObject(pi.hProcess, 200) == WAIT_OBJECT_0)
            break;
        if (cancelled()) {
            was_cancelled = 1;
            TerminateProcess(pi.hProcess, 1);
        } else if (timeout_ms && GetTickCount() - t0 > timeout_ms) {
            TerminateProcess(pi.hProcess, 1);
            add_err(r, "rclone non ha risposto in tempo");
        }
    }
    WaitForSingleObject(t_out, 15000);
    WaitForSingleObject(t_err, 15000);
    CloseHandle(t_out);
    CloseHandle(t_err);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(out_r);
    CloseHandle(err_r);
    if (err) {
        StrBuf e;
        sb_init(&e);
        for (int i = 0; i < r->nerrs; i++) {
            if (i)
                sb_puts(&e, " | ");
            sb_puts(&e, r->errs[i]);
        }
        if (!e.n)
            sb_printf(&e, "rclone ha restituito il codice %lu", (unsigned long)code);
        *err = sb_steal(&e);
    }
    for (int i = 0; i < r->nerrs; i++)
        free(r->errs[i]);
    DeleteCriticalSection(&r->cs);
    if (was_cancelled) {
        set_err("operazione annullata");
        return -2;
    }
    return (int)code;
}

static int capture(Rclone *rc, const char *const *args, StrBuf *out, size_t max, DWORD timeout, char **err)
{
    Run r;
    memset(&r, 0, sizeof r);
    r.sb = out;
    r.max = max;
    int code = run_rclone(rc, args, NULL, 0, &r, timeout, err);
    if (r.overflow && code >= 0) {
        free(*err);
        *err = xstrdup("file troppo grande");
        return 1;
    }
    return code;
}

int rc_listremotes(Rclone *rc, StrList *out)
{
    const char *args[] = {"listremotes", NULL};
    StrBuf b;
    sb_init(&b);
    char *err = NULL;
    int code = capture(rc, args, &b, 1 << 20, 30000, &err);
    if (code != 0) {
        set_err("rclone listremotes: %s", err ? err : "errore");
        free(err);
        sb_free(&b);
        return FAIL;
    }
    free(err);
    for (char *line = strtok(b.s ? b.s : (char *)"", "\r\n"); line; line = strtok(NULL, "\r\n"))
        if (*line)
            sl_push(out, line);
    sb_free(&b);
    return OK;
}

int rc_list_names(Rclone *rc, const char *dir, int dirs, StrList *out)
{
    const char *args[] = {"lsf", dirs ? "--dirs-only" : "--files-only", dir, NULL};
    StrBuf b;
    sb_init(&b);
    char *err = NULL;
    int code = capture(rc, args, &b, 64 << 20, 300000, &err);
    if (code == 3) {                              /* cartella inesistente */
        free(err);
        sb_free(&b);
        return OK;
    }
    if (code != 0) {
        if (code != -2)
            set_err("impossibile elencare %s: %s", dir, err ? err : "errore");
        free(err);
        sb_free(&b);
        return code == -2 ? CANCELLED : FAIL;
    }
    free(err);
    for (char *line = strtok(b.s ? b.s : (char *)"", "\r\n"); line; line = strtok(NULL, "\r\n")) {
        size_t n = strlen(line);
        if (n && line[n - 1] == '/')
            line[--n] = 0;
        if (n)
            sl_push(out, line);
    }
    sb_free(&b);
    return OK;
}

int rc_cat(Rclone *rc, const char *file, int64_t max, StrBuf *out)
{
    const char *args[] = {"cat", "--use-json-log", file, NULL};
    char *err = NULL;
    int code = capture(rc, args, out, (size_t)max, 600000, &err);
    if (code != 0) {
        if (code != -2)
            set_err("impossibile leggere %s: %s", file, err ? err : "errore");
        free(err);
        return code == -2 ? CANCELLED : FAIL;
    }
    free(err);
    return OK;
}

static int with_log(const char *const *head, const char *tail, const char **argv, int max)
{
    int n = 0;
    for (int i = 0; head[i] && n < max - 1; i++)
        argv[n++] = head[i];
    for (int i = 0; RCLONE_LOG[i] && n < max - 1; i++)
        argv[n++] = RCLONE_LOG[i];
    argv[n++] = tail;
    argv[n] = NULL;
    return n;
}

int rc_upload(Rclone *rc, const char *remote, const uint8_t *data, size_t n, ProgressFn fn, void *ctx, char **err)
{
    char size[32];
    snprintf(size, sizeof size, "%llu", (unsigned long long)n);
    const char *head[] = {"rcat", "--size", size, NULL};
    const char *argv[20];
    with_log(head, remote, argv, 20);
    Run r;
    memset(&r, 0, sizeof r);
    r.fn = fn;
    r.ctx = ctx;
    int code = run_rclone(rc, argv, data, n, &r, 0, err);
    if (code == -2)
        return CANCELLED;
    return code == 0;
}

int rc_download(Rclone *rc, const char *remote, uint8_t *buf, size_t max, size_t *got, ProgressFn fn, void *ctx,
                char **err)
{
    const char *head[] = {"cat", NULL};
    const char *argv[20];
    with_log(head, remote, argv, 20);
    Run r;
    memset(&r, 0, sizeof r);
    r.buf = buf;
    r.max = max;
    r.dl_fn = fn;            /* avanzamento dai byte ricevuti: rclone cat non conosce il totale */
    r.dl_ctx = ctx;
    int code = run_rclone(rc, argv, NULL, 0, &r, 0, err);
    *got = r.got;
    if (code == -2)
        return CANCELLED;
    if (r.overflow) {
        free(*err);
        *err = xasprintf("%s: dati piu' grandi del previsto", remote);
        return 0;
    }
    return code == 0;
}

int rc_remote_size(Rclone *rc, const char *file, int64_t *size)
{
    const char *args[] = {"lsjson", "--no-mimetype", "--no-modtime", file, NULL};
    StrBuf b;
    sb_init(&b);
    char *err = NULL;
    int code = capture(rc, args, &b, 16 << 20, 300000, &err);
    free(err);
    *size = -1;
    if (code != 0) {
        sb_free(&b);
        return FAIL;
    }
    JVal *items = jparse(b.s ? b.s : "[]", b.n);
    sb_free(&b);
    if (!items || items->t != J_ARR) {
        jfree(items);
        return FAIL;
    }
    const char *name = strrchr(file, '/');
    name = name ? name + 1 : (strrchr(file, ':') ? strrchr(file, ':') + 1 : file);
    for (size_t i = 0; i < items->n; i++) {
        const char *nm = jget_str(items->items[i], "Name");
        int64_t s;
        if ((nm && !strcmp(nm, name)) || items->n == 1)
            if (!jget_int(items->items[i], "Size", &s)) {
                *size = s;
                break;
            }
    }
    jfree(items);
    return *size >= 0 ? OK : FAIL;
}

int rc_delete_backup_files(Rclone *rc, const char *dir)
{
    const char *args[] = {"delete", dir, "--include", "*.bkhd", "--include", "manifest.json", "--include",
                          "/index/index-*.json", "--include", "usedmap.json", NULL};
    StrBuf b;
    sb_init(&b);
    char *err = NULL;
    int code = capture(rc, args, &b, 1 << 20, 3600000, &err);
    free(err);
    sb_free(&b);
    return code == 0 || code == 3 ? OK : FAIL;
}

int rc_open_config_console(Rclone *rc)
{
    const char *args[] = {"config", NULL};
    wchar_t *cmd = build_cmdline(rc, args);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    BOOL ok = CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NEW_CONSOLE, NULL, NULL, &si, &pi);
    free(cmd);
    if (!ok) {
        set_win_err(GetLastError(), "impossibile avviare rclone config");
        return FAIL;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return OK;
}
