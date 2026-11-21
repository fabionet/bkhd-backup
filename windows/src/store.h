/* BKHD Backup per Windows - destinazioni: cloud (rclone, pacchetto in RAM) o cartella locale/di rete.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_STORE_H
#define BKHD_STORE_H
#include "util.h"
#include "json.h"
#include "rclone.h"
#include "report.h"

#define MAX_META (256 * MIB)

typedef struct {
    int local;
    char *base;              /* cloud: remote:cartella/nome; locale: percorso di Windows */
    Rclone *rc;
    Reporter *rep;
    int retries;
} Store;

int store_open(Store *s, const char *remote, const char *a, const char *b, Rclone *rc, Reporter *rep, int retries);
void store_close(Store *s);
char *store_where(const Store *s, const char *rel);
int store_list_names(Store *s, const char *rel, int dirs, StrList *out);
int store_read(Store *s, const char *rel, StrBuf *out);
int store_write_json(Store *s, const char *rel, const JVal *data, const char *what);
int store_prepare(Store *s);
int store_check_space(Store *s, int64_t need);
int store_delete_backup_files(Store *s);

/* pacchetto in scrittura: in RAM (cloud) o file .partial (locale) */
typedef struct {
    int local;
    uint8_t *mem;
    size_t cap, len;
    HANDLE h;
    wchar_t *partial, *final;
} ChunkOut;

int store_chunk_create(Store *s, const char *rel, uint8_t *mem, size_t cap, ChunkOut *o);
int chunkout_write(ChunkOut *o, const void *data, size_t n);
int chunkout_header(ChunkOut *o, const uint8_t *hdr, size_t n);
int store_chunk_commit(Store *s, ChunkOut *o, const char *rel, ProgressFn fn, void *ctx, int verify);
void store_chunk_discard(ChunkOut *o);

/* pacchetto in lettura: scaricato in RAM (cloud) o file aperto senza condivisione in scrittura (locale) */
typedef struct {
    uint8_t *mem;
    HANDLE h;
    int64_t size;
} ChunkIn;

int store_chunk_fetch(Store *s, const char *rel, int64_t max, uint8_t *mem, size_t memcap, ProgressFn fn, void *ctx,
                      ChunkIn *in);
int chunkin_read_at(ChunkIn *in, int64_t off, void *buf, size_t n);
void chunkin_close(ChunkIn *in);

#endif
