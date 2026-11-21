/* BKHD Backup per Windows - utilita' comuni.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "util.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* RtlGenRandom (advapi32), disponibile da Windows XP */
BOOLEAN NTAPI SystemFunction036(PVOID buffer, ULONG length);

static void oom(void)
{
    MessageBoxA(NULL, "Memoria esaurita.", "BKHD Backup", MB_ICONERROR);
    ExitProcess(3);
}

void *xmalloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p)
        oom();
    return p;
}

void *xcalloc(size_t n, size_t m)
{
    void *p = calloc(n ? n : 1, m ? m : 1);
    if (!p)
        oom();
    return p;
}

void *xrealloc(void *p, size_t n)
{
    void *q = realloc(p, n ? n : 1);
    if (!q)
        oom();
    return q;
}

char *xstrdup(const char *s)
{
    size_t n = strlen(s ? s : "");
    char *r = xmalloc(n + 1);
    memcpy(r, s ? s : "", n + 1);
    return r;
}

char *xvasprintf(const char *fmt, va_list ap)
{
    va_list ap2;
    va_copy(ap2, ap);
    int n = vsnprintf(NULL, 0, fmt, ap2);
    va_end(ap2);
    if (n < 0)
        n = 0;
    char *s = xmalloc((size_t)n + 1);
    vsnprintf(s, (size_t)n + 1, fmt, ap);
    return s;
}

char *xasprintf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    char *s = xvasprintf(fmt, ap);
    va_end(ap);
    return s;
}

/* --- StrBuf -------------------------------------------------------------------------------------- */
void sb_init(StrBuf *b)
{
    b->s = NULL;
    b->n = b->cap = 0;
}

void sb_free(StrBuf *b)
{
    free(b->s);
    sb_init(b);
}

static void sb_grow(StrBuf *b, size_t extra)
{
    if (b->n + extra + 1 <= b->cap)
        return;
    size_t cap = b->cap ? b->cap : 64;
    while (cap < b->n + extra + 1)
        cap *= 2;
    b->s = xrealloc(b->s, cap);
    b->cap = cap;
}

void sb_putn(StrBuf *b, const char *s, size_t n)
{
    sb_grow(b, n);
    memcpy(b->s + b->n, s, n);
    b->n += n;
    b->s[b->n] = 0;
}

void sb_puts(StrBuf *b, const char *s)
{
    sb_putn(b, s, strlen(s));
}

void sb_putc(StrBuf *b, char c)
{
    sb_putn(b, &c, 1);
}

void sb_printf(StrBuf *b, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    char *s = xvasprintf(fmt, ap);
    va_end(ap);
    sb_puts(b, s);
    free(s);
}

char *sb_steal(StrBuf *b)
{
    char *s = b->s ? b->s : xstrdup("");
    sb_init(b);
    return s;
}

/* --- StrList ------------------------------------------------------------------------------------- */
void sl_push(StrList *l, const char *s)
{
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 16;
        l->v = xrealloc(l->v, l->cap * sizeof(char *));
    }
    l->v[l->n++] = xstrdup(s);
}

void sl_free(StrList *l)
{
    for (size_t i = 0; i < l->n; i++)
        free(l->v[i]);
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

static int cmp_str(const void *a, const void *b)
{
    return strcmp(*(char *const *)a, *(char *const *)b);
}

void sl_sort(StrList *l)
{
    if (l->n > 1)
        qsort(l->v, l->n, sizeof(char *), cmp_str);
}

int sl_has(const StrList *l, const char *s)
{
    for (size_t i = 0; i < l->n; i++)
        if (!strcmp(l->v[i], s))
            return 1;
    return 0;
}

/* --- UTF-8 <-> UTF-16 ---------------------------------------------------------------------------- */
wchar_t *wide(const char *s)
{
    if (!s)
        s = "";
    int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    if (n <= 0) {                       /* testo non UTF-8: lo si interpreta con la code page di sistema */
        n = MultiByteToWideChar(CP_ACP, 0, s, -1, NULL, 0);
        wchar_t *w = xmalloc(sizeof(wchar_t) * (size_t)(n > 0 ? n : 1));
        if (n <= 0 || !MultiByteToWideChar(CP_ACP, 0, s, -1, w, n))
            w[0] = 0;
        return w;
    }
    wchar_t *w = xmalloc(sizeof(wchar_t) * (size_t)n);
    MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n);
    return w;
}

char *utf8(const wchar_t *w)
{
    if (!w)
        w = L"";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *s = xmalloc((size_t)(n > 0 ? n : 1));
    if (n <= 0 || !WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL))
        s[0] = 0;
    return s;
}

/* --- errori -------------------------------------------------------------------------------------- */
static char g_errbuf[2048];
volatile LONG g_cancel = 0;

void set_err(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_errbuf, sizeof g_errbuf, fmt, ap);
    va_end(ap);
}

char *win_msg(DWORD code)
{
    wchar_t *buf = NULL;
    DWORD n = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                             NULL, code, 0, (LPWSTR)&buf, 0, NULL);
    char *s;
    if (n && buf) {
        while (n && (buf[n - 1] == L'\r' || buf[n - 1] == L'\n' || buf[n - 1] == L' ' || buf[n - 1] == L'.'))
            buf[--n] = 0;
        s = utf8(buf);
        LocalFree(buf);
    } else {
        s = xasprintf("errore di sistema %lu", (unsigned long)code);
    }
    return s;
}

void set_win_err(DWORD code, const char *fmt, ...)
{
    char head[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(head, sizeof head, fmt, ap);
    va_end(ap);
    char *m = win_msg(code);
    snprintf(g_errbuf, sizeof g_errbuf, "%s: %s", head, m);
    free(m);
}

const char *last_err(void)
{
    return g_errbuf[0] ? g_errbuf : "errore sconosciuto";
}

int cancelled(void)
{
    return InterlockedCompareExchange(&g_cancel, 0, 0) != 0;
}

int cancel_wait(DWORD ms)
{
    DWORD t0 = GetTickCount();
    while (GetTickCount() - t0 < ms) {
        if (cancelled())
            return 1;
        Sleep(100);
    }
    return cancelled();
}

/* --- varie --------------------------------------------------------------------------------------- */
char *now_iso(void)
{
    SYSTEMTIME st;
    TIME_ZONE_INFORMATION tz;
    GetLocalTime(&st);
    DWORD r = GetTimeZoneInformation(&tz);
    long bias = tz.Bias;                          /* minuti da aggiungere all'ora locale per avere UTC */
    if (r == TIME_ZONE_ID_DAYLIGHT)
        bias += tz.DaylightBias;
    else if (r == TIME_ZONE_ID_STANDARD)
        bias += tz.StandardBias;
    long off = -bias;
    char sign = off < 0 ? '-' : '+';
    if (off < 0)
        off = -off;
    return xasprintf("%04u-%02u-%02uT%02u:%02u:%02u%c%02ld:%02ld", st.wYear, st.wMonth, st.wDay, st.wHour,
                     st.wMinute, st.wSecond, sign, off / 60, off % 60);
}

char *human(double n)
{
    static const char *units[] = {"B", "KB", "MB", "GB", "TB"};
    for (int u = 0; u < 5; u++) {
        if ((n < 1024 && n > -1024) || u == 4)
            return u == 0 ? xasprintf("%.0f %s", n, units[u]) : xasprintf("%.2f %s", n, units[u]);
        n /= 1024;
    }
    return xasprintf("%.2f TB", n);
}

int random_bytes(void *buf, size_t n)
{
    return SystemFunction036(buf, (ULONG)n) ? 0 : -1;
}

void hex_encode(const uint8_t *in, size_t n, char *out)
{
    static const char d[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = d[in[i] >> 4];
        out[2 * i + 1] = d[in[i] & 15];
    }
    out[2 * n] = 0;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

int hex_decode(const char *hex, uint8_t *out, size_t n)
{
    if (!hex || strlen(hex) != 2 * n)
        return -1;
    for (size_t i = 0; i < n; i++) {
        int a = hexval(hex[2 * i]), b = hexval(hex[2 * i + 1]);
        if (a < 0 || b < 0)
            return -1;
        out[i] = (uint8_t)(a << 4 | b);
    }
    return 0;
}

char *new_uuid4(void)
{
    uint8_t b[16];
    char h[33];
    random_bytes(b, sizeof b);
    b[6] = (uint8_t)((b[6] & 0x0f) | 0x40);
    b[8] = (uint8_t)((b[8] & 0x3f) | 0x80);
    hex_encode(b, 16, h);
    return xasprintf("%.8s-%.4s-%.4s-%.4s-%.12s", h, h + 8, h + 12, h + 16, h + 20);
}

uint64_t mem_available(void)
{
    MEMORYSTATUSEX ms;
    ms.dwLength = sizeof ms;
    if (!GlobalMemoryStatusEx(&ms))
        return 0;
    uint64_t avail = ms.ullAvailPhys;
    if (ms.ullAvailVirtual < avail)                  /* programma a 32 bit: conta lo spazio di indirizzi */
        avail = ms.ullAvailVirtual;
    return avail;
}

char *exe_dir(void)
{
    wchar_t buf[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, buf, MAX_PATH * 2);
    if (!n || n >= MAX_PATH * 2)
        return xstrdup(".");
    wchar_t *slash = wcsrchr(buf, L'\\');
    if (slash)
        *slash = 0;
    return utf8(buf);
}

int path_is_local(const char *r)
{
    if (!r)
        return 0;
    while (*r == ' ')
        r++;
    if (((r[0] >= 'A' && r[0] <= 'Z') || (r[0] >= 'a' && r[0] <= 'z')) && r[1] == ':' && (r[2] == '\\' || r[2] == '/'))
        return 1;
    return r[0] == '\\' && r[1] == '\\';
}

int valid_name(const char *s)
{
    size_t n = strlen(s);
    if (n == 0 || n > 121)
        return 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        int alnum = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
        if (i == 0 ? !alnum : !(alnum || c == '.' || c == '_' || c == '-'))
            return 0;
    }
    return 1;
}

double mono_seconds(void)
{
    static LARGE_INTEGER freq;
    LARGE_INTEGER now;
    if (!freq.QuadPart)
        QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)freq.QuadPart;
}
