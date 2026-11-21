/* BKHD Backup per Windows - trasferimenti con rclone.exe (rcat/cat a flusso, nessun file temporaneo).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_RCLONE_H
#define BKHD_RCLONE_H
#include "util.h"

typedef struct {
    char *binary;
    char *config;
} Rclone;

typedef void (*ProgressFn)(void *ctx, int64_t done, int64_t total, double speed);

int rc_init(Rclone *rc, const char *binary, const char *config);
void rc_free(Rclone *rc);
char *rc_find(void);                     /* rclone.exe accanto al programma o nel PATH; NULL se assente */

/* le funzioni che restituiscono "ok" (1/0) mettono in *err il motivo del fallimento: servono ai nuovi tentativi */
int rc_listremotes(Rclone *rc, StrList *out);
int rc_list_names(Rclone *rc, const char *dir, int dirs, StrList *out);
int rc_cat(Rclone *rc, const char *file, int64_t max, StrBuf *out);
int rc_upload(Rclone *rc, const char *remote, const uint8_t *data, size_t n, ProgressFn fn, void *ctx, char **err);
int rc_download(Rclone *rc, const char *remote, uint8_t *buf, size_t max, size_t *got, ProgressFn fn, void *ctx,
                char **err);
int rc_remote_size(Rclone *rc, const char *file, int64_t *size);
int rc_delete_backup_files(Rclone *rc, const char *dir);
int rc_open_config_console(Rclone *rc);  /* "rclone config" in una nuova finestra */
char *remote_join(const char *remote, const char *a, const char *b);

#endif
