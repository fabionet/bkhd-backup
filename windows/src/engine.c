/* BKHD Backup per Windows - motore (vedi engine.h). Traduzione fedele di bkhd_engine.py: stessi file,
 * stessi nomi, stesse intestazioni, stessi indici e stesse firme.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "engine.h"
#include "crypto.h"
#include "disk.h"
#include "rclone.h"
#include "store.h"
#include "zstd.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define IO_BLOCK (4 * MIB)
#define INDEX_BATCH 1000
#define SAVE_INTERVAL 30.0
#define GROUP_THRESHOLD 10000
#define VERIFY_MIN_SIZE (64 * MIB)
#define QUIET_THRESHOLD 2000
#define BAD_LOG_LIMIT 50
#define SKIP_RUN 64
#ifdef _WIN64
#define RAM_MARGIN (512 * MIB)
#else
#define RAM_MARGIN (96 * MIB)
#endif

static const char MAGIC[] = "BKHD\x01\n";   /* 6 byte */
#define MAGIC_LEN 6

void backup_opts_default(BackupOpts *o)
{
    memset(o, 0, sizeof *o);
    o->path = "BKHD";
    o->chunk_kib = 650 * 1024;
    o->compression = "none";
    o->level = 3;
    o->bad_sectors = "retry";
    o->retries = 10;
}

void restore_opts_default(RestoreOpts *o)
{
    memset(o, 0, sizeof *o);
    o->from_chunk = 1;
    o->retries = 10;
}

int64_t parse_size_kib(const char *text)
{
    char t[64];
    size_t n = 0;
    for (const char *p = text; *p && n < sizeof t - 1; p++)
        if (*p != ' ')
            t[n++] = (char)(*p == ',' ? '.' : (*p >= 'a' && *p <= 'z' ? *p - 32 : *p));
    t[n] = 0;
    while (n && (t[n - 1] == 'B' || t[n - 1] == 'I'))
        t[--n] = 0;
    if (!n)
        return -1;
    double mult = 0;
    if (t[n - 1] == 'K')
        mult = 1;
    else if (t[n - 1] == 'M')
        mult = 1024;
    else if (t[n - 1] == 'G')
        mult = 1024 * 1024;
    char *end;
    if (mult) {
        t[n - 1] = 0;
        double v = strtod(t, &end);
        if (*end || end == t)
            return -1;
        return (int64_t)(v * mult + 0.5);
    }
    long long v = strtoll(t, &end, 10);
    return *end || end == t ? -1 : (int64_t)v;
}

/* ===================================================================================================== */
/* Formato .bkhd, manifest e indici                                                                        */
/* ===================================================================================================== */

typedef struct {
    int64_t index, offset, length, payload_size, used;
    char raw[65], payload[65];
    JVal *bad;             /* [[inizio, lunghezza], ...] */
    int sparse, empty;
    char *file;
} Chunk;

static void chunk_free(Chunk *c)
{
    jfree(c->bad);
    free(c->file);
    memset(c, 0, sizeof *c);
}

static int is_hex64(const char *s)
{
    if (!s || strlen(s) != 64)
        return 0;
    for (; *s; s++)
        if (!((*s >= '0' && *s <= '9') || (*s >= 'a' && *s <= 'f')))
            return 0;
    return 1;
}

static int ival(const JVal *o, const char *k, int64_t lo, int64_t hi, int64_t dflt, int has_dflt, int64_t *out,
                const char *what)
{
    JVal *v = jobj_get(o, k);
    if (!v && has_dflt) {
        *out = dflt;
        return OK;
    }
    if (!v || v->t != J_INT || v->i < lo || v->i > hi) {
        set_err("backup non valido: %s", what);
        return FAIL;
    }
    *out = v->i;
    return OK;
}

/* controlla tipi e limiti: un manifest alterato non deve far danni (come validate_manifest) */
static int validate_manifest(const JVal *m)
{
    const char *fmt = jget_str(m, "format");
    if (!m || m->t != J_OBJ || !fmt || strcmp(fmt, "bkhd-manifest")) {
        set_err("manifest non valido");
        return FAIL;
    }
    int64_t ver, size, cs, cc, x;
    if (ival(m, "version", 1, 2, 0, 0, &ver, "versione"))
        return FAIL;
    const char *name = jget_str(m, "name");
    if (!name || !valid_name(name)) {
        set_err("backup non valido: nome");
        return FAIL;
    }
    const char *id = jget_str(m, "backup_id");
    size_t idl = id ? strlen(id) : 0;
    int id_ok = idl >= 8 && idl <= 64;
    for (size_t k = 0; id_ok && k < idl; k++)
        id_ok = strchr("0123456789abcdefABCDEF-", id[k]) != NULL;
    if (!id_ok) {
        set_err("backup non valido: identificativo");
        return FAIL;
    }
    JVal *src = jobj_get(m, "source");
    if (!src || src->t != J_OBJ) {
        set_err("backup non valido: sorgente");
        return FAIL;
    }
    if (ival(src, "size", 1, (int64_t)1 << 60, 0, 0, &size, "dimensione del disco") ||
        ival(m, "chunk_size", 512, MAX_CHUNK_KIB * KIB, 0, 0, &cs, "dimensione dei pacchetti"))
        return FAIL;
    int64_t expect = (size + cs - 1) / cs;
    if (ival(m, "chunk_count", expect, expect, 0, 0, &cc, "numero di pacchetti"))
        return FAIL;
    const char *comp = jget_str(m, "compression"), *status = jget_str(m, "status");
    if (!comp || (strcmp(comp, "none") && strcmp(comp, "zstd"))) {
        set_err("backup non valido: compressione");
        return FAIL;
    }
    if (!status || (strcmp(status, "in_progress") && strcmp(status, "complete"))) {
        set_err("backup non valido: stato");
        return FAIL;
    }
    JVal *sp = jobj_get(m, "sparse");
    if (sp && sp->t != J_BOOL) {
        set_err("backup non valido: modalita'");
        return FAIL;
    }
    if (ver >= 2) {
        JVal *lay = jobj_get(m, "layout");
        if (lay && lay->t == J_NULL)
            lay = NULL;
        if (lay && lay->t != J_OBJ) {
            set_err("backup non valido: disposizione");
            return FAIL;
        }
        if (lay) {
            if (ival(lay, "digits", 5, 12, 5, 1, &x, "disposizione") ||
                ival(lay, "group", 0, 1000, 0, 1, &x, "disposizione"))
                return FAIL;
            if (x != 0 && x != 1000) {
                set_err("backup non valido: disposizione");
                return FAIL;
            }
        }
    } else {
        JVal *ch = jobj_get(m, "chunks");
        if (ch && ch->t != J_ARR) {
            set_err("backup non valido: elenco pacchetti");
            return FAIL;
        }
    }
    JVal *enc = jobj_get(m, "encryption");
    if (enc && enc->t != J_NULL && enc->t != J_OBJ) {
        set_err("backup non valido: cifratura");
        return FAIL;
    }
    return OK;
}

static int64_t mget(const JVal *m, const char *k)
{
    int64_t v = 0;
    jget_int(m, k, &v);
    return v;
}

static int msparse(const JVal *m)
{
    JVal *v = jobj_get(m, "sparse");
    return v && v->t == J_BOOL && v->b;
}

static const JVal *mencryption(const JVal *m)
{
    JVal *e = jobj_get(m, "encryption");
    return e && e->t == J_OBJ ? e : NULL;
}

static void manifest_layout(const JVal *m, int *digits, int *group)
{
    JVal *lay = jobj_get(m, "layout");
    int64_t d = 5, g = 0;
    if (lay && lay->t == J_OBJ) {
        jget_int(lay, "digits", &d);
        jget_int(lay, "group", &g);
    }
    *digits = (int)d;
    *group = (int)g;
}

static char *chunk_filename(const char *name, int64_t index, int digits, int group)
{
    char *fname = xasprintf("%s.part%0*lld.bkhd", name, digits, (long long)index);
    if (!group)
        return fname;
    char *r = xasprintf("g%05lld/%s", (long long)((index - 1) / group), fname);
    free(fname);
    return r;
}

static int64_t bad_bytes(const JVal *bad)
{
    int64_t t = 0;
    for (size_t k = 0; bad && k < bad->n; k++) {
        JVal *r = bad->items[k];
        if (r->t == J_ARR && r->n == 2 && r->items[1]->t == J_INT)
            t += r->items[1]->i;
        else if (r->t != J_ARR)
            t += 512;
    }
    return t;
}

static void add_bad(JVal *bad, int64_t start, int64_t len)
{
    if (bad->n) {
        JVal *last = bad->items[bad->n - 1];
        if (last->t == J_ARR && last->n == 2 && last->items[0]->i + last->items[1]->i == start) {
            last->items[1]->i += len;
            return;
        }
    }
    JVal *r = jarr();
    jarr_push(r, jint(start));
    jarr_push(r, jint(len));
    jarr_push(bad, r);
}

/* come validate_chunk: ogni pacchetto deve stare esattamente nella sua posizione */
static int validate_chunk(const Chunk *c, const JVal *m)
{
    int64_t cs = mget(m, "chunk_size"), cc = mget(m, "chunk_count");
    JVal *src = jobj_get(m, "source");
    int64_t size = mget(src, "size");
    if (c->index < 1 || c->index > cc) {
        set_err("backup non valido: numero del pacchetto");
        return FAIL;
    }
    int64_t off = (c->index - 1) * cs, length = cs < size - off ? cs : size - off;
    if (c->offset != off || c->length != length) {
        set_err("backup non valido: posizione del pacchetto %lld", (long long)c->index);
        return FAIL;
    }
    if (c->payload_size < 0 || c->payload_size > cs + cs / 8 + 16 * MIB) {
        set_err("backup non valido: dimensione pacchetto");
        return FAIL;
    }
    if (!is_hex64(c->raw) || !is_hex64(c->payload)) {
        set_err("backup non valido: checksum del pacchetto %lld", (long long)c->index);
        return FAIL;
    }
    if (c->sparse && (c->used < 0 || c->used > length)) {
        set_err("backup non valido: zone usate");
        return FAIL;
    }
    if (c->bad && (c->bad->t != J_ARR || c->bad->n > 1000000)) {
        set_err("backup non valido: settori illeggibili del pacchetto %lld", (long long)c->index);
        return FAIL;
    }
    return OK;
}

static int str_field(const JVal *o, const char *k, char out[65])
{
    const char *s = jget_str(o, k);
    if (!s || strlen(s) > 64)
        return FAIL;
    strcpy(out, s);
    return OK;
}

/* record compatto dell'indice -> pacchetto completo (come expand_record) */
static int expand_record(const JVal *m, const JVal *rec, Chunk *c)
{
    memset(c, 0, sizeof *c);
    if (!rec || rec->t != J_OBJ) {
        set_err("backup non valido: indice");
        return FAIL;
    }
    int64_t cc = mget(m, "chunk_count"), cs = mget(m, "chunk_size"), size = mget(jobj_get(m, "source"), "size");
    if (ival(rec, "i", 1, cc, 0, 0, &c->index, "indice") || jget_int(rec, "s", &c->payload_size) ||
        str_field(rec, "r", c->raw)) {
        set_err("backup non valido: indice");
        return FAIL;
    }
    if (str_field(rec, "p", c->payload))
        strcpy(c->payload, c->raw);
    int digits, group;
    manifest_layout(m, &digits, &group);
    c->file = chunk_filename(jget_str(m, "name"), c->index, digits, group);
    c->offset = (c->index - 1) * cs;
    c->length = cs < size - c->offset ? cs : size - c->offset;
    JVal *b = jobj_get(rec, "b");
    c->bad = b ? jclone(b) : jarr();
    if (msparse(m)) {
        c->sparse = 1;
        jget_int(rec, "u", &c->used);
        int64_t e = 0;
        jget_int(rec, "e", &e);
        c->empty = e == 1;
    }
    if (validate_chunk(c, m)) {
        chunk_free(c);
        return FAIL;
    }
    return OK;
}

/* pacchetto del manifest v1 (formato 1.0) */
static int v1_chunk(const JVal *m, const JVal *o, Chunk *c)
{
    memset(c, 0, sizeof *c);
    if (!o || o->t != J_OBJ || jget_int(o, "index", &c->index) || jget_int(o, "offset", &c->offset) ||
        jget_int(o, "length", &c->length) || jget_int(o, "payload_size", &c->payload_size) ||
        str_field(o, "raw_sha256", c->raw) || str_field(o, "payload_sha256", c->payload)) {
        set_err("backup non valido: elenco pacchetti");
        return FAIL;
    }
    const char *f = jget_str(o, "file");
    c->file = chunk_filename(jget_str(m, "name"), c->index, 5, 0);
    (void)f;
    JVal *b = jobj_get(o, "bad_sectors");
    c->bad = b ? jclone(b) : jarr();
    if (validate_chunk(c, m)) {
        chunk_free(c);
        return FAIL;
    }
    return OK;
}

static JVal *compact_record(const Chunk *c)
{
    JVal *r = jobj();
    jobj_set(r, "i", jint(c->index));
    jobj_set(r, "s", jint(c->payload_size));
    jobj_set(r, "r", jstr(c->raw));
    if (strcmp(c->payload, c->raw))
        jobj_set(r, "p", jstr(c->payload));
    if (c->bad && c->bad->n)
        jobj_set(r, "b", jclone(c->bad));
    if (c->sparse) {
        jobj_set(r, "u", jint(c->used));
        if (c->empty)
            jobj_set(r, "e", jint(1));
    }
    return r;
}

static int64_t stored_size(const Chunk *c)
{
    return c->empty ? 0 : c->payload_size + HEADER_SIZE;
}

static int load_manifest(Store *s, const char *rel, JVal **out)
{
    char *path = rel && *rel ? xasprintf("%s/manifest.json", rel) : xstrdup("manifest.json");
    StrBuf b;
    sb_init(&b);
    int r = store_read(s, path, &b);
    free(path);
    if (r) {
        sb_free(&b);
        return r;
    }
    JVal *m = jparse(b.s ? b.s : "", b.n);
    sb_free(&b);
    if (!m) {
        set_err("manifest non leggibile");
        return FAIL;
    }
    if (validate_manifest(m)) {
        jfree(m);
        return FAIL;
    }
    *out = m;
    return OK;
}

/* pacchetti salvati, contigui dal primo (come load_chunks) */
static int load_chunks(Store *s, const JVal *m, const Crypto *crypto, Chunk **out, int64_t *count)
{
    int64_t cc = mget(m, "chunk_count");
    Chunk *list = xcalloc((size_t)(cc > 0 ? cc : 1), sizeof(Chunk));
    int64_t n = 0;
    if (mget(m, "version") < 2) {
        JVal *ch = jobj_get(m, "chunks");
        for (size_t k = 0; ch && k < ch->n && n < cc; k++) {
            Chunk c;
            if (v1_chunk(m, ch->items[k], &c))
                goto fail;
            if (c.index != n + 1) {
                chunk_free(&c);
                break;
            }
            list[n++] = c;
        }
    } else {
        JVal **recs = xcalloc((size_t)cc + 2, sizeof(JVal *));
        StrList names = {0};
        int r = store_list_names(s, "index", 0, &names);
        if (r) {
            free(recs);
            sl_free(&names);
            free(list);
            return r;
        }
        sl_sort(&names);
        JVal *keep = jarr();                  /* tiene in vita i record */
        for (size_t k = 0; k < names.n; k++) {
            const char *nm = names.v[k];
            size_t l = strlen(nm);
            if (strncmp(nm, "index-", 6) || l < 5 || strcmp(nm + l - 5, ".json"))
                continue;
            char *rel = xasprintf("index/%s", nm);
            StrBuf b;
            sb_init(&b);
            r = store_read(s, rel, &b);
            free(rel);
            if (r) {
                sb_free(&b);
                goto fail_idx;
            }
            JVal *data = jparse(b.s ? b.s : "", b.n);
            sb_free(&b);
            JVal *chunks = data ? jobj_get(data, "chunks") : NULL;
            if (!data || data->t != J_OBJ || !chunks || chunks->t != J_ARR) {
                jfree(data);
                set_err("indice %s non valido", nm);
                r = FAIL;
                goto fail_idx;
            }
            if (crypto) {
                char *what = xasprintf("Indice %s", nm);
                r = crypto_verify(crypto, data, what);
                free(what);
                if (r) {
                    jfree(data);
                    goto fail_idx;
                }
            }
            for (size_t q = 0; q < chunks->n; q++) {
                JVal *rec = chunks->items[q];
                int64_t i;
                if (rec->t == J_OBJ && !jget_int(rec, "i", &i) && i >= 1 && i <= cc)
                    recs[i] = rec;
            }
            jarr_push(keep, data);
            continue;
        fail_idx:
            jfree(keep);
            free(recs);
            sl_free(&names);
            for (int64_t q = 0; q < n; q++)
                chunk_free(&list[q]);
            free(list);
            return r;
        }
        while (n < cc && recs[n + 1]) {
            Chunk c;
            if (expand_record(m, recs[n + 1], &c)) {
                jfree(keep);
                free(recs);
                sl_free(&names);
                goto fail;
            }
            list[n++] = c;
        }
        jfree(keep);
        free(recs);
        sl_free(&names);
    }
    *out = list;
    *count = n;
    return OK;
fail:
    for (int64_t q = 0; q < n; q++)
        chunk_free(&list[q]);
    free(list);
    return FAIL;
}

static void free_chunks(Chunk *list, int64_t n)
{
    for (int64_t q = 0; q < n; q++)
        chunk_free(&list[q]);
    free(list);
}

/* manifest v1 -> v2 (come upgrade_manifest) */
static void upgrade_manifest(JVal *m)
{
    if (mget(m, "version") >= 2)
        return;
    jobj_del(m, "chunks");
    jobj_set(m, "version", jint(2));
    jobj_set(m, "index_batch", jint(INDEX_BATCH));
    JVal *lay = jobj();
    jobj_set(lay, "digits", jint(5));
    jobj_set(lay, "group", jint(0));
    jobj_set(m, "layout", lay);
    jobj_set(m, "engine", jstr(BKHD_ENGINE_ID));
}

static int write_signed(Store *s, const char *rel, JVal *obj, const Crypto *crypto, const char *what)
{
    if (crypto) {
        char sig[65];
        crypto_sign(crypto, obj, sig);
        jobj_set(obj, "mac", jstr(sig));
    }
    return store_write_json(s, rel, obj, what);
}

static char *index_filename(int64_t batch)
{
    return xasprintf("index/index-%05lld.json", (long long)batch);
}

/* ===================================================================================================== */
/* BACKUP                                                                                                  */
/* ===================================================================================================== */

typedef struct {
    Reporter *rep;
    int64_t chunk, chunks;
    int64_t offset, length, total;
    double t0;
} ProgCtx;

static void upload_progress(void *p, int64_t done, int64_t tot, double speed)
{
    ProgCtx *c = p;
    double frac = tot ? (double)done / (double)tot : 0.0;
    if (frac > 1)
        frac = 1;
    double el = mono_seconds() - c->t0;
    if (!speed)
        speed = (double)done / (el > 1e-6 ? el : 1e-6);
    double within = 0.25 + 0.75 * frac;
    rep_progress(c->rep, 0, "upload", frac, speed, c->chunk, c->chunks,
                 ((double)c->offset + (double)c->length * within) / (double)c->total);
}

/* legge un blocco; i settori illeggibili diventano zeri e vengono annotati (come read_block) */
static int read_block(Dev *d, uint8_t *buf, int64_t off, size_t n, int sector, JVal *bad, Reporter *rep, int skip)
{
    DWORD e;
    if (!dev_read_at(d, off, buf, n, &e))
        return OK;
    if (!read_error_is_bad(e))
        return FAIL;
    int attempts = skip ? 1 : 5, run = 0;
    for (size_t s = 0; s < n; s += (size_t)sector) {
        size_t plen = n - s < (size_t)sector ? n - s : (size_t)sector;
        int ok = 0;
        if (!(skip && run >= SKIP_RUN))
            for (int a = 0; a < attempts; a++) {
                if (!dev_read_at(d, off + (int64_t)s, buf + s, plen, &e)) {
                    ok = 1;
                    break;
                }
                if (!read_error_is_bad(e))
                    return FAIL;
            }
        if (ok) {
            run = 0;
            continue;
        }
        run++;
        memset(buf + s, 0, plen);
        add_bad(bad, off + (int64_t)s, (int64_t)plen);
        rep->bad_count++;
        if (rep->bad_count <= BAD_LOG_LIMIT)
            rep_log(rep, "warning", "Settore illeggibile all'offset %lld: salvato come zeri", (long long)(off + (int64_t)s));
        else if (rep->bad_count == BAD_LOG_LIMIT + 1)
            rep_log(rep, "warning", "Altri settori illeggibili: l'elenco completo e' nel manifest del backup.");
    }
    return OK;
}

typedef struct {
    ChunkOut *out;
    Gcm *gcm;
    uint8_t *ebuf;           /* appoggio per i dati cifrati */
    Hash payload_hash;
} Sink;

static int sink_write(Sink *s, const uint8_t *data, size_t n)
{
    while (n) {
        size_t k = n > (size_t)IO_BLOCK ? (size_t)IO_BLOCK : n;
        const uint8_t *w = data;
        size_t wn = k;
        if (s->gcm) {
            wn = gcm_update(s->gcm, data, k, s->ebuf);
            w = s->ebuf;
        }
        hash_update(&s->payload_hash, w, wn);
        if (chunkout_write(s->out, w, wn))
            return FAIL;
        data += k;
        n -= k;
    }
    return OK;
}

typedef struct {
    const BackupOpts *o;
    Reporter *rep;
    Store *store;
    Dev *dev;
    JVal *manifest;
    const Crypto *crypto;
    int64_t total, chunk_size, nchunks;
    int sector, quiet, digits, group, zstd;
    uint8_t *ram;            /* pacchetto in RAM (cloud) */
    size_t ram_cap;
    uint8_t *rbuf;           /* blocco letto dal disco (allineato) */
    uint8_t *zbuf, *ebuf;
    size_t zcap;
    ZSTD_CCtx *cctx;
} BackupCtx;

static int backup_chunk(BackupCtx *b, int64_t i, Chunk *meta)
{
    Reporter *rep = b->rep;
    int64_t index = i + 1, offset = i * b->chunk_size;
    int64_t length = b->chunk_size < b->total - offset ? b->chunk_size : b->total - offset;
    memset(meta, 0, sizeof *meta);
    meta->index = index;
    meta->offset = offset;
    meta->length = length;
    meta->file = chunk_filename(b->o->name, index, b->digits, b->group);
    meta->bad = jarr();
    const char *backup_id = jget_str(b->manifest, "backup_id");

    if (store_check_space(b->store, length + HEADER_SIZE))
        return FAIL;
    char *label = xasprintf("lettura settori %lld-%lld%s", (long long)(offset / b->sector),
                            (long long)((offset + length) / b->sector - 1),
                            b->store->local ? " e scrittura sulla destinazione" : " in RAM");
    rep_phase(rep, "read", index, b->nchunks, label);
    free(label);

    ChunkOut out;
    if (store_chunk_create(b->store, meta->file, b->ram, b->ram_cap, &out))
        return FAIL;
    int r = FAIL;
    Gcm gcm;
    int gcm_on = 0;
    uint8_t nonce[12], tag[16];
    Hash raw_hash;
    Sink sink;
    memset(&sink, 0, sizeof sink);
    sink.out = &out;
    sink.ebuf = b->ebuf;
    hash_init(&sink.payload_hash, NULL);
    hash_init(&raw_hash, b->crypto ? b->crypto->mac_key : NULL);
    uint8_t *zero = xcalloc(1, HEADER_SIZE);
    if (chunkout_write(&out, zero, HEADER_SIZE)) {
        free(zero);
        goto done;
    }
    free(zero);
    if (b->crypto) {
        random_bytes(nonce, sizeof nonce);
        char *aad = crypto_aad(backup_id, index, offset, length, b->o->compression, 0);
        int g = gcm_start(&gcm, b->crypto->enc_key, nonce, aad, 1);
        free(aad);
        if (g)
            goto done;
        gcm_on = 1;
        sink.gcm = &gcm;
    }
    if (b->zstd)
        ZSTD_CCtx_reset(b->cctx, ZSTD_reset_session_only);

    double t0 = mono_seconds();
    int64_t pos = 0;
    while (pos < length) {
        if (cancelled()) {
            set_err("operazione annullata");
            r = CANCELLED;
            goto done;
        }
        size_t n = (size_t)(length - pos < IO_BLOCK ? length - pos : IO_BLOCK);
        if (read_block(b->dev, b->rbuf, offset + pos, n, b->sector, meta->bad, rep, !strcmp(b->o->bad_sectors, "skip")))
            goto done;
        hash_update(&raw_hash, b->rbuf, n);
        if (b->zstd) {
            ZSTD_inBuffer in = {b->rbuf, n, 0};
            while (in.pos < in.size) {
                ZSTD_outBuffer zo = {b->zbuf, b->zcap, 0};
                size_t z = ZSTD_compressStream2(b->cctx, &zo, &in, ZSTD_e_continue);
                if (ZSTD_isError(z)) {
                    set_err("compressione zstd non riuscita: %s", ZSTD_getErrorName(z));
                    goto done;
                }
                if (zo.pos && sink_write(&sink, b->zbuf, zo.pos))
                    goto done;
            }
        } else if (sink_write(&sink, b->rbuf, n)) {
            goto done;
        }
        pos += (int64_t)n;
        double el = mono_seconds() - t0;
        double frac = (double)pos / (double)length;
        rep_progress(rep, 0, "read", frac, (double)pos / (el > 1e-6 ? el : 1e-6), index, b->nchunks,
                     ((double)offset + (double)length * 0.25 * frac) / (double)b->total);
    }
    if (b->zstd) {
        size_t left;
        do {
            ZSTD_inBuffer in = {NULL, 0, 0};
            ZSTD_outBuffer zo = {b->zbuf, b->zcap, 0};
            left = ZSTD_compressStream2(b->cctx, &zo, &in, ZSTD_e_end);
            if (ZSTD_isError(left)) {
                set_err("compressione zstd non riuscita: %s", ZSTD_getErrorName(left));
                goto done;
            }
            if (zo.pos && sink_write(&sink, b->zbuf, zo.pos))
                goto done;
        } while (left);
    }
    if (gcm_on) {
        size_t k = gcm_finish(&gcm, b->ebuf, tag);
        if (k) {
            hash_update(&sink.payload_hash, b->ebuf, k);
            if (chunkout_write(&out, b->ebuf, k))
                goto done;
        }
    }
    meta->payload_size = (int64_t)out.len - HEADER_SIZE;
    hash_final_hex(&raw_hash, meta->raw);
    {
        char ph[65];
        hash_final_hex(&sink.payload_hash, ph);
        strcpy(meta->payload, (b->zstd || b->crypto) ? ph : meta->raw);
    }
    {
        /* intestazione: stessi campi e stesso ordine del motore Linux */
        JVal *h = jobj();
        jobj_set(h, "index", jint(index));
        jobj_set(h, "file", jstr(meta->file));
        jobj_set(h, "offset", jint(offset));
        jobj_set(h, "length", jint(length));
        jobj_set(h, "payload_size", jint(meta->payload_size));
        jobj_set(h, "raw_sha256", jstr(meta->raw));
        jobj_set(h, "payload_sha256", jstr(meta->payload));
        jobj_set(h, "format", jstr("bkhd"));
        jobj_set(h, "version", jint(1));
        jobj_set(h, "backup_id", jstr(backup_id));
        jobj_set(h, "name", jstr(b->o->name));
        jobj_set(h, "chunks", jint(b->nchunks));
        jobj_set(h, "compression", jstr(b->o->compression));
        jobj_set(h, "source_size", jint(b->total));
        char *now = now_iso();
        jobj_set(h, "created", jstr(now));
        free(now);
        jobj_set(h, "bad_sector_count", jint(bad_bytes(meta->bad) / b->sector));
        if (gcm_on) {
            char hx[33];
            jobj_set(h, "encrypted", jbool(1));
            hex_encode(nonce, 12, hx);
            jobj_set(h, "nonce", jstr(hx));
            hex_encode(tag, 16, hx);
            jobj_set(h, "tag", jstr(hx));
        }
        char *js = jdump(h, J_COMPACT_ASCII);
        jfree(h);
        size_t jl = strlen(js);
        if (MAGIC_LEN + jl >= HEADER_SIZE) {
            free(js);
            set_err("intestazione .bkhd troppo grande");
            goto done;
        }
        uint8_t *hdr = xcalloc(1, HEADER_SIZE);
        memcpy(hdr, MAGIC, MAGIC_LEN);
        memcpy(hdr + MAGIC_LEN, js, jl);
        free(js);
        int hr = chunkout_header(&out, hdr, HEADER_SIZE);
        free(hdr);
        if (hr)
            goto done;
    }
    int64_t file_size = HEADER_SIZE + meta->payload_size;
    if (b->zstd && !b->quiet) {
        char *a = human((double)length), *c = human((double)file_size);
        rep_log(rep, "info", "Pacchetto %lld: %s di disco -> %s compressi", (long long)index, a, c);
        free(a);
        free(c);
    }
    {
        char *fs = human((double)file_size);
        char *lbl = xasprintf("%s di %s (%s)", b->store->local ? "scrittura" : "upload", meta->file, fs);
        free(fs);
        rep_phase(rep, b->store->local ? "write" : "upload", index, b->nchunks, lbl);
        free(lbl);
        ProgCtx pc = {rep, index, b->nchunks, offset, length, b->total, mono_seconds()};
        r = store_chunk_commit(b->store, &out, meta->file, upload_progress, &pc, b->chunk_size >= VERIFY_MIN_SIZE);
        if (!r)
            rep_progress(rep, 1, "upload", 1.0, 0, index, b->nchunks,
                         ((double)offset + (double)length) / (double)b->total);
    }
done:
    if (gcm_on)
        gcm_free(&gcm);
    if (r)
        store_chunk_discard(&out);
    else
        memset(&out, 0, sizeof out);
    return r;
}

static int check_dest_not_on_source(const char *source, const char *dest)
{
    int sd = device_disk_number(source);
    if (sd < 0)
        return OK;
    int dd[8], n = disk_of_path(dest, dd, 8);
    for (int k = 0; k < n; k++)
        if (dd[k] == sd) {
            set_err("La cartella di destinazione si trova sul disco da copiare: scegli un altro disco");
            return FAIL;
        }
    return OK;
}

static JVal *build_manifest(const BackupOpts *o, JVal *info, int64_t chunk_size, int64_t nchunks, int64_t total,
                            const Crypto *crypto, int local)
{
    JVal *m = jobj();
    char *id = new_uuid4(), *now = now_iso();
    wchar_t host[256];
    DWORD hl = 256;
    char *h = GetComputerNameExW(ComputerNameDnsHostname, host, &hl) ? utf8(host) : xstrdup("windows");
    jobj_set(m, "format", jstr("bkhd-manifest"));
    jobj_set(m, "version", jint(2));
    jobj_set(m, "engine", jstr(BKHD_ENGINE_ID));
    jobj_set(m, "backup_id", jstr(id));
    jobj_set(m, "name", jstr(o->name));
    jobj_set(m, "status", jstr("in_progress"));
    jobj_set(m, "created", jstr(now));
    jobj_set(m, "updated", jstr(now));
    jobj_set(m, "finished", jnull());
    jobj_set(m, "host", jstr(h));
    jobj_set(m, "source", info);
    jobj_set(m, "chunk_size", jint(chunk_size));
    jobj_set(m, "chunk_count", jint(nchunks));
    jobj_set(m, "compression", jstr(o->compression));
    jobj_set(m, "compression_level", strcmp(o->compression, "zstd") ? jnull() : jint(o->level));
    jobj_set(m, "bad_sectors_total", jint(0));
    jobj_set(m, "chunks_done", jint(0));
    jobj_set(m, "stored_bytes", jint(0));
    jobj_set(m, "index_batch", jint(INDEX_BATCH));
    JVal *lay = jobj();
    char digits[32];
    snprintf(digits, sizeof digits, "%lld", (long long)nchunks);
    jobj_set(lay, "digits", jint(strlen(digits) > 5 ? (int64_t)strlen(digits) : 5));
    jobj_set(lay, "group", jint(nchunks > GROUP_THRESHOLD ? 1000 : 0));
    jobj_set(m, "layout", lay);
    jobj_set(m, "sparse", jbool(0));
    jobj_set(m, "encryption", crypto ? jclone(crypto->meta) : jnull());
    jobj_set(m, "bad_sector_mode", jstr(o->bad_sectors));
    jobj_set(m, "used_bytes", jint(total));
    jobj_set(m, "destination", jstr(local ? "local" : "cloud"));
    free(id);
    free(now);
    free(h);
    return m;
}

static void emit_start(Reporter *rep, const char *mode, int64_t total, int64_t chunks, int64_t chunk_size,
                       const char *dest, int64_t first, const char *backup_id, int encrypted)
{
    JVal *ev = jobj();
    jobj_set(ev, "type", jstr("start"));
    jobj_set(ev, "mode", jstr(mode));
    jobj_set(ev, "total", jint(total));
    jobj_set(ev, "chunks", jint(chunks));
    jobj_set(ev, "chunk_size", jint(chunk_size));
    jobj_set(ev, "dest", jstr(dest ? dest : ""));
    jobj_set(ev, "first", jint(first));
    if (backup_id) {
        jobj_set(ev, "backup_id", jstr(backup_id));
        jobj_set(ev, "encrypted", jbool(encrypted));
    }
    rep_emit(rep, ev);
}

static void emit_chunk_done(Reporter *rep, int64_t chunk, int64_t chunks, const char *msg, double overall, double eta)
{
    JVal *ev = jobj();
    jobj_set(ev, "type", jstr("chunk_done"));
    jobj_set(ev, "chunk", jint(chunk));
    jobj_set(ev, "chunks", jint(chunks));
    jobj_set(ev, "msg", msg ? jstr(msg) : jnull());
    JVal *o = jint(0);
    o->t = J_FLOAT;
    o->d = overall > 1 ? 1 : overall;
    jobj_set(ev, "overall", o);
    if (eta >= 0) {
        JVal *e = jint(0);
        e->t = J_FLOAT;
        e->d = eta;
        jobj_set(ev, "eta", e);
    }
    rep_emit(rep, ev);
}

static uint8_t *big_alloc(size_t n)
{
    return VirtualAlloc(NULL, n ? n : 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
}

int engine_backup(const BackupOpts *o, Reporter *rep, char **done_msg)
{
    int r = FAIL;
    *done_msg = NULL;
    JVal *info = NULL, *manifest = NULL, *batch_records = NULL;
    Crypto crypto_s, *crypto = NULL;
    Chunk *done = NULL;
    int64_t ndone = 0, start = 0;
    int converted = 0;
    char *dest = NULL;
    Rclone rc;
    Store store;
    BackupCtx b;
    Dev dev;
    memset(&rc, 0, sizeof rc);
    memset(&store, 0, sizeof store);
    memset(&b, 0, sizeof b);
    memset(&dev, 0, sizeof dev);
    if (!o->name || !valid_name(o->name)) {
        set_err("nome del backup non valido (usa lettere, numeri, . _ -)");
        return FAIL;
    }
    int cloud = !path_is_local(o->remote);
    if (o->chunk_kib < MIN_CHUNK_KIB || o->chunk_kib > MAX_CHUNK_KIB) {
        set_err("la dimensione dei pacchetti deve essere tra 1,2 MB (floppy) e 8 GB");
        return FAIL;
    }
    if (cloud && o->chunk_kib > MAX_CLOUD_CHUNK_KIB) {
        set_err("la versione a 32 bit invia sul cloud pacchetti fino a 700 MB: scegli una dimensione minore "
                "oppure salva su un disco");
        return FAIL;
    }
    int zstd = !strcmp(o->compression, "zstd");
    if (!zstd && strcmp(o->compression, "none")) {
        set_err("compressione non valida");
        return FAIL;
    }
    if (o->level < 1 || o->level > 19) {
        set_err("livello di compressione non valido (1-19)");
        return FAIL;
    }
    if (strcmp(o->bad_sectors, "retry") && strcmp(o->bad_sectors, "skip")) {
        set_err("modalita' per i settori illeggibili non valida");
        return FAIL;
    }

    if (source_info(o->source, &info))
        return FAIL;
    int64_t total = mget(info, "size"), lsec = mget(info, "logical_sector");
    int block = !strcmp(jget_str(info, "kind"), "block");
    if (total <= 0) {
        jfree(info);
        set_err("la sorgente ha dimensione zero");
        return FAIL;
    }
    int sector = (int)(lsec > 512 ? lsec : 512);
    int64_t chunk_size = o->chunk_kib * KIB;
    chunk_size -= chunk_size % sector;
    if (chunk_size <= 0) {
        jfree(info);
        set_err("dimensione dei pacchetti piu' piccola del settore del disco");
        return FAIL;
    }
    int64_t nchunks = (total + chunk_size - 1) / chunk_size;
    int quiet = nchunks > QUIET_THRESHOLD;
    if (block) {
        char *busy = in_use_reason(o->source);
        if (busy) {
            rep_log(rep, "warning", "La sorgente e' in uso (%s). Il backup proseguira', ma per una copia coerente "
                                    "e' consigliato avviare il PC dalla pennetta BKHD Live.", busy);
            free(busy);
        }
    }

    if (cloud && rc_init(&rc, o->rclone, o->rclone_config)) {
        jfree(info);
        return FAIL;
    }
    if (store_open(&store, o->remote, o->path, o->name, cloud ? &rc : NULL, rep, o->retries)) {
        jfree(info);
        rc_free(&rc);
        return FAIL;
    }
    dest = store_where(&store, NULL);
    size_t zbound = zstd ? ZSTD_compressBound((size_t)(chunk_size < total ? chunk_size : total)) : 0;
    size_t ram_cap = 0;
    if (store.local) {
        if (check_dest_not_on_source(o->source, dest))
            goto out_early;
    } else {
        int64_t payload = zstd ? (int64_t)zbound : (chunk_size < total ? chunk_size : total);
        ram_cap = (size_t)(payload + HEADER_SIZE + MIB);
        uint64_t avail = mem_available();
        if (avail && avail < (uint64_t)ram_cap + RAM_MARGIN) {
            char *a = human((double)ram_cap + RAM_MARGIN), *b = human((double)avail);
            set_err("RAM insufficiente: servono %s, disponibili %s. Riduci la dimensione dei pacchetti.", a, b);
            free(a);
            free(b);
            goto out_early;
        }
    }
    {
        char *t = human((double)total), *c = human((double)chunk_size);
        rep_log(rep, "info", "Sorgente: %s (%s, settore %d B) -> %s%s", o->source, t, sector, dest,
                store.local ? " (scrittura diretta su disco)" : "");
        rep_log(rep, "info", "Pacchetti: %lld da %s, compressione: %s", (long long)nchunks, c, o->compression);
        free(t);
        free(c);
    }
    if (nchunks > 50000)
        rep_log(rep, "warning", "Con %lld pacchetti ogni pacchetto e' un trasferimento separato: il backup sara' molto "
                                "piu' lento che con pacchetti grandi.", (long long)nchunks);

    {
        StrList names = {0};
        int lr = store_list_names(&store, NULL, 0, &names);
        int exists = !lr && sl_has(&names, "manifest.json");
        sl_free(&names);
        if (lr) {
            r = lr;
            goto out_early;
        }
        if (exists) {
            JVal *old = NULL;
            if ((r = load_manifest(&store, NULL, &old)))
                goto out_early;
            r = FAIL;
            const char *st = jget_str(old, "status");
            if (o->overwrite) {
                rep_log(rep, "warning", "Backup esistente con lo stesso nome: verra' sovrascritto.");
                store_delete_backup_files(&store);
                jfree(old);
            } else if (!strcmp(st, "complete")) {
                set_err("esiste gia' un backup completo chiamato '%s'. Scegli un altro nome.", o->name);
                jfree(old);
                goto out_early;
            } else if (!o->resume) {
                set_err("esiste un backup interrotto chiamato '%s': attiva 'Riprendi' per continuarlo oppure "
                        "scegli un altro nome.", o->name);
                jfree(old);
                goto out_early;
            } else {
                int same = mget(jobj_get(old, "source"), "size") == total && mget(old, "chunk_size") == chunk_size &&
                           !strcmp(jget_str(old, "compression"), o->compression);
                if (!same || msparse(old)) {
                    set_err(msparse(old) ? "il backup interrotto e' di tipo 'solo settori usati': riprendilo con la "
                                           "pennetta BKHD Live (Linux)"
                                         : "il backup interrotto non corrisponde (disco, dimensione pacchetti o "
                                           "compressione diversi): impossibile riprenderlo");
                    jfree(old);
                    goto out_early;
                }
                const JVal *enc = mencryption(old);
                if (enc) {
                    if (crypto_open(&crypto_s, o->password, enc)) {
                        jfree(old);
                        goto out_early;
                    }
                    crypto = &crypto_s;
                } else if (o->encrypt) {
                    set_err("il backup interrotto non e' protetto da password: riprendilo senza password");
                    jfree(old);
                    goto out_early;
                }
                if (crypto) {
                    if (crypto_verify(crypto, old, "Manifest")) {
                        jfree(old);
                        goto out_crypto;
                    }
                } else if (o->expect_encrypted) {
                    set_err("il backup risulta NON protetto da password ma era stato creato protetto: possibile "
                            "manomissione, ripresa annullata");
                    jfree(old);
                    goto out_early;
                }
                if ((r = load_chunks(&store, old, crypto, &done, &ndone))) {
                    jfree(old);
                    goto out_crypto;
                }
                r = FAIL;
                converted = mget(old, "version") < 2;
                upgrade_manifest(old);
                manifest = old;
                start = ndone;
                rep_log(rep, "info", "Ripresa del backup interrotto dal pacchetto %lld/%lld.", (long long)start + 1,
                        (long long)nchunks);
            }
        }
    }
    if (!manifest && o->encrypt) {
        if (!o->password || strlen(o->password) < MIN_PASSWORD) {
            set_err("la password deve avere almeno %d caratteri", MIN_PASSWORD);
            goto out_early;
        }
        rep_log(rep, "info", "Calcolo della chiave di cifratura dalla password...");
        if (crypto_create(&crypto_s, o->password))
            goto out_early;
        crypto = &crypto_s;
        rep_log(rep, "info", "Backup protetto da password (AES-256-GCM): senza la password non sara' possibile "
                             "ripristinarlo.");
    }
    if (!manifest) {
        manifest = build_manifest(o, info, chunk_size, nchunks, total, crypto, store.local);
        info = NULL;
    }
    {
        int64_t stored = 0, badt = 0;
        for (int64_t k = 0; k < ndone; k++) {
            stored += stored_size(&done[k]);
            badt += bad_bytes(done[k].bad);
        }
        jobj_set(manifest, "chunks_done", jint(ndone));
        jobj_set(manifest, "stored_bytes", jint(stored));
        jobj_set(manifest, "bad_sectors_total", jint(badt / sector));
    }
    /* record del gruppo di indice ancora aperto */
    batch_records = jarr();
    for (int64_t k = (ndone / INDEX_BATCH) * INDEX_BATCH; k < ndone; k++)
        jarr_push(batch_records, compact_record(&done[k]));

    emit_start(rep, "backup", total, nchunks, chunk_size, dest, start + 1, jget_str(manifest, "backup_id"),
               crypto != NULL);
    rep->bad_count = 0;

    b.o = o;
    b.rep = rep;
    b.store = &store;
    b.manifest = manifest;
    b.crypto = crypto;
    b.total = total;
    b.chunk_size = chunk_size;
    b.nchunks = nchunks;
    b.sector = sector;
    b.quiet = quiet;
    b.zstd = zstd;
    manifest_layout(manifest, &b.digits, &b.group);
    if (dev_open_read(o->source, &dev))
        goto out_records;
    b.dev = &dev;
    b.rbuf = big_alloc(IO_BLOCK);
    b.ebuf = big_alloc(IO_BLOCK + 64);
    if (zstd) {
        b.zcap = ZSTD_CStreamOutSize();
        b.zbuf = xmalloc(b.zcap);
        b.cctx = ZSTD_createCCtx();
        ZSTD_CCtx_setParameter(b.cctx, ZSTD_c_compressionLevel, o->level);
    }
    if (!store.local) {
        b.ram_cap = ram_cap;
        b.ram = big_alloc(ram_cap);
        if (!b.ram) {
            set_err("memoria insufficiente per un pacchetto da %lld MB: riduci la dimensione dei pacchetti",
                    (long long)(ram_cap / MIB));
            goto out_dev;
        }
    }
    if (!b.rbuf || !b.ebuf) {
        set_err("memoria insufficiente");
        goto out_dev;
    }
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED);     /* niente sospensione durante il backup */
    if ((r = store_prepare(&store)))
        goto out_sleep;
    r = FAIL;
#define SAVE_STATE(batch)                                                                                         \
    do {                                                                                                          \
        if (batch_records->n) {                                                                                   \
            JVal *ix = jobj();                                                                                    \
            jobj_set(ix, "batch", jint(batch));                                                                   \
            jobj_set(ix, "chunks", jclone(batch_records));                                                        \
            char *fn_ = index_filename(batch);                                                                    \
            int e_ = write_signed(&store, fn_, ix, crypto, "Salvataggio dell'indice");                            \
            free(fn_);                                                                                            \
            jfree(ix);                                                                                            \
            if (e_) {                                                                                             \
                r = e_;                                                                                           \
                goto out_sleep;                                                                                   \
            }                                                                                                     \
        }                                                                                                         \
        char *now_ = now_iso();                                                                                   \
        jobj_set(manifest, "updated", jstr(now_));                                                                \
        free(now_);                                                                                               \
        int e2_ = write_signed(&store, "manifest.json", manifest, crypto, "Salvataggio del manifest");            \
        if (e2_) {                                                                                                \
            r = e2_;                                                                                              \
            goto out_sleep;                                                                                       \
        }                                                                                                         \
    } while (0)

    if (converted) {                  /* backup della versione 1.0: indici dei pacchetti gia' salvati */
        for (int64_t bt = 0; bt < ndone / INDEX_BATCH; bt++) {
            JVal *ix = jobj(), *recs = jarr();
            for (int64_t k = bt * INDEX_BATCH; k < (bt + 1) * INDEX_BATCH; k++)
                jarr_push(recs, compact_record(&done[k]));
            jobj_set(ix, "batch", jint(bt));
            jobj_set(ix, "chunks", recs);
            char *fn = index_filename(bt);
            int e = write_signed(&store, fn, ix, crypto, "Salvataggio dell'indice");
            free(fn);
            jfree(ix);
            if (e) {
                r = e;
                goto out_sleep;
            }
        }
        SAVE_STATE(ndone / INDEX_BATCH);
    } else if (start == 0) {
        SAVE_STATE(0);
    }
    double t_start = mono_seconds(), last_save = t_start, last_log = t_start;
    const char *where = store.local ? "su disco" : "sul cloud e rimosso dalla RAM";
    int unsaved = 0;
    int64_t sum_stored = mget(manifest, "stored_bytes"), sum_bad = mget(manifest, "bad_sectors_total");
    for (int64_t i = start; i < nchunks; i++) {
        Chunk meta;
        memset(&meta, 0, sizeof meta);
        int cr = cancelled() ? CANCELLED : backup_chunk(&b, i, &meta);
        if (cr) {
            chunk_free(&meta);
            if (cr == CANCELLED && unsaved) {   /* annullamento: salva l'indice, la ripresa partira' da qui */
                InterlockedExchange(&g_cancel, 0);
                int64_t bt = (i - 1) / INDEX_BATCH;
                JVal *ix = jobj();
                jobj_set(ix, "batch", jint(bt));
                jobj_set(ix, "chunks", jclone(batch_records));
                char *fn = index_filename(bt);
                int e = batch_records->n ? write_signed(&store, fn, ix, crypto, "Salvataggio dell'indice") : OK;
                free(fn);
                jfree(ix);
                if (!e) {
                    char *now = now_iso();
                    jobj_set(manifest, "updated", jstr(now));
                    free(now);
                    e = write_signed(&store, "manifest.json", manifest, crypto, "Salvataggio del manifest");
                }
                if (!e)
                    rep_log(rep, "info", "Indice salvato: la ripresa partira' dal pacchetto %lld.", (long long)i + 1);
                InterlockedExchange(&g_cancel, 1);
            }
            r = cr == CANCELLED ? CANCELLED : FAIL;
            if (r == CANCELLED)
                set_err("operazione annullata");
            goto out_sleep;
        }
        unsaved = 1;
        int64_t batch = i / INDEX_BATCH;
        jarr_push(batch_records, compact_record(&meta));
        sum_stored += stored_size(&meta);
        sum_bad += bad_bytes(meta.bad) / sector;
        jobj_set(manifest, "chunks_done", jint(i + 1));
        jobj_set(manifest, "stored_bytes", jint(sum_stored));
        jobj_set(manifest, "bad_sectors_total", jint(sum_bad));
        chunk_free(&meta);
        double now = mono_seconds();
        int batch_full = (i + 1) % INDEX_BATCH == 0;
        if (batch_full || chunk_size >= VERIFY_MIN_SIZE || now - last_save >= SAVE_INTERVAL || i + 1 == nchunks) {
            SAVE_STATE(batch);
            last_save = mono_seconds();
            unsaved = 0;
            if (batch_full) {
                jfree(batch_records);
                batch_records = jarr();
            }
        }
        double done_bytes = (double)(i + 1) * (double)chunk_size;
        double elapsed = now - t_start;
        char *msg = xasprintf("Pacchetto %lld/%lld salvato %s.", (long long)i + 1, (long long)nchunks, where);
        if (quiet) {
            free(msg);
            msg = NULL;
            if (now - last_log >= 60 || i + 1 == nchunks) {
                last_log = now;
                char *hs = human((double)sum_stored);
                msg = xasprintf("%lld/%lld pacchetti salvati (%s scritti sulla destinazione).", (long long)i + 1,
                                (long long)nchunks, hs);
                free(hs);
            }
        }
        emit_chunk_done(rep, i + 1, nchunks, msg, (done_bytes < total ? done_bytes : (double)total) / (double)total,
                        elapsed / (double)(i + 1 - start) * (double)(nchunks - i - 1));
        free(msg);
        {                                       /* test automatici: simula un annullamento dopo N pacchetti */
            char tc[32];
            DWORD k = GetEnvironmentVariableA("BKHD_TEST_CANCEL_AFTER", tc, sizeof tc);
            if (k && k < sizeof tc && _atoi64(tc) == i + 1)
                InterlockedExchange(&g_cancel, 1);
        }
    }
    jobj_set(manifest, "status", jstr("complete"));
    {
        char *now = now_iso();
        jobj_set(manifest, "finished", jstr(now));
        jobj_set(manifest, "updated", jstr(now));
        free(now);
    }
    if ((r = write_signed(&store, "manifest.json", manifest, crypto, "Salvataggio del manifest")))
        goto out_sleep;
    {
        char *hs = human((double)sum_stored);
        if (sum_bad)
            *done_msg = xasprintf("Backup completato: %lld pacchetti in %s (%s occupati) (%lld settori illeggibili "
                                  "salvati come zeri)", (long long)nchunks, dest, hs, (long long)sum_bad);
        else
            *done_msg = xasprintf("Backup completato: %lld pacchetti in %s (%s occupati)", (long long)nchunks, dest, hs);
        free(hs);
    }
    r = OK;
#undef SAVE_STATE
out_sleep:
    SetThreadExecutionState(ES_CONTINUOUS);
out_dev:
    if (b.ram)
        VirtualFree(b.ram, 0, MEM_RELEASE);
    if (b.rbuf)
        VirtualFree(b.rbuf, 0, MEM_RELEASE);
    if (b.ebuf)
        VirtualFree(b.ebuf, 0, MEM_RELEASE);
    free(b.zbuf);
    if (b.cctx)
        ZSTD_freeCCtx(b.cctx);
    dev_close(&dev);
out_records:
out_crypto:
    if (crypto)
        crypto_free(crypto);
out_early:
    jfree(batch_records);
    free_chunks(done, ndone);
    jfree(manifest);
    jfree(info);
    free(dest);
    store_close(&store);
    rc_free(&rc);
    return r;
}

/* ===================================================================================================== */
/* RIPRISTINO / VERIFICA                                                                                   */
/* ===================================================================================================== */

static int parse_header(ChunkIn *in, JVal **out)
{
    uint8_t *blob = xmalloc(HEADER_SIZE + 1);
    if (in->size < HEADER_SIZE || chunkin_read_at(in, 0, blob, HEADER_SIZE) || memcmp(blob, MAGIC, MAGIC_LEN)) {
        free(blob);
        set_err("il file non e' un pacchetto .bkhd valido");
        return FAIL;
    }
    blob[HEADER_SIZE] = 0;
    size_t n = strlen((char *)blob + MAGIC_LEN);
    JVal *h = jparse((char *)blob + MAGIC_LEN, n);
    free(blob);
    if (!h || h->t != J_OBJ) {
        jfree(h);
        set_err("intestazione del pacchetto non leggibile");
        return FAIL;
    }
    *out = h;
    return OK;
}

static int sha256_payload(ChunkIn *in, uint8_t *buf, char out[65], Reporter *rep, int64_t index, int64_t chunks,
                          double base, double weight, double span_off, double span_len, double total)
{
    Hash h;
    hash_init(&h, NULL);
    int64_t pos = HEADER_SIZE, size = in->size - HEADER_SIZE;
    double t0 = mono_seconds();
    while (pos < in->size) {
        if (cancelled()) {
            set_err("operazione annullata");
            return CANCELLED;
        }
        size_t n = (size_t)(in->size - pos < IO_BLOCK ? in->size - pos : IO_BLOCK);
        if (chunkin_read_at(in, pos, buf, n))
            return FAIL;
        hash_update(&h, buf, n);
        pos += (int64_t)n;
        double frac = (double)(pos - HEADER_SIZE) / (double)(size > 0 ? size : 1);
        double el = mono_seconds() - t0;
        rep_progress(rep, 0, "verify", frac, (double)(pos - HEADER_SIZE) / (el > 1e-6 ? el : 1e-6), index, chunks,
                     (span_off + span_len * (base + weight * frac)) / total);
    }
    hash_final_hex(&h, out);
    return OK;
}

static int gcm_check(ChunkIn *in, const Crypto *c, const uint8_t nonce[12], const uint8_t tag[16], const char *aad,
                     uint8_t *buf, uint8_t *scratch)
{
    Gcm g;
    if (gcm_start(&g, c->enc_key, nonce, aad, 0))
        return FAIL;
    int64_t pos = HEADER_SIZE;
    while (pos < in->size) {
        if (cancelled()) {
            gcm_free(&g);
            set_err("operazione annullata");
            return CANCELLED;
        }
        size_t n = (size_t)(in->size - pos < IO_BLOCK ? in->size - pos : IO_BLOCK);
        if (chunkin_read_at(in, pos, buf, n)) {
            gcm_free(&g);
            return FAIL;
        }
        gcm_update(&g, buf, n, scratch);
        pos += (int64_t)n;
    }
    uint8_t got[16];
    gcm_finish(&g, scratch, got);
    gcm_free(&g);
    return ct_equal(got, tag, 16) ? OK : 1;           /* 1 = sigillo non valido */
}

/* flusso dei dati originali di un pacchetto: decifra, decomprime e calcola il checksum */
typedef struct {
    ChunkIn *in;
    int64_t pos;
    int crypto;
    Gcm gcm;
    uint8_t tag[16];
    int tag_bad, src_done;
    ZSTD_DCtx *dctx;
    size_t zlast;
    uint8_t *cbuf, *rbuf;    /* dati in chiaro pronti / letti dal pacchetto */
    size_t clen, coff;
    Hash hash;
    int64_t count;
} Stream;

static int stream_fill(Stream *s)
{
    s->clen = s->coff = 0;
    if (s->src_done)
        return OK;
    if (s->pos >= s->in->size) {
        s->src_done = 1;
        if (s->crypto) {
            uint8_t got[16];
            s->clen = gcm_finish(&s->gcm, s->cbuf, got);
            if (!ct_equal(got, s->tag, 16))
                s->tag_bad = 1;
        }
        return OK;
    }
    size_t n = (size_t)(s->in->size - s->pos < IO_BLOCK ? s->in->size - s->pos : IO_BLOCK);
    if (chunkin_read_at(s->in, s->pos, s->crypto ? s->rbuf : s->cbuf, n))
        return FAIL;
    s->pos += (int64_t)n;
    s->clen = s->crypto ? gcm_update(&s->gcm, s->rbuf, n, s->cbuf) : n;
    return OK;
}

/* legge fino a n byte; restituisce i byte letti (0 = fine) o -1 in caso di errore */
static int64_t stream_read(Stream *s, uint8_t *out, size_t n)
{
    size_t got = 0;
    while (got < n) {
        if (s->coff >= s->clen) {                    /* dati in chiaro esauriti */
            if (!s->src_done) {
                if (stream_fill(s))
                    return -1;
                continue;
            }
            if (!s->dctx)
                break;
            ZSTD_inBuffer in = {NULL, 0, 0};          /* svuota cio' che zstd tiene ancora da parte */
            ZSTD_outBuffer zo = {out + got, n - got, 0};
            size_t z = ZSTD_decompressStream(s->dctx, &zo, &in);
            if (ZSTD_isError(z)) {
                set_err("decompressione non riuscita: %s", ZSTD_getErrorName(z));
                return -1;
            }
            if (!zo.pos)                              /* niente di nuovo: lo stato del frame resta quello di prima */
                break;
            s->zlast = z;
            got += zo.pos;
            continue;
        }
        if (!s->dctx) {
            size_t k = s->clen - s->coff < n - got ? s->clen - s->coff : n - got;
            memcpy(out + got, s->cbuf + s->coff, k);
            s->coff += k;
            got += k;
            continue;
        }
        ZSTD_inBuffer in = {s->cbuf + s->coff, s->clen - s->coff, 0};
        ZSTD_outBuffer zo = {out + got, n - got, 0};
        size_t z = ZSTD_decompressStream(s->dctx, &zo, &in);
        if (ZSTD_isError(z)) {
            set_err("decompressione non riuscita: %s", ZSTD_getErrorName(z));
            return -1;
        }
        s->zlast = z;
        s->coff += in.pos;
        got += zo.pos;
        if (!in.pos && !zo.pos)
            break;
    }
    hash_update(&s->hash, out, got);
    s->count += (int64_t)got;
    return (int64_t)got;
}

static int stream_read_exact(Stream *s, uint8_t *out, size_t n, const char *what)
{
    int64_t k = stream_read(s, out, n);
    if (k < 0)
        return FAIL;
    if ((size_t)k != n) {
        set_err("pacchetto troncato (%s)", what);
        return FAIL;
    }
    return OK;
}

static int all_zero(const uint8_t *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        if (p[i])
            return 0;
    return 1;
}

typedef struct {
    const RestoreOpts *o;
    Reporter *rep;
    Store *store;
    JVal *manifest;
    const Crypto *crypto;
    Dev *target;             /* NULL = solo verifica */
    int64_t total, nchunks;
    uint8_t *ram;
    size_t ram_cap;
    uint8_t *buf, *scratch, *cbuf, *rbuf, *obuf;
} RestoreCtx;

static void download_progress(void *p, int64_t done, int64_t tot, double speed)
{
    ProgCtx *c = p;
    double frac = tot ? (double)done / (double)tot : 0;
    if (frac > 1)
        frac = 1;
    double el = mono_seconds() - c->t0;
    if (!speed)
        speed = (double)done / (el > 1e-6 ? el : 1e-6);
    rep_progress(c->rep, 0, "download", frac, speed, c->chunk, c->chunks,
                 ((double)c->offset + (double)c->length * 0.6 * frac) / (double)c->total);
}

static int restore_chunk(RestoreCtx *x, const Chunk *c)
{
    Reporter *rep = x->rep;
    if (c->empty)
        return OK;
    const char *comp = jget_str(x->manifest, "compression");
    int zstd = !strcmp(comp, "zstd");
    int64_t file_size = HEADER_SIZE + c->payload_size;
    double dl_w = x->store->local ? 0.0 : 0.6, ver_w = x->store->local ? 0.3 : 0.1,
           wr_w = x->store->local ? 0.7 : 0.3;
    if (!x->store->local) {
        char *fs = human((double)file_size);
        char *lbl = xasprintf("download di %s (%s) in RAM", c->file, fs);
        rep_phase(rep, "download", c->index, x->nchunks, lbl);
        free(lbl);
        free(fs);
        if ((size_t)file_size > x->ram_cap) {
            set_err("pacchetto %s piu' grande della memoria riservata", c->file);
            return FAIL;
        }
    }
    char *aad = x->crypto ? crypto_aad(jget_str(x->manifest, "backup_id"), c->index, c->offset, c->length, comp,
                                       c->sparse)
                          : NULL;
    ChunkIn in;
    memset(&in, 0, sizeof in);
    JVal *hdr = NULL;
    uint8_t nonce[12], tag[16];
    char *problem = NULL;
    int r = FAIL;
    for (int attempt = 0; attempt < (x->store->local ? 1 : 3); attempt++) {
        ProgCtx pc = {rep, c->index, x->nchunks, c->offset, c->length, x->total, mono_seconds()};
        r = store_chunk_fetch(x->store, c->file, file_size, x->ram, x->ram_cap, download_progress, &pc, &in);
        if (r)
            goto out;
        r = FAIL;
        rep_phase(rep, "verify", c->index, x->nchunks, x->store->local ? "verifica SHA-256" : "verifica SHA-256 in RAM");
        free(problem);
        problem = NULL;
        jfree(hdr);
        hdr = NULL;
        if (parse_header(&in, &hdr)) {
            problem = xstrdup(last_err());
        } else {
            int64_t hi = -1, ho = -1, hl = -1;
            const char *hid = jget_str(hdr, "backup_id");
            jget_int(hdr, "index", &hi);
            jget_int(hdr, "offset", &ho);
            jget_int(hdr, "length", &hl);
            if (!hid || strcmp(hid, jget_str(x->manifest, "backup_id")) || hi != c->index || ho != c->offset ||
                hl != c->length)
                problem = xstrdup("intestazione non corrispondente al manifest");
            else if (in.size != file_size)
                problem = xstrdup("dimensione errata");
        }
        if (!problem) {
            char sha[65];
            int e = sha256_payload(&in, x->buf, sha, rep, c->index, x->nchunks, dl_w, ver_w, (double)c->offset,
                                   (double)c->length, (double)x->total);
            if (e == CANCELLED) {
                r = CANCELLED;
                goto out;
            }
            if (e)
                problem = xstrdup(last_err());
            else if (strcmp(sha, c->payload))
                problem = xstrdup("checksum SHA-256 non corrispondente");
        }
        if (!problem && x->crypto) {
            JVal *encv = jobj_get(hdr, "encrypted");
            if (!encv || encv->t != J_BOOL || !encv->b) {
                problem = xstrdup("pacchetto non cifrato in un backup protetto");
            } else if (hex_decode(jget_str(hdr, "nonce"), nonce, 12) || hex_decode(jget_str(hdr, "tag"), tag, 16)) {
                problem = xstrdup("sigillo di autenticita' non valido (pacchetto manomesso o danneggiato)");
            } else {
                int g = gcm_check(&in, x->crypto, nonce, tag, aad, x->buf, x->scratch);
                if (g == CANCELLED) {
                    r = CANCELLED;
                    goto out;
                }
                if (g)
                    problem = xstrdup("sigillo di autenticita' non valido (pacchetto manomesso o danneggiato)");
            }
        }
        if (!problem)
            break;
        chunkin_close(&in);
        if (!x->store->local)
            rep_log(rep, "warning", "%s: %s. Lo riscarico.", c->file, problem);
    }
    if (problem) {
        set_err("il pacchetto %s %s e' danneggiato (%s)", c->file, x->store->local ? "sul disco" : "sul cloud", problem);
        goto out;
    }
    if (!x->target && !zstd) {               /* verifica: il file coincide gia' con i dati originali */
        r = OK;
        goto out;
    }
    rep_phase(rep, "write", c->index, x->nchunks, x->target ? "scrittura sul disco" : "verifica dei dati decompressi");
    Stream s;
    memset(&s, 0, sizeof s);
    s.in = &in;
    s.pos = HEADER_SIZE;
    s.cbuf = x->cbuf;
    s.rbuf = x->rbuf;
    hash_init(&s.hash, x->crypto ? x->crypto->mac_key : NULL);
    if (x->crypto) {
        if (gcm_start(&s.gcm, x->crypto->enc_key, nonce, aad, 0))
            goto out;
        memcpy(s.tag, tag, 16);
        s.crypto = 1;
    }
    if (zstd)
        s.dctx = ZSTD_createDCtx();
    int64_t expected = c->sparse ? c->used : c->length, written = 0;
    double t0 = mono_seconds();
    int64_t nparts = 1;
    uint64_t *parts = NULL;
    if (c->sparse) {
        uint8_t cnt[8];
        if (stream_read_exact(&s, cnt, 8, "tabella"))
            goto out_stream;
        uint64_t n = 0;
        for (int k = 7; k >= 0; k--)
            n = n << 8 | cnt[k];
        if (n > (uint64_t)c->length / 512 + 1 || n > (1u << 20)) {
            set_err("%s: tabella delle zone non valida", c->file);
            goto out_stream;
        }
        nparts = (int64_t)n;
        parts = xmalloc((size_t)(n ? n : 1) * 16);
        if (n && stream_read_exact(&s, (uint8_t *)parts, (size_t)n * 16, "tabella"))
            goto out_parts;
    }
    for (int64_t p = 0; p < nparts; p++) {
        uint64_t rel = parts ? parts[2 * p] : 0, ln = parts ? parts[2 * p + 1] : (uint64_t)c->length;
        if (rel + ln > (uint64_t)c->length || rel + ln < rel) {
            set_err("%s: zona fuori dal pacchetto", c->file);
            goto out_parts;
        }
        uint64_t pos = 0;
        while (pos < ln) {
            if (cancelled()) {
                set_err("operazione annullata");
                r = CANCELLED;
                goto out_parts;
            }
            size_t k = (size_t)(ln - pos < (uint64_t)IO_BLOCK ? ln - pos : (uint64_t)IO_BLOCK);
            if (stream_read_exact(&s, x->obuf, k, "dati"))
                goto out_parts;
            if (x->target && !(!x->target->block && all_zero(x->obuf, k)))     /* file: resta sparso */
                if (dev_write_at(x->target, c->offset + (int64_t)(rel + pos), x->obuf, k))
                    goto out_parts;
            pos += k;
            written += (int64_t)k;
            double el = mono_seconds() - t0;
            double frac = (double)written / (double)(expected > 0 ? expected : 1);
            rep_progress(rep, 0, "write", frac, (double)written / (el > 1e-6 ? el : 1e-6), c->index, x->nchunks,
                         ((double)c->offset + (double)c->length * (dl_w + ver_w + wr_w * frac)) / (double)x->total);
        }
    }
    {
        uint8_t extra;
        int64_t k = stream_read(&s, &extra, 1);
        if (k < 0)
            goto out_parts;
        if (k) {
            set_err("%s: dati piu' lunghi del previsto", c->file);
            goto out_parts;
        }
    }
    if (s.tag_bad) {
        set_err("decifratura non riuscita: sigillo non valido");
        goto out_parts;
    }
    if (s.dctx && s.zlast != 0) {
        set_err("decompressione non riuscita (dati compressi incompleti)");
        goto out_parts;
    }
    {
        char raw[65];
        hash_final_hex(&s.hash, raw);
        if (strcmp(raw, c->raw)) {
            set_err("%s: i dati estratti non corrispondono al checksum originale", c->file);
            goto out_parts;
        }
    }
    if (x->target && dev_flush(x->target))              /* dati al sicuro sul disco prima di liberare la RAM */
        goto out_parts;
    r = OK;
out_parts:
    free(parts);
out_stream:
    if (s.crypto)
        gcm_free(&s.gcm);
    if (s.dctx)
        ZSTD_freeDCtx(s.dctx);
out:
    jfree(hdr);
    free(problem);
    free(aad);
    chunkin_close(&in);
    return r;
}

int engine_restore(const RestoreOpts *o, Reporter *rep, char **done_msg)
{
    *done_msg = NULL;
    int r = FAIL;
    int cloud = !path_is_local(o->remote);
    Rclone rc;
    memset(&rc, 0, sizeof rc);
    if (cloud && rc_init(&rc, o->rclone, o->rclone_config))
        return FAIL;
    Store store;
    if (store_open(&store, o->remote, o->path, NULL, cloud ? &rc : NULL, rep, o->retries)) {
        rc_free(&rc);
        return FAIL;
    }
    JVal *manifest = NULL;
    Chunk *chunks = NULL;
    int64_t nch = 0;
    Crypto crypto_s, *crypto = NULL;
    Dev target;
    memset(&target, 0, sizeof target);
    int have_target = 0;
    RestoreCtx x;
    memset(&x, 0, sizeof x);
    if ((r = load_manifest(&store, NULL, &manifest)))
        goto out;
    r = FAIL;
    int64_t total = mget(jobj_get(manifest, "source"), "size"), cc = mget(manifest, "chunk_count");
    const JVal *enc = mencryption(manifest);
    if (enc) {
        if (crypto_open(&crypto_s, o->password, enc))
            goto out;
        crypto = &crypto_s;
        if (crypto_verify(crypto, manifest, "Manifest"))
            goto out;
        rep_log(rep, "info", "Password corretta: i pacchetti verranno decifrati e verificati.");
    } else if (o->expect_encrypted) {
        set_err("il backup risulta NON protetto da password ma era stato creato protetto: possibile manomissione, "
                "ripristino annullato");
        goto out;
    }
    if ((r = load_chunks(&store, manifest, crypto, &chunks, &nch)))
        goto out;
    r = FAIL;
    const char *status = jget_str(manifest, "status");
    if (!strcmp(status, "complete") && nch != cc) {
        set_err("indice del backup incompleto: trovati %lld pacchetti su %lld", (long long)nch, (long long)cc);
        goto out;
    }
    if (strcmp(status, "complete")) {
        if (!o->allow_incomplete) {
            set_err("il backup e' incompleto (interrotto). Usa --allow-incomplete per ripristinare solo i %lld "
                    "pacchetti disponibili.", (long long)nch);
            goto out;
        }
        rep_log(rep, "warning", "Backup incompleto: verranno ripristinati solo %lld pacchetti.", (long long)nch);
    }
    int64_t first = o->from_chunk > 1 ? o->from_chunk : 1;
    if (!o->verify_only) {
        if (!o->target || !*o->target) {
            set_err("specifica la destinazione (--target) oppure --verify-only");
            goto out;
        }
        if (!o->yes) {
            set_err("il ripristino cancella la destinazione: conferma con --yes");
            goto out;
        }
        if (dev_open_restore(o->target, total, &target, store.local ? store.base : NULL))
            goto out;
        have_target = 1;
        if (target.block) {
            if (target.size > total) {
                char *a = human((double)target.size), *b = human((double)total);
                rep_log(rep, "warning", "La destinazione e' piu' grande del disco originale (%s > %s): lo spazio in "
                                        "eccesso restera' non allocato.", a, b);
                free(a);
                free(b);
            }
        } else {
            LARGE_INTEGER zero = {{0, 0}}, sz;
            sz.QuadPart = total;
            if ((first == 1 && (!SetFilePointerEx(target.h, zero, NULL, FILE_BEGIN) || !SetEndOfFile(target.h))) ||
                !SetFilePointerEx(target.h, sz, NULL, FILE_BEGIN) || !SetEndOfFile(target.h)) {
                set_win_err(GetLastError(), "impossibile preparare il file %s", o->target);
                goto out;
            }
        }
        if (msparse(manifest))
            rep_log(rep, "info", "Backup 'solo settori usati': vengono scritti solo i settori occupati; lo spazio "
                                 "libero dei file system sulla destinazione non viene sovrascritto.");
    }
    size_t need = 0;
    if (!store.local) {
        int64_t maxp = 0;
        for (int64_t k = 0; k < nch; k++)
            if (chunks[k].index >= first && chunks[k].payload_size > maxp)
                maxp = chunks[k].payload_size;
        need = (size_t)(maxp + HEADER_SIZE);
        if ((uint64_t)maxp + HEADER_SIZE > (uint64_t)SIZE_MAX / 2) {
            set_err("pacchetti troppo grandi per la versione a 32 bit: usa la versione a 64 bit o la pennetta "
                    "BKHD Live");
            goto out;
        }
        uint64_t avail = mem_available();
        if (avail && avail < (uint64_t)need + RAM_MARGIN) {
            char *a = human((double)need + RAM_MARGIN), *b = human((double)avail);
            set_err("RAM insufficiente: servono %s, disponibili %s", a, b);
            free(a);
            free(b);
            goto out;
        }
        x.ram = big_alloc(need);
        x.ram_cap = need;
        if (!x.ram) {
            set_err("memoria insufficiente per un pacchetto da %lld MB%s", (long long)(need / MIB),
                    sizeof(void *) == 4 ? ": usa la versione a 64 bit o la pennetta BKHD Live" : "");
            goto out;
        }
    }
    const char *mode = o->verify_only ? "verify" : "restore";
    {
        char *t = human((double)total);
        if (o->verify_only)
            rep_log(rep, "info", "Backup '%s' del %s (%s, %lld pacchetti) - solo verifica", jget_str(manifest, "name"),
                    jget_str(manifest, "created") ? jget_str(manifest, "created") : "?", t, (long long)nch);
        else
            rep_log(rep, "info", "Backup '%s' del %s (%s, %lld pacchetti) -> %s", jget_str(manifest, "name"),
                    jget_str(manifest, "created") ? jget_str(manifest, "created") : "?", t, (long long)nch, o->target);
        free(t);
    }
    emit_start(rep, mode, total, cc, mget(manifest, "chunk_size"), o->target ? o->target : "", first, NULL, 0);
    x.o = o;
    x.rep = rep;
    x.store = &store;
    x.manifest = manifest;
    x.crypto = crypto;
    x.target = have_target ? &target : NULL;
    x.total = total;
    x.nchunks = cc;
    x.buf = big_alloc(IO_BLOCK);
    x.scratch = big_alloc(IO_BLOCK + 64);
    x.cbuf = big_alloc(IO_BLOCK + 64);
    x.rbuf = big_alloc(IO_BLOCK);
    x.obuf = big_alloc(IO_BLOCK);
    if (!x.buf || !x.scratch || !x.cbuf || !x.rbuf || !x.obuf) {
        set_err("memoria insufficiente");
        goto out;
    }
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED);
    int64_t todo = 0;
    for (int64_t k = 0; k < nch; k++)
        todo += chunks[k].index >= first;
    int quiet = todo > QUIET_THRESHOLD;
    double last_log = mono_seconds();
    int64_t n = 0;
    for (int64_t k = 0; k < nch; k++) {
        Chunk *c = &chunks[k];
        if (c->index < first)
            continue;
        n++;
        if (cancelled()) {
            set_err("operazione annullata");
            r = CANCELLED;
            goto out_sleep;
        }
        if ((r = restore_chunk(&x, c)))
            goto out_sleep;
        r = FAIL;
        char *msg;
        if (c->empty)
            msg = xasprintf("Pacchetto %lld/%lld: solo spazio libero.", (long long)c->index, (long long)cc);
        else
            msg = xasprintf("Pacchetto %lld/%lld %s%s", (long long)c->index, (long long)cc,
                            o->verify_only ? "verificato" : "ripristinato", store.local ? "." : " e rimosso dalla RAM.");
        if (quiet) {
            free(msg);
            msg = NULL;
            if (mono_seconds() - last_log >= 60 || n == todo) {
                last_log = mono_seconds();
                msg = xasprintf("%lld/%lld pacchetti %s.", (long long)c->index, (long long)cc,
                                o->verify_only ? "verificati" : "ripristinati");
            }
        }
        emit_chunk_done(rep, c->index, cc, msg,
                        (double)(c->offset + c->length < total ? c->offset + c->length : total) / (double)total, -1);
        free(msg);
    }
    if (have_target) {
        if (dev_flush(&target))
            goto out_sleep;
        if (target.block)
            rep_log(rep, "info", "Tabella delle partizioni ricaricata.");
        const char *pt = jget_str(jobj_get(manifest, "source"), "pttype");
        if (target.block && target.size > total && pt && !strcmp(pt, "gpt"))
            rep_log(rep, "info", "Disco GPT piu' grande dell'originale: con Gestione disco o con 'sgdisk -e' (BKHD Live) "
                                 "si puo' spostare l'intestazione GPT di riserva alla fine del disco.");
    }
    if (o->verify_only)
        *done_msg = xasprintf("Verifica completata: tutti i %lld pacchetti sono integri.", (long long)todo);
    else
        *done_msg = xasprintf("Ripristino completato su %s.", o->target);
    r = OK;
out_sleep:
    SetThreadExecutionState(ES_CONTINUOUS);
out:
    if (x.ram)
        VirtualFree(x.ram, 0, MEM_RELEASE);
    uint8_t *bufs[] = {x.buf, x.scratch, x.cbuf, x.rbuf, x.obuf};
    for (int k = 0; k < 5; k++)
        if (bufs[k])
            VirtualFree(bufs[k], 0, MEM_RELEASE);
    if (have_target)
        dev_close(&target);
    free_chunks(chunks, nch);
    if (crypto)
        crypto_free(crypto);
    jfree(manifest);
    store_close(&store);
    rc_free(&rc);
    return r;
}

/* ===================================================================================================== */
/* ELENCO                                                                                                  */
/* ===================================================================================================== */

int engine_list(const ListOpts *o, Reporter *rep, JVal **items)
{
    *items = NULL;
    int cloud = !path_is_local(o->remote);
    Rclone rc;
    memset(&rc, 0, sizeof rc);
    if (cloud && rc_init(&rc, o->rclone, o->rclone_config))
        return FAIL;
    Store store;
    if (store_open(&store, o->remote, o->path, NULL, cloud ? &rc : NULL, rep, 1)) {
        rc_free(&rc);
        return FAIL;
    }
    StrList dirs = {0};
    int r = store_list_names(&store, NULL, 1, &dirs);
    if (r) {
        sl_free(&dirs);
        store_close(&store);
        rc_free(&rc);
        return r;
    }
    JVal *out = jarr();
    for (size_t k = 0; k <= dirs.n; k++) {
        const char *d = k == 0 ? "" : dirs.v[k - 1];
        StrList names = {0};
        int lr = store_list_names(&store, d, 0, &names);
        int has = !lr && sl_has(&names, "manifest.json");
        sl_free(&names);
        if (lr == CANCELLED) {
            r = CANCELLED;
            break;
        }
        if (!has)
            continue;
        JVal *m = NULL;
        if (load_manifest(&store, d, &m))
            continue;
        char *where = store_where(&store, d);
        jobj_set(m, "_dir", jstr(where));
        jobj_set(m, "_rel", jstr(d));
        free(where);
        if (mget(m, "version") >= 2) {
            jobj_set(m, "_chunks_done", jint(mget(m, "chunks_done")));
            jobj_set(m, "_stored_bytes", jint(mget(m, "stored_bytes")));
        } else {
            JVal *ch = jobj_get(m, "chunks");
            int64_t stored = 0;
            for (size_t q = 0; ch && q < ch->n; q++)
                stored += mget(ch->items[q], "payload_size") + HEADER_SIZE;
            jobj_set(m, "_chunks_done", jint(ch ? (int64_t)ch->n : 0));
            jobj_set(m, "_stored_bytes", jint(stored));
        }
        jobj_del(m, "chunks");
        const char *st = jget_str(m, "status");
        if (o->interrupted && st && !strcmp(st, "complete")) {
            jfree(m);
            continue;
        }
        jarr_push(out, m);
    }
    sl_free(&dirs);
    store_close(&store);
    rc_free(&rc);
    if (r) {
        jfree(out);
        return r;
    }
    *items = out;
    return OK;
}
