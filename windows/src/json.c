/* BKHD Backup per Windows - JSON (vedi json.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "json.h"
#include "util.h"
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static JVal *jnew(JType t)
{
    JVal *v = xcalloc(1, sizeof *v);
    v->t = t;
    return v;
}

JVal *jnull(void) { return jnew(J_NULL); }

JVal *jbool(int b)
{
    JVal *v = jnew(J_BOOL);
    v->b = b ? 1 : 0;
    return v;
}

JVal *jint(int64_t i)
{
    JVal *v = jnew(J_INT);
    v->i = i;
    return v;
}

JVal *jstr(const char *s)
{
    JVal *v = jnew(J_STR);
    v->s = xstrdup(s ? s : "");
    return v;
}

JVal *jarr(void) { return jnew(J_ARR); }
JVal *jobj(void) { return jnew(J_OBJ); }

void jfree(JVal *v)
{
    if (!v)
        return;
    free(v->s);
    for (size_t k = 0; k < v->n; k++) {
        jfree(v->items[k]);
        if (v->keys)
            free(v->keys[k]);
    }
    free(v->items);
    free(v->keys);
    free(v);
}

JVal *jclone(const JVal *v)
{
    if (!v)
        return NULL;
    JVal *c = jnew(v->t);
    c->b = v->b;
    c->i = v->i;
    c->d = v->d;
    if (v->s)
        c->s = xstrdup(v->s);
    for (size_t k = 0; k < v->n; k++) {
        if (v->t == J_OBJ)
            jobj_set(c, v->keys[k], jclone(v->items[k]));
        else
            jarr_push(c, jclone(v->items[k]));
    }
    return c;
}

static void jgrow(JVal *a)
{
    if (a->n < a->cap)
        return;
    a->cap = a->cap ? a->cap * 2 : 8;
    a->items = xrealloc(a->items, a->cap * sizeof(JVal *));
    if (a->t == J_OBJ)
        a->keys = xrealloc(a->keys, a->cap * sizeof(char *));
}

void jarr_push(JVal *a, JVal *v)
{
    jgrow(a);
    a->items[a->n++] = v;
}

void jobj_set(JVal *o, const char *k, JVal *v)
{
    for (size_t i = 0; i < o->n; i++)
        if (!strcmp(o->keys[i], k)) {
            jfree(o->items[i]);
            o->items[i] = v;
            return;
        }
    jgrow(o);
    o->keys[o->n] = xstrdup(k);
    o->items[o->n++] = v;
}

JVal *jobj_get(const JVal *o, const char *k)
{
    if (!o || o->t != J_OBJ)
        return NULL;
    for (size_t i = 0; i < o->n; i++)
        if (!strcmp(o->keys[i], k))
            return o->items[i];
    return NULL;
}

void jobj_del(JVal *o, const char *k)
{
    if (!o || o->t != J_OBJ)
        return;
    for (size_t i = 0; i < o->n; i++)
        if (!strcmp(o->keys[i], k)) {
            jfree(o->items[i]);
            free(o->keys[i]);
            memmove(o->items + i, o->items + i + 1, (o->n - i - 1) * sizeof(JVal *));
            memmove(o->keys + i, o->keys + i + 1, (o->n - i - 1) * sizeof(char *));
            o->n--;
            return;
        }
}

const char *jget_str(const JVal *o, const char *k)
{
    JVal *v = jobj_get(o, k);
    return v && v->t == J_STR ? v->s : NULL;
}

int jget_int(const JVal *o, const char *k, int64_t *out)
{
    JVal *v = jobj_get(o, k);
    if (!v || v->t != J_INT)
        return -1;
    *out = v->i;
    return 0;
}

int jis_int(const JVal *v) { return v && v->t == J_INT; }

/* --- lettura ------------------------------------------------------------------------------------- */
typedef struct {
    const char *p, *end;
    int depth;
} Parser;

static void skip_ws(Parser *ps)
{
    while (ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r'))
        ps->p++;
}

static void put_utf8(StrBuf *b, uint32_t cp)
{
    char c[4];
    if (cp < 0x80) {
        c[0] = (char)cp;
        sb_putn(b, c, 1);
    } else if (cp < 0x800) {
        c[0] = (char)(0xC0 | cp >> 6);
        c[1] = (char)(0x80 | (cp & 0x3F));
        sb_putn(b, c, 2);
    } else if (cp < 0x10000) {
        c[0] = (char)(0xE0 | cp >> 12);
        c[1] = (char)(0x80 | (cp >> 6 & 0x3F));
        c[2] = (char)(0x80 | (cp & 0x3F));
        sb_putn(b, c, 3);
    } else {
        c[0] = (char)(0xF0 | cp >> 18);
        c[1] = (char)(0x80 | (cp >> 12 & 0x3F));
        c[2] = (char)(0x80 | (cp >> 6 & 0x3F));
        c[3] = (char)(0x80 | (cp & 0x3F));
        sb_putn(b, c, 4);
    }
}

static int hex4(Parser *ps, uint32_t *out)
{
    if (ps->end - ps->p < 4)
        return -1;
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) {
        char c = ps->p[i];
        v <<= 4;
        if (c >= '0' && c <= '9')
            v |= (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f')
            v |= (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F')
            v |= (uint32_t)(c - 'A' + 10);
        else
            return -1;
    }
    ps->p += 4;
    *out = v;
    return 0;
}

static char *parse_string(Parser *ps)
{
    StrBuf b;
    sb_init(&b);
    ps->p++;                                    /* " iniziale */
    while (ps->p < ps->end) {
        unsigned char c = (unsigned char)*ps->p++;
        if (c == '"')
            return sb_steal(&b);
        if (c < 0x20)
            break;
        if (c != '\\') {
            sb_putn(&b, (const char *)&c, 1);
            continue;
        }
        if (ps->p >= ps->end)
            break;
        char e = *ps->p++;
        uint32_t cp;
        switch (e) {
        case '"': sb_putc(&b, '"'); break;
        case '\\': sb_putc(&b, '\\'); break;
        case '/': sb_putc(&b, '/'); break;
        case 'b': sb_putc(&b, '\b'); break;
        case 'f': sb_putc(&b, '\f'); break;
        case 'n': sb_putc(&b, '\n'); break;
        case 'r': sb_putc(&b, '\r'); break;
        case 't': sb_putc(&b, '\t'); break;
        case 'u':
            if (hex4(ps, &cp))
                goto bad;
            if (cp >= 0xD800 && cp <= 0xDBFF && ps->end - ps->p >= 6 && ps->p[0] == '\\' && ps->p[1] == 'u') {
                Parser save = *ps;
                uint32_t lo;
                ps->p += 2;
                if (!hex4(ps, &lo) && lo >= 0xDC00 && lo <= 0xDFFF)
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                else
                    *ps = save;
            }
            put_utf8(&b, cp);
            break;
        default:
            goto bad;
        }
    }
bad:
    sb_free(&b);
    return NULL;
}

static JVal *parse_value(Parser *ps);

static JVal *parse_number(Parser *ps)
{
    const char *start = ps->p;
    int is_float = 0;
    if (ps->p < ps->end && *ps->p == '-')
        ps->p++;
    while (ps->p < ps->end) {
        char c = *ps->p;
        if (c >= '0' && c <= '9')
            ps->p++;
        else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
            is_float = 1;
            ps->p++;
        } else
            break;
    }
    size_t n = (size_t)(ps->p - start);
    if (n == 0 || n > 64)
        return NULL;
    char tmp[72];
    memcpy(tmp, start, n);
    tmp[n] = 0;
    char *endp;
    if (!is_float) {
        errno = 0;
        long long v = strtoll(tmp, &endp, 10);
        if (*endp || errno)
            return NULL;
        return jint((int64_t)v);
    }
    double d = strtod(tmp, &endp);
    if (*endp)
        return NULL;
    JVal *v = jnew(J_FLOAT);
    v->d = d;
    return v;
}

static JVal *parse_value(Parser *ps)
{
    skip_ws(ps);
    if (ps->p >= ps->end || ++ps->depth > 200)
        return NULL;
    JVal *v = NULL;
    char c = *ps->p;
    if (c == '{') {
        v = jobj();
        ps->p++;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == '}') {
            ps->p++;
            goto out;
        }
        for (;;) {
            skip_ws(ps);
            if (ps->p >= ps->end || *ps->p != '"')
                goto bad;
            char *k = parse_string(ps);
            if (!k)
                goto bad;
            skip_ws(ps);
            if (ps->p >= ps->end || *ps->p != ':') {
                free(k);
                goto bad;
            }
            ps->p++;
            JVal *item = parse_value(ps);
            if (!item) {
                free(k);
                goto bad;
            }
            jobj_set(v, k, item);
            free(k);
            skip_ws(ps);
            if (ps->p < ps->end && *ps->p == ',') {
                ps->p++;
                continue;
            }
            if (ps->p < ps->end && *ps->p == '}') {
                ps->p++;
                goto out;
            }
            goto bad;
        }
    } else if (c == '[') {
        v = jarr();
        ps->p++;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == ']') {
            ps->p++;
            goto out;
        }
        for (;;) {
            JVal *item = parse_value(ps);
            if (!item)
                goto bad;
            jarr_push(v, item);
            skip_ws(ps);
            if (ps->p < ps->end && *ps->p == ',') {
                ps->p++;
                continue;
            }
            if (ps->p < ps->end && *ps->p == ']') {
                ps->p++;
                goto out;
            }
            goto bad;
        }
    } else if (c == '"') {
        char *s = parse_string(ps);
        if (!s)
            return NULL;
        v = jnew(J_STR);
        v->s = s;
    } else if (ps->end - ps->p >= 4 && !strncmp(ps->p, "true", 4)) {
        ps->p += 4;
        v = jbool(1);
    } else if (ps->end - ps->p >= 5 && !strncmp(ps->p, "false", 5)) {
        ps->p += 5;
        v = jbool(0);
    } else if (ps->end - ps->p >= 4 && !strncmp(ps->p, "null", 4)) {
        ps->p += 4;
        v = jnull();
    } else {
        v = parse_number(ps);
    }
out:
    ps->depth--;
    return v;
bad:
    jfree(v);
    return NULL;
}

JVal *jparse(const char *text, size_t len)
{
    Parser ps = {text, text + len, 0};
    /* BOM UTF-8 eventuale */
    if (len >= 3 && (unsigned char)text[0] == 0xEF && (unsigned char)text[1] == 0xBB && (unsigned char)text[2] == 0xBF)
        ps.p += 3;
    JVal *v = parse_value(&ps);
    if (!v)
        return NULL;
    skip_ws(&ps);
    if (ps.p != ps.end) {
        jfree(v);
        return NULL;
    }
    return v;
}

/* --- scrittura (come il modulo json di Python) ------------------------------------------------- */

/* repr() di un float di Python: cifre minime che rileggono lo stesso valore */
static void py_float(StrBuf *b, double d)
{
    if (isnan(d)) {
        sb_puts(b, "NaN");
        return;
    }
    if (isinf(d)) {
        sb_puts(b, d < 0 ? "-Infinity" : "Infinity");
        return;
    }
    char tmp[64];
    int prec;
    for (prec = 1; prec <= 17; prec++) {
        snprintf(tmp, sizeof tmp, "%.*e", prec - 1, d);
        if (strtod(tmp, NULL) == d)
            break;
    }
    /* tmp = [-]D[.DDD]e[+-]XX */
    char digits[32];
    int nd = 0, neg = 0;
    const char *p = tmp;
    if (*p == '-') {
        neg = 1;
        p++;
    }
    for (; *p && *p != 'e'; p++)
        if (*p >= '0' && *p <= '9')
            digits[nd++] = *p;
    int exp = atoi(p + 1);
    while (nd > 1 && digits[nd - 1] == '0')
        nd--;
    digits[nd] = 0;
    if (neg)
        sb_putc(b, '-');
    if (exp >= -4 && exp < 16) {
        if (exp >= 0) {
            for (int k = 0; k <= exp; k++)
                sb_putc(b, k < nd ? digits[k] : '0');
            sb_putc(b, '.');
            if (nd > exp + 1)
                sb_putn(b, digits + exp + 1, (size_t)(nd - exp - 1));
            else
                sb_putc(b, '0');
        } else {
            sb_puts(b, "0.");
            for (int k = 0; k < -exp - 1; k++)
                sb_putc(b, '0');
            sb_puts(b, digits);
        }
    } else {
        sb_putc(b, digits[0]);
        if (nd > 1) {
            sb_putc(b, '.');
            sb_puts(b, digits + 1);
        }
        sb_printf(b, "e%c%02d", exp < 0 ? '-' : '+', exp < 0 ? -exp : exp);
    }
}

/* legge un code point UTF-8; restituisce il numero di byte (1 se non valido) */
static int utf8_cp(const unsigned char *s, uint32_t *cp)
{
    if (s[0] < 0x80) {
        *cp = s[0];
        return 1;
    }
    if ((s[0] & 0xE0) == 0xC0 && (s[1] & 0xC0) == 0x80) {
        *cp = (uint32_t)(s[0] & 0x1F) << 6 | (s[1] & 0x3F);
        return 2;
    }
    if ((s[0] & 0xF0) == 0xE0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80) {
        *cp = (uint32_t)(s[0] & 0x0F) << 12 | (uint32_t)(s[1] & 0x3F) << 6 | (s[2] & 0x3F);
        return 3;
    }
    if ((s[0] & 0xF8) == 0xF0 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80 && (s[3] & 0xC0) == 0x80) {
        *cp = (uint32_t)(s[0] & 0x07) << 18 | (uint32_t)(s[1] & 0x3F) << 12 | (uint32_t)(s[2] & 0x3F) << 6 |
              (s[3] & 0x3F);
        return 4;
    }
    *cp = 0xFFFD;
    return 1;
}

static void put_str(StrBuf *b, const char *str, int ascii)
{
    const unsigned char *s = (const unsigned char *)str;
    sb_putc(b, '"');
    while (*s) {
        unsigned char c = *s;
        const char *esc = NULL;
        switch (c) {
        case '"': esc = "\\\""; break;
        case '\\': esc = "\\\\"; break;
        case '\n': esc = "\\n"; break;
        case '\r': esc = "\\r"; break;
        case '\t': esc = "\\t"; break;
        case '\b': esc = "\\b"; break;
        case '\f': esc = "\\f"; break;
        }
        if (esc) {
            sb_puts(b, esc);
            s++;
        } else if (c < 0x20 || (ascii && c == 0x7F)) {
            sb_printf(b, "\\u%04x", c);
            s++;
        } else if (c < 0x80 || !ascii) {
            sb_putn(b, (const char *)s, 1);
            s++;
        } else {
            uint32_t cp;
            s += utf8_cp(s, &cp);
            if (cp >= 0x10000) {
                cp -= 0x10000;
                sb_printf(b, "\\u%04x\\u%04x", 0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF));
            } else {
                sb_printf(b, "\\u%04x", cp);
            }
        }
    }
    sb_putc(b, '"');
}

static void indent(StrBuf *b, int level)
{
    sb_putc(b, '\n');
    for (int k = 0; k < level; k++)
        sb_putc(b, ' ');
}

static void dump(StrBuf *b, const JVal *v, int mode, int level)
{
    int pretty = mode == J_PRETTY;
    int ascii = mode == J_COMPACT_ASCII;
    switch (v->t) {
    case J_NULL: sb_puts(b, "null"); break;
    case J_BOOL: sb_puts(b, v->b ? "true" : "false"); break;
    case J_INT: sb_printf(b, "%lld", (long long)v->i); break;
    case J_FLOAT: py_float(b, v->d); break;
    case J_STR: put_str(b, v->s, ascii); break;
    case J_ARR:
        if (!v->n) {
            sb_puts(b, "[]");
            break;
        }
        sb_putc(b, '[');
        for (size_t k = 0; k < v->n; k++) {
            if (k)
                sb_putc(b, ',');
            if (pretty)
                indent(b, level + 1);
            dump(b, v->items[k], mode, level + 1);
        }
        if (pretty)
            indent(b, level);
        sb_putc(b, ']');
        break;
    case J_OBJ: {
        if (!v->n) {
            sb_puts(b, "{}");
            break;
        }
        size_t *order = xmalloc(v->n * sizeof(size_t));
        for (size_t k = 0; k < v->n; k++)
            order[k] = k;
        if (mode == J_CANONICAL)            /* ordinamento per inserzione: niente stato globale */
            for (size_t k = 1; k < v->n; k++)
                for (size_t j = k; j > 0 && strcmp(v->keys[order[j - 1]], v->keys[order[j]]) > 0; j--) {
                    size_t t = order[j];
                    order[j] = order[j - 1];
                    order[j - 1] = t;
                }
        sb_putc(b, '{');
        for (size_t k = 0; k < v->n; k++) {
            size_t i = order[k];
            if (k)
                sb_putc(b, ',');
            if (pretty)
                indent(b, level + 1);
            put_str(b, v->keys[i], ascii);
            sb_puts(b, pretty ? ": " : ":");
            dump(b, v->items[i], mode, level + 1);
        }
        free(order);
        if (pretty)
            indent(b, level);
        sb_putc(b, '}');
        break;
    }
    }
}

char *jdump(const JVal *v, int mode)
{
    StrBuf b;
    sb_init(&b);
    dump(&b, v, mode, 0);
    return sb_steal(&b);
}
