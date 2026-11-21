/* BKHD Backup per Windows - dischi fisici, volumi e file immagine.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_DISK_H
#define BKHD_DISK_H
#include "util.h"
#include "json.h"

typedef struct {
    int number;                /* N di \\.\PhysicalDriveN */
    char *path;
    char *model, *serial, *bus;
    int64_t size;
    int logical_sector, physical_sector;
    char *pttype, *ptuuid;     /* "gpt"/"dos" come lsblk */
    int removable;
    char *letters;             /* "C: D:" */
    int is_system;             /* contiene Windows */
    int volumes;               /* volumi montati */
} DiskInfo;

int disk_list(DiskInfo **out, int *count);
void disk_list_free(DiskInfo *d, int count);
char *disk_label(const DiskInfo *d);          /* testo per gli elenchi dell'interfaccia */

/* dispositivo (disco, volume o file immagine) aperto per leggere o scrivere */
typedef struct {
    HANDLE h;
    int block;                 /* 1 = disco o volume, 0 = file */
    int sector;
    int64_t size;
    HANDLE locks[64];          /* volumi bloccati durante il ripristino */
    int nlocks;
    uint8_t *bounce;           /* settore di appoggio per scritture non allineate */
} Dev;

int path_is_device(const char *path);                       /* \\.\... */
int source_info(const char *path, JVal **info);              /* informazioni per il manifest */
int dev_open_read(const char *path, Dev *d);
int dev_open_restore(const char *path, int64_t min_size, Dev *d, const char *backup_dir);
int dev_read_at(Dev *d, int64_t off, void *buf, size_t n, DWORD *werr);
int dev_write_at(Dev *d, int64_t off, const void *buf, size_t n);
int dev_flush(Dev *d);
void dev_close(Dev *d);
int read_error_is_bad(DWORD e);
int disk_of_path(const char *path, int *numbers, int max);  /* dischi su cui si trova un file o un volume */
int device_disk_number(const char *dev);                     /* -1 se non e' un disco o volume */
char *in_use_reason(const char *dev);                        /* NULL se non in uso */

#endif
