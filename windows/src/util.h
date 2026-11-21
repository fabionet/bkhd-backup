/* BKHD Backup per Windows - utilita' comuni.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_UTIL_H
#define BKHD_UTIL_H

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0501          /* Windows XP */
#endif
#ifndef WINVER
#define WINVER 0x0501
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>

#define BKHD_VERSION "1.0.0-beta1"
#define BKHD_ENGINE_ID "win-" BKHD_VERSION
#define BKHD_AUTHOR "fabionet"
#define BKHD_LICENSE "GPL-3.0-or-later"

#define KIB 1024LL
#define MIB (1024LL * 1024)
#define GIB (1024LL * MIB)

/* codici di ritorno delle funzioni del motore */
#define OK 0
#define FAIL (-1)
#define CANCELLED (-2)

/* --- stringhe dinamiche ------------------------------------------------------------------------- */
typedef struct {
    char *s;
    size_t n, cap;
} StrBuf;

void sb_init(StrBuf *b);
void sb_free(StrBuf *b);
void sb_putn(StrBuf *b, const char *s, size_t n);
void sb_puts(StrBuf *b, const char *s);
void sb_putc(StrBuf *b, char c);
void sb_printf(StrBuf *b, const char *fmt, ...);
char *sb_steal(StrBuf *b);              /* restituisce la stringa (da liberare con free) */

char *xstrdup(const char *s);
char *xasprintf(const char *fmt, ...);
char *xvasprintf(const char *fmt, va_list ap);
void *xmalloc(size_t n);
void *xcalloc(size_t n, size_t m);
void *xrealloc(void *p, size_t n);

/* elenco di stringhe */
typedef struct {
    char **v;
    size_t n, cap;
} StrList;
void sl_push(StrList *l, const char *s);
void sl_free(StrList *l);
void sl_sort(StrList *l);
int sl_has(const StrList *l, const char *s);

/* --- UTF-8 <-> UTF-16 --------------------------------------------------------------------------- */
wchar_t *wide(const char *utf8);        /* da liberare con free */
char *utf8(const wchar_t *w);

/* --- errori e annullamento ---------------------------------------------------------------------- */
void set_err(const char *fmt, ...);
void set_win_err(DWORD code, const char *fmt, ...);   /* aggiunge il messaggio di Windows */
const char *last_err(void);
char *win_msg(DWORD code);
extern volatile LONG g_cancel;
int cancelled(void);                    /* 1 se l'utente ha chiesto di annullare */
int cancel_wait(DWORD ms);              /* attende; 1 se annullato nel frattempo */

/* --- varie -------------------------------------------------------------------------------------- */
char *now_iso(void);
char *human(double n);
int random_bytes(void *buf, size_t n);
void hex_encode(const uint8_t *in, size_t n, char *out);   /* out: 2n+1 */
int hex_decode(const char *hex, uint8_t *out, size_t n);   /* 0 se valido */
char *new_uuid4(void);
uint64_t mem_available(void);
char *exe_dir(void);
int path_is_local(const char *remote);  /* percorso assoluto di Windows (C:\..., \\server\...) */
int valid_name(const char *s);          /* [A-Za-z0-9][A-Za-z0-9._-]{0,120} */
double mono_seconds(void);

#endif
