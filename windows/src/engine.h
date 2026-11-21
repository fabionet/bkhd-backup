/* BKHD Backup per Windows - motore di backup, ripristino ed elenco (formato identico al motore Linux).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_ENGINE_H
#define BKHD_ENGINE_H
#include "util.h"
#include "json.h"
#include "report.h"

#define HEADER_SIZE 4096
#define MIN_CHUNK_KIB 1200LL                 /* floppy 5,25" da 1,2 MB */
#define MAX_CHUNK_KIB (8192LL * 1024)         /* 8 GB */
#ifdef _WIN64
#define MAX_CLOUD_CHUNK_KIB MAX_CHUNK_KIB
#else
#define MAX_CLOUD_CHUNK_KIB (700LL * 1024)    /* 32 bit: il pacchetto deve stare nello spazio di indirizzi */
#endif

typedef struct {
    const char *source, *remote, *path, *name;
    int64_t chunk_kib;
    const char *compression;     /* "none" o "zstd" */
    int level;
    int encrypt, resume, expect_encrypted, overwrite;
    const char *bad_sectors;     /* "retry" o "skip" */
    const char *password;
    const char *rclone, *rclone_config;
    int retries;
} BackupOpts;

typedef struct {
    const char *remote, *path, *target;
    int verify_only, allow_incomplete, expect_encrypted, yes;
    int64_t from_chunk;
    const char *password;
    const char *rclone, *rclone_config;
    int retries;
} RestoreOpts;

typedef struct {
    const char *remote, *path;
    int interrupted;
    const char *rclone, *rclone_config;
} ListOpts;

void backup_opts_default(BackupOpts *o);
void restore_opts_default(RestoreOpts *o);
int64_t parse_size_kib(const char *text);    /* -1 se non valido */

/* restituiscono OK, FAIL (messaggio in last_err()) o CANCELLED; *done_msg = messaggio finale */
int engine_backup(const BackupOpts *o, Reporter *rep, char **done_msg);
int engine_restore(const RestoreOpts *o, Reporter *rep, char **done_msg);
int engine_list(const ListOpts *o, Reporter *rep, JVal **items);

#endif
