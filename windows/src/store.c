/* BKHD Backup per Windows - destinazioni (vedi store.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "store.h"
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*TryFn)(void *ctx, char **err);     /* 1 = riuscito, 0 = fallito (err), CANCELLED */

static int with_retries(Store *s, const char *what, TryFn fn, void *ctx)
{
    int delay = 15;
    for (int attempt = 1; attempt <= s->retries; attempt++) {
        if (cancelled()) {
            set_err("operazione annullata");
            return CANCELLED;
        }
        char *err = NULL;
        int r = fn(ctx, &err);
        if (r == CANCELLED) {
            free(err);
            set_err("operazione annullata");
            return CANCELLED;
        }
        if (r == 1) {
            free(err);
            return OK;
        }
        if (attempt == s->retries) {
            set_err("%s non riuscito dopo %d tentativi: %s", what, s->retries, err ? err : "errore");
            free(err);
            return FAIL;
        }
        rep_log(s->rep, "warning", "%s: tentativo %d/%d fallito (%s). Nuovo tentativo tra %d s...", what, attempt,
                s->retries, err ? err : "errore", delay);
        free(err);
        if (cancel_wait((DWORD)delay * 1000)) {
            set_err("operazione annullata");
            return CANCELLED;
        }
        delay = delay * 2 > 300 ? 300 : delay * 2;
    }
    return FAIL;
}

static char *slashes_to_back(const char *p)
{
    char *r = xstrdup(p);
    for (char *q = r; *q; q++)
        if (*q == '/')
            *q = '\\';
    return r;
}

static void add_parts(StrBuf *b, const char *part)
{
    if (!part)
        return;
    char *p = slashes_to_back(part);
    for (char *tok = strtok(p, "\\"); tok; tok = strtok(NULL, "\\")) {
        if (!*tok)
            continue;
        if (b->n && b->s[b->n - 1] != '\\')
            sb_putc(b, '\\');
        sb_puts(b, tok);
    }
    free(p);
}

int store_open(Store *s, const char *remote, const char *a, const char *b, Rclone *rc, Reporter *rep, int retries)
{
    memset(s, 0, sizeof *s);
    s->rep = rep;
    s->retries = retries > 0 ? retries : 10;
    const char *parts[2] = {a, b};
    for (int i = 0; i < 2; i++) {
        if (!parts[i])
            continue;
        char *p = slashes_to_back(parts[i]);
        for (char *tok = strtok(p, "\\"); tok; tok = strtok(NULL, "\\"))
            if (!strcmp(tok, ".") || !strcmp(tok, "..")) {
                free(p);
                set_err("percorso del backup non valido");
                return FAIL;
            }
        free(p);
    }
    if (path_is_local(remote)) {
        s->local = 1;
        StrBuf base;
        sb_init(&base);
        char *root = slashes_to_back(remote);
        size_t n = strlen(root);
        while (n > 3 && root[n - 1] == '\\')
            root[--n] = 0;
        sb_puts(&base, root);
        free(root);
        add_parts(&base, a);
        add_parts(&base, b);
        s->base = sb_steal(&base);
    } else {
        if (!rc) {
            set_err("rclone non disponibile");
            return FAIL;
        }
        s->rc = rc;
        s->base = remote_join(remote, a, b);
    }
    return OK;
}

void store_close(Store *s)
{
    free(s->base);
    s->base = NULL;
}

char *store_where(const Store *s, const char *rel)
{
    if (!rel || !*rel)
        return xstrdup(s->base);
    if (s->local) {
        StrBuf b;
        sb_init(&b);
        sb_puts(&b, s->base);
        add_parts(&b, rel);
        return sb_steal(&b);
    }
    return remote_join(s->base, rel, NULL);
}

static int ensure_dir(const char *path)
{
    wchar_t *w = wide(path);
    int r = SHCreateDirectoryExW(NULL, w, NULL);
    free(w);
    if (r != ERROR_SUCCESS && r != ERROR_ALREADY_EXISTS && r != ERROR_FILE_EXISTS) {
        set_win_err((DWORD)r, "impossibile creare la cartella %s", path);
        return FAIL;
    }
    return OK;
}

static char *parent_of(const char *path)
{
    char *p = xstrdup(path);
    char *slash = strrchr(p, '\\');
    if (slash)
        *slash = 0;
    return p;
}

int store_list_names(Store *s, const char *rel, int dirs, StrList *out)
{
    char *dir = store_where(s, rel);
    if (!s->local) {
        int r = rc_list_names(s->rc, dir, dirs, out);
        free(dir);
        return r;
    }
    char *pat = xasprintf("%s\\*", dir);
    free(dir);
    wchar_t *w = wide(pat);
    free(pat);
    WIN32_FIND_DATAW fd;
    HANDLE f = FindFirstFileW(w, &fd);
    free(w);
    if (f == INVALID_HANDLE_VALUE)
        return OK;                                   /* cartella inesistente: nessun nome */
    do {
        char *n = utf8(fd.cFileName);
        size_t l = strlen(n);
        int skip = n[0] == '.' || (l > 8 && !strcmp(n + l - 8, ".partial")) || (l > 4 && !strcmp(n + l - 4, ".tmp")) ||
                   (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT);
        int is_dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (!skip && is_dir == (dirs != 0))
            sl_push(out, n);
        free(n);
    } while (FindNextFileW(f, &fd));
    FindClose(f);
    sl_sort(out);
    return OK;
}

static int read_local(const char *path, StrBuf *out, int64_t max)
{
    wchar_t *w = wide(path);
    HANDLE h = CreateFileW(w, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                           FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    free(w);
    if (h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile leggere %s", path);
        return FAIL;
    }
    LARGE_INTEGER sz;
    BY_HANDLE_FILE_INFORMATION fi;
    if (!GetFileSizeEx(h, &sz) || !GetFileInformationByHandle(h, &fi) ||
        (fi.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) || sz.QuadPart > max) {
        CloseHandle(h);
        set_err("%s: file non valido o troppo grande", path);
        return FAIL;
    }
    char buf[65536];
    DWORD n;
    while (ReadFile(h, buf, sizeof buf, &n, NULL) && n) {
        sb_putn(out, buf, n);
        if ((int64_t)out->n > max) {
            CloseHandle(h);
            set_err("%s: file troppo grande", path);
            return FAIL;
        }
    }
    CloseHandle(h);
    return OK;
}

int store_read(Store *s, const char *rel, StrBuf *out)
{
    char *path = store_where(s, rel);
    int r = s->local ? read_local(path, out, MAX_META) : rc_cat(s->rc, path, MAX_META, out);
    free(path);
    return r;
}

typedef struct {
    Rclone *rc;
    const char *remote;
    const uint8_t *data;
    size_t n;
    ProgressFn fn;
    void *ctx;
} UpCtx;

static int try_upload(void *p, char **err)
{
    UpCtx *u = p;
    return rc_upload(u->rc, u->remote, u->data, u->n, u->fn, u->ctx, err);
}

static int write_file_atomic(const char *path, const void *data, size_t n)
{
    char *dir = parent_of(path);
    if (ensure_dir(dir)) {
        free(dir);
        return FAIL;
    }
    const char *name = strrchr(path, '\\') ? strrchr(path, '\\') + 1 : path;
    char *tmp = xasprintf("%s\\.%s.%lu.tmp", dir, name, (unsigned long)GetCurrentProcessId());
    free(dir);
    wchar_t *wt = wide(tmp), *wf = wide(path);
    HANDLE h = CreateFileW(wt, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    int r = FAIL;
    if (h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile scrivere %s", tmp);
    } else {
        DWORD w = 0;
        BOOL ok = WriteFile(h, data, (DWORD)n, &w, NULL) && w == n && FlushFileBuffers(h);
        DWORD e = GetLastError();
        CloseHandle(h);
        if (!ok)
            set_win_err(e, "impossibile scrivere %s", tmp);
        else if (!MoveFileExW(wt, wf, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
            set_win_err(GetLastError(), "impossibile sostituire %s", path);
        else
            r = OK;
        if (r)
            DeleteFileW(wt);
    }
    free(wt);
    free(wf);
    free(tmp);
    return r;
}

int store_write_json(Store *s, const char *rel, const JVal *data, const char *what)
{
    char *body = jdump(data, J_PRETTY);
    char *path = store_where(s, rel);
    int r;
    if (s->local) {
        r = write_file_atomic(path, body, strlen(body));
        if (r) {
            char *m = xstrdup(last_err());
            set_err("%s: %s", what, m);
            free(m);
        }
    } else {
        UpCtx u = {s->rc, path, (const uint8_t *)body, strlen(body), NULL, NULL};
        r = with_retries(s, what, try_upload, &u);
    }
    free(path);
    free(body);
    return r;
}

int store_prepare(Store *s)
{
    return s->local ? ensure_dir(s->base) : OK;
}

int store_check_space(Store *s, int64_t need)
{
    if (!s->local)
        return OK;
    wchar_t *w = wide(s->base);
    ULARGE_INTEGER avail;
    BOOL ok = GetDiskFreeSpaceExW(w, &avail, NULL, NULL);
    free(w);
    if (ok && (int64_t)avail.QuadPart < need + 16 * MIB) {
        char *a = human((double)avail.QuadPart), *b = human((double)need);
        set_err("spazio esaurito sulla destinazione %s: liberi %s, servono almeno %s per il prossimo pacchetto",
                s->base, a, b);
        free(a);
        free(b);
        return FAIL;
    }
    return OK;
}

static int is_backup_file(const char *n)
{
    size_t l = strlen(n);
    return (l > 5 && !strcmp(n + l - 5, ".bkhd")) || (l > 13 && !strcmp(n + l - 13, ".bkhd.partial")) ||
           !strcmp(n, "manifest.json") || !strcmp(n, "usedmap.json") ||
           (!strncmp(n, "index-", 6) && l > 5 && !strcmp(n + l - 5, ".json"));
}

static void clean_dir(const char *dir, int top)
{
    char *pat = xasprintf("%s\\*", dir);
    wchar_t *w = wide(pat);
    free(pat);
    WIN32_FIND_DATAW fd;
    HANDLE f = FindFirstFileW(w, &fd);
    free(w);
    if (f == INVALID_HANDLE_VALUE)
        return;
    do {
        char *n = utf8(fd.cFileName);
        char *full = xasprintf("%s\\%s", dir, n);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
            /* collegamenti: mai seguiti */
        } else if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            int grp = n[0] == 'g' && strlen(n) == 6 && strspn(n + 1, "0123456789") == 5;
            if (top && (!strcmp(n, "index") || grp))
                clean_dir(full, 0);
        } else if (is_backup_file(n)) {
            wchar_t *wf = wide(full);
            DeleteFileW(wf);
            free(wf);
        }
        free(full);
        free(n);
    } while (FindNextFileW(f, &fd));
    FindClose(f);
}

int store_delete_backup_files(Store *s)
{
    if (!s->local)
        return rc_delete_backup_files(s->rc, s->base);
    clean_dir(s->base, 1);
    return OK;
}

/* --- pacchetti in scrittura ---------------------------------------------------------------------- */
int store_chunk_create(Store *s, const char *rel, uint8_t *mem, size_t cap, ChunkOut *o)
{
    memset(o, 0, sizeof *o);
    o->local = s->local;
    if (!s->local) {
        o->mem = mem;
        o->cap = cap;
        return OK;
    }
    char *path = store_where(s, rel);
    char *dir = parent_of(path);
    int r = ensure_dir(dir);
    free(dir);
    if (r) {
        free(path);
        return FAIL;
    }
    char *partial = xasprintf("%s.partial", path);
    o->final = wide(path);
    o->partial = wide(partial);
    free(path);
    o->h = CreateFileW(o->partial, GENERIC_READ | GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (o->h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile creare %s", partial);
        free(partial);
        store_chunk_discard(o);
        return FAIL;
    }
    free(partial);
    return OK;
}

int chunkout_write(ChunkOut *o, const void *data, size_t n)
{
    if (!o->local) {
        if (o->len + n > o->cap) {
            set_err("il pacchetto e' piu' grande della memoria riservata");
            return FAIL;
        }
        memcpy(o->mem + o->len, data, n);
        o->len += n;
        return OK;
    }
    const uint8_t *p = data;
    while (n) {
        DWORD k = (DWORD)(n > (64u << 20) ? (64u << 20) : n), w = 0;
        if (!WriteFile(o->h, p, k, &w, NULL) || !w) {
            set_win_err(GetLastError(), "scrittura del pacchetto non riuscita");
            return FAIL;
        }
        p += w;
        n -= w;
        o->len += w;
    }
    return OK;
}

int chunkout_header(ChunkOut *o, const uint8_t *hdr, size_t n)
{
    if (!o->local) {
        memcpy(o->mem, hdr, n);
        return OK;
    }
    OVERLAPPED ov;
    memset(&ov, 0, sizeof ov);
    DWORD w = 0;
    if (!WriteFile(o->h, hdr, (DWORD)n, &w, &ov) || w != n) {
        set_win_err(GetLastError(), "scrittura dell'intestazione non riuscita");
        return FAIL;
    }
    return OK;
}

int store_chunk_commit(Store *s, ChunkOut *o, const char *rel, ProgressFn fn, void *ctx, int verify)
{
    if (o->local) {
        int r = OK;
        if (!FlushFileBuffers(o->h)) {                 /* dati davvero sul disco prima di dichiararli salvati */
            set_win_err(GetLastError(), "scrittura del pacchetto %s non riuscita", rel);
            r = FAIL;
        }
        CloseHandle(o->h);
        o->h = NULL;
        if (!r && !MoveFileExW(o->partial, o->final, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            set_win_err(GetLastError(), "impossibile completare il pacchetto %s", rel);
            r = FAIL;
        }
        if (r)
            DeleteFileW(o->partial);
        else if (fn)
            fn(ctx, (int64_t)o->len, (int64_t)o->len, 0);
        free(o->partial);
        free(o->final);
        o->partial = o->final = NULL;
        return r;
    }
    char *remote = store_where(s, rel);
    char *what = xasprintf("Upload di %s", rel);
    int r = FAIL;
    for (int round = 0; round < 2; round++) {
        UpCtx u = {s->rc, remote, o->mem, o->len, fn, ctx};
        r = with_retries(s, what, try_upload, &u);
        if (r || !verify)
            break;
        int64_t rsize = -1;
        if (rc_remote_size(s->rc, remote, &rsize) || rsize == (int64_t)o->len)
            break;
        rep_log(s->rep, "warning", "Dimensione remota di %s errata (%lld invece di %llu): ricarico.", rel,
                (long long)rsize, (unsigned long long)o->len);
        r = FAIL;
        set_err("il pacchetto %s non risulta caricato correttamente sul cloud", rel);
    }
    free(what);
    free(remote);
    return r;
}

void store_chunk_discard(ChunkOut *o)
{
    if (o->h && o->h != INVALID_HANDLE_VALUE)
        CloseHandle(o->h);
    if (o->partial)
        DeleteFileW(o->partial);
    free(o->partial);
    free(o->final);
    memset(o, 0, sizeof *o);
}

/* --- pacchetti in lettura ------------------------------------------------------------------------ */
typedef struct {
    Rclone *rc;
    const char *remote;
    uint8_t *mem;
    size_t cap, got;
    ProgressFn fn;
    void *ctx;
} DownCtx;

static int try_download(void *p, char **err)
{
    DownCtx *d = p;
    return rc_download(d->rc, d->remote, d->mem, d->cap, &d->got, d->fn, d->ctx, err);
}

int store_chunk_fetch(Store *s, const char *rel, int64_t max, uint8_t *mem, size_t memcap, ProgressFn fn, void *ctx,
                      ChunkIn *in)
{
    memset(in, 0, sizeof *in);
    char *path = store_where(s, rel);
    if (!s->local) {
        char *what = xasprintf("Download di %s", rel);
        DownCtx d = {s->rc, path, mem, (size_t)max < memcap ? (size_t)max : memcap, 0, fn, ctx};
        int r = with_retries(s, what, try_download, &d);
        free(what);
        free(path);
        if (r)
            return r;
        in->mem = mem;
        in->size = (int64_t)d.got;
        return OK;
    }
    wchar_t *w = wide(path);
    /* nessun altro programma puo' modificare il file mentre lo si verifica e lo si scrive */
    in->h = CreateFileW(w, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                        FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    free(w);
    if (in->h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "pacchetto mancante o non valido: %s", path);
        free(path);
        in->h = NULL;
        return FAIL;
    }
    LARGE_INTEGER sz;
    BY_HANDLE_FILE_INFORMATION fi;
    if (!GetFileSizeEx(in->h, &sz) || !GetFileInformationByHandle(in->h, &fi) ||
        (fi.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) || sz.QuadPart > max) {
        set_err("pacchetto non valido: %s", path);
        free(path);
        chunkin_close(in);
        return FAIL;
    }
    free(path);
    in->size = sz.QuadPart;
    if (fn)
        fn(ctx, in->size, in->size, 0);
    return OK;
}

int chunkin_read_at(ChunkIn *in, int64_t off, void *buf, size_t n)
{
    if (off < 0 || off + (int64_t)n > in->size) {
        set_err("lettura oltre la fine del pacchetto");
        return FAIL;
    }
    if (in->mem) {
        memcpy(buf, in->mem + off, n);
        return OK;
    }
    size_t got = 0;
    while (got < n) {
        OVERLAPPED ov;
        memset(&ov, 0, sizeof ov);
        uint64_t pos = (uint64_t)off + got;
        ov.Offset = (DWORD)pos;
        ov.OffsetHigh = (DWORD)(pos >> 32);
        DWORD k = (DWORD)(n - got > (64u << 20) ? (64u << 20) : n - got), r = 0;
        if (!ReadFile(in->h, (char *)buf + got, k, &r, &ov) || !r) {
            set_win_err(GetLastError(), "lettura del pacchetto non riuscita");
            return FAIL;
        }
        got += r;
    }
    return OK;
}

void chunkin_close(ChunkIn *in)
{
    if (in->h && in->h != INVALID_HANDLE_VALUE)
        CloseHandle(in->h);
    memset(in, 0, sizeof *in);
}
