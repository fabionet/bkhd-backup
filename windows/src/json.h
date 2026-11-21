/* BKHD Backup per Windows - JSON con interi a 64 bit e scrittura identica al modulo json di Python
 * (necessaria perche' le firme HMAC dei backup protetti coincidano con quelle del motore Linux).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_JSON_H
#define BKHD_JSON_H
#include <stdint.h>
#include <stddef.h>

typedef enum { J_NULL, J_BOOL, J_INT, J_FLOAT, J_STR, J_ARR, J_OBJ } JType;

typedef struct JVal JVal;
struct JVal {
    JType t;
    int b;
    int64_t i;
    double d;
    char *s;
    size_t n, cap;
    JVal **items;          /* elementi dell'array o valori dell'oggetto */
    char **keys;           /* chiavi dell'oggetto (nell'ordine di inserimento) */
};

/* modi di scrittura */
enum {
    J_PRETTY,              /* json.dumps(x, indent=1, ensure_ascii=False)                  - file sul cloud */
    J_COMPACT_ASCII,       /* json.dumps(x, separators=(",", ":"))                         - intestazione .bkhd */
    J_CANONICAL            /* json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False) - firme */
};

JVal *jnull(void);
JVal *jbool(int b);
JVal *jint(int64_t i);
JVal *jstr(const char *s);
JVal *jarr(void);
JVal *jobj(void);
void jfree(JVal *v);
JVal *jclone(const JVal *v);

void jarr_push(JVal *a, JVal *v);
void jobj_set(JVal *o, const char *k, JVal *v);     /* sostituisce il valore se la chiave esiste */
JVal *jobj_get(const JVal *o, const char *k);
void jobj_del(JVal *o, const char *k);

/* lettura tipizzata: restituiscono 0 se il campo esiste ed e' del tipo giusto */
const char *jget_str(const JVal *o, const char *k);         /* NULL se assente o non stringa */
int jget_int(const JVal *o, const char *k, int64_t *out);
int jis_int(const JVal *v);

JVal *jparse(const char *text, size_t len);                 /* NULL se non valido */
char *jdump(const JVal *v, int mode);                       /* da liberare con free */

#endif
