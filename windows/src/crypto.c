/* BKHD Backup per Windows - crittografia (vedi crypto.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "crypto.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>

/* --- SHA-256 / HMAC ------------------------------------------------------------------------------ */
void hash_init(Hash *h, const uint8_t *key)
{
    mbedtls_sha256_init(&h->inner);
    mbedtls_sha256_starts(&h->inner, 0);
    h->hmac = key != NULL;
    if (key) {
        uint8_t ipad[64];
        for (int i = 0; i < 64; i++) {
            uint8_t k = i < 32 ? key[i] : 0;
            ipad[i] = k ^ 0x36;
            h->opad[i] = k ^ 0x5c;
        }
        mbedtls_sha256_update(&h->inner, ipad, 64);
    }
}

void hash_update(Hash *h, const void *data, size_t n)
{
    const uint8_t *p = data;
    while (n) {                              /* a blocchi: mai piu' di 1 GB per chiamata */
        size_t k = n > (1u << 30) ? (1u << 30) : n;
        mbedtls_sha256_update(&h->inner, p, k);
        p += k;
        n -= k;
    }
}

void hash_final_hex(Hash *h, char out[65])
{
    uint8_t d[32];
    mbedtls_sha256_finish(&h->inner, d);
    mbedtls_sha256_free(&h->inner);
    if (h->hmac) {
        mbedtls_sha256_context o;
        mbedtls_sha256_init(&o);
        mbedtls_sha256_starts(&o, 0);
        mbedtls_sha256_update(&o, h->opad, 64);
        mbedtls_sha256_update(&o, d, 32);
        mbedtls_sha256_finish(&o, d);
        mbedtls_sha256_free(&o);
    }
    hex_encode(d, 32, out);
}

void hmac_sha256(const uint8_t *key, size_t klen, const void *data, size_t n, uint8_t out[32])
{
    uint8_t k[64] = {0}, ipad[64], opad[64], inner[32];
    if (klen > 64)
        mbedtls_sha256(key, klen, k, 0);
    else
        memcpy(k, key, klen);
    for (int i = 0; i < 64; i++) {
        ipad[i] = k[i] ^ 0x36;
        opad[i] = k[i] ^ 0x5c;
    }
    mbedtls_sha256_context c;
    mbedtls_sha256_init(&c);
    mbedtls_sha256_starts(&c, 0);
    mbedtls_sha256_update(&c, ipad, 64);
    mbedtls_sha256_update(&c, data, n);
    mbedtls_sha256_finish(&c, inner);
    mbedtls_sha256_starts(&c, 0);
    mbedtls_sha256_update(&c, opad, 64);
    mbedtls_sha256_update(&c, inner, 32);
    mbedtls_sha256_finish(&c, out);
    mbedtls_sha256_free(&c);
}

int ct_equal(const void *a, const void *b, size_t n)
{
    const uint8_t *x = a, *y = b;
    uint8_t d = 0;
    for (size_t i = 0; i < n; i++)
        d |= x[i] ^ y[i];
    return d == 0;
}

/* --- scrypt (RFC 7914) --------------------------------------------------------------------------- */
static void pbkdf2_sha256_1(const uint8_t *pw, size_t pwlen, const uint8_t *salt, size_t saltlen, uint8_t *out,
                            size_t outlen)
{
    uint8_t *buf = xmalloc(saltlen + 4), u[32];
    memcpy(buf, salt, saltlen);
    for (uint32_t i = 1; outlen; i++) {      /* una sola iterazione: T_i = HMAC(P, S || INT(i)) */
        buf[saltlen] = (uint8_t)(i >> 24);
        buf[saltlen + 1] = (uint8_t)(i >> 16);
        buf[saltlen + 2] = (uint8_t)(i >> 8);
        buf[saltlen + 3] = (uint8_t)i;
        hmac_sha256(pw, pwlen, buf, saltlen + 4, u);
        size_t k = outlen < 32 ? outlen : 32;
        memcpy(out, u, k);
        out += k;
        outlen -= k;
    }
    free(buf);
}

#define ROTL(a, b) (((a) << (b)) | ((a) >> (32 - (b))))

static void salsa20_8(uint32_t B[16])
{
    uint32_t x[16];
    memcpy(x, B, 64);
    for (int i = 0; i < 8; i += 2) {
        x[4] ^= ROTL(x[0] + x[12], 7);   x[8] ^= ROTL(x[4] + x[0], 9);
        x[12] ^= ROTL(x[8] + x[4], 13);  x[0] ^= ROTL(x[12] + x[8], 18);
        x[9] ^= ROTL(x[5] + x[1], 7);    x[13] ^= ROTL(x[9] + x[5], 9);
        x[1] ^= ROTL(x[13] + x[9], 13);  x[5] ^= ROTL(x[1] + x[13], 18);
        x[14] ^= ROTL(x[10] + x[6], 7);  x[2] ^= ROTL(x[14] + x[10], 9);
        x[6] ^= ROTL(x[2] + x[14], 13);  x[10] ^= ROTL(x[6] + x[2], 18);
        x[3] ^= ROTL(x[15] + x[11], 7);  x[7] ^= ROTL(x[3] + x[15], 9);
        x[11] ^= ROTL(x[7] + x[3], 13);  x[15] ^= ROTL(x[11] + x[7], 18);
        x[1] ^= ROTL(x[0] + x[3], 7);    x[2] ^= ROTL(x[1] + x[0], 9);
        x[3] ^= ROTL(x[2] + x[1], 13);   x[0] ^= ROTL(x[3] + x[2], 18);
        x[6] ^= ROTL(x[5] + x[4], 7);    x[7] ^= ROTL(x[6] + x[5], 9);
        x[4] ^= ROTL(x[7] + x[6], 13);   x[5] ^= ROTL(x[4] + x[7], 18);
        x[11] ^= ROTL(x[10] + x[9], 7);  x[8] ^= ROTL(x[11] + x[10], 9);
        x[9] ^= ROTL(x[8] + x[11], 13);  x[10] ^= ROTL(x[9] + x[8], 18);
        x[12] ^= ROTL(x[15] + x[14], 7); x[13] ^= ROTL(x[12] + x[15], 9);
        x[14] ^= ROTL(x[13] + x[12], 13); x[15] ^= ROTL(x[14] + x[13], 18);
    }
    for (int i = 0; i < 16; i++)
        B[i] += x[i];
}

/* BlockMix: B (2r blocchi da 64 byte) -> Y */
static void blockmix(const uint32_t *B, uint32_t *Y, uint32_t r)
{
    uint32_t X[16];
    memcpy(X, B + (2 * r - 1) * 16, 64);
    for (uint32_t i = 0; i < 2 * r; i++) {
        for (int k = 0; k < 16; k++)
            X[k] ^= B[i * 16 + k];
        salsa20_8(X);
        /* blocchi pari nella prima meta', dispari nella seconda */
        memcpy(Y + ((i & 1) ? (r + i / 2) : (i / 2)) * 16, X, 64);
    }
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void st32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

int scrypt_kdf(const uint8_t *pw, size_t pwlen, const uint8_t *salt, size_t saltlen, uint64_t N, uint32_t r,
               uint32_t p, uint8_t *out, size_t outlen)
{
    size_t words = 32 * (size_t)r;                       /* parole da 32 bit in un blocco da 128*r byte */
    uint64_t vbytes = (uint64_t)128 * r * N;
    if (vbytes > (uint64_t)SIZE_MAX / 2) {
        set_err("memoria insufficiente per la chiave del backup");
        return FAIL;
    }
    uint8_t *B = xmalloc(128 * (size_t)r * p);
    uint32_t *X = xmalloc(words * 4), *Y = xmalloc(words * 4);
    uint32_t *V = malloc((size_t)vbytes);
    if (!V) {
        free(B);
        free(X);
        free(Y);
        set_err("memoria insufficiente per calcolare la chiave del backup (servono %llu MB)",
                (unsigned long long)(vbytes / MIB));
        return FAIL;
    }
    pbkdf2_sha256_1(pw, pwlen, salt, saltlen, B, 128 * (size_t)r * p);
    for (uint32_t blk = 0; blk < p; blk++) {
        uint8_t *Bi = B + 128 * (size_t)r * blk;
        for (size_t k = 0; k < words; k++)
            X[k] = le32(Bi + 4 * k);
        for (uint64_t i = 0; i < N; i++) {
            memcpy(V + words * i, X, words * 4);
            blockmix(X, Y, r);
            memcpy(X, Y, words * 4);
        }
        for (uint64_t i = 0; i < N; i++) {
            uint64_t j = X[(2 * r - 1) * 16] & (N - 1);
            for (size_t k = 0; k < words; k++)
                X[k] ^= V[words * j + k];
            blockmix(X, Y, r);
            memcpy(X, Y, words * 4);
        }
        for (size_t k = 0; k < words; k++)
            st32(Bi + 4 * k, X[k]);
    }
    pbkdf2_sha256_1(pw, pwlen, B, 128 * (size_t)r * p, out, outlen);
    SecureZeroMemory(V, (size_t)vbytes);
    SecureZeroMemory(B, 128 * (size_t)r * p);
    free(V);
    free(B);
    free(X);
    free(Y);
    return OK;
}

/* --- chiavi del backup --------------------------------------------------------------------------- */
static const char PW_CHECK[] = "BKHD password check v1";

static void check_value(const Crypto *c, char out[65])
{
    uint8_t d[32];
    hmac_sha256(c->mac_key, 32, PW_CHECK, sizeof PW_CHECK - 1, d);
    hex_encode(d, 32, out);
}

static int derive(Crypto *c, const char *password, const JVal *meta)
{
    const char *alg = jget_str(meta, "alg"), *kdf = jget_str(meta, "kdf"), *salt = jget_str(meta, "salt");
    if (!meta || meta->t != J_OBJ || !alg || strcmp(alg, "aes-256-gcm") || !kdf || strcmp(kdf, "scrypt")) {
        set_err("cifratura del backup non supportata da questa versione");
        return FAIL;
    }
    int64_t n, r, p;
    uint8_t saltb[16];
    int salt_ok = salt && strlen(salt) == 32 && !hex_decode(salt, saltb, 16);
    for (const char *q = salt; salt_ok && *q; q++)
        if ((*q < '0' || *q > '9') && (*q < 'a' || *q > 'f'))
            salt_ok = 0;
    if (jget_int(meta, "n", &n) || jget_int(meta, "r", &r) || jget_int(meta, "p", &p) || n < (1 << 14) ||
        n > (1 << 20) || (n & (n - 1)) || r != 8 || p < 1 || p > 4 || !salt_ok) {
        set_err("parametri di cifratura del backup non validi");
        return FAIL;
    }
    uint8_t key[64];
    if (scrypt_kdf((const uint8_t *)password, strlen(password), saltb, 16, (uint64_t)n, (uint32_t)r, (uint32_t)p,
                   key, 64))
        return FAIL;
    memcpy(c->enc_key, key, 32);
    memcpy(c->mac_key, key + 32, 32);
    SecureZeroMemory(key, sizeof key);
    return OK;
}

int crypto_create(Crypto *c, const char *password)
{
    memset(c, 0, sizeof *c);
    uint8_t salt[16];
    char hex[33], chk[65];
    if (random_bytes(salt, sizeof salt)) {
        set_err("generatore di numeri casuali non disponibile");
        return FAIL;
    }
    hex_encode(salt, 16, hex);
    JVal *m = jobj();
    jobj_set(m, "alg", jstr("aes-256-gcm"));
    jobj_set(m, "kdf", jstr("scrypt"));
    jobj_set(m, "salt", jstr(hex));
    jobj_set(m, "n", jint(1 << 15));
    jobj_set(m, "r", jint(8));
    jobj_set(m, "p", jint(1));
    if (derive(c, password, m)) {
        jfree(m);
        return FAIL;
    }
    check_value(c, chk);
    jobj_set(m, "check", jstr(chk));
    c->meta = m;
    return OK;
}

int crypto_open(Crypto *c, const char *password, const JVal *meta)
{
    memset(c, 0, sizeof *c);
    if (!password || !*password) {
        set_err("questo backup e' protetto da password: inseriscila per continuare");
        return FAIL;
    }
    if (derive(c, password, meta))
        return FAIL;
    char chk[65];
    check_value(c, chk);
    const char *want = jget_str(meta, "check");
    if (!want || strlen(want) != 64 || !ct_equal(chk, want, 64)) {
        crypto_free(c);
        set_err("password errata");
        return FAIL;
    }
    c->meta = jclone(meta);
    return OK;
}

void crypto_free(Crypto *c)
{
    SecureZeroMemory(c->enc_key, 32);
    SecureZeroMemory(c->mac_key, 32);
    jfree(c->meta);
    c->meta = NULL;
}

void crypto_sign(const Crypto *c, const JVal *obj, char out[65])
{
    /* firma: HMAC(mac, "bkhd-meta:" + JSON canonico senza "mac" e senza chiavi che iniziano con "_") */
    JVal *body = jobj();
    for (size_t k = 0; k < obj->n; k++)
        if (strcmp(obj->keys[k], "mac") && obj->keys[k][0] != '_')
            jobj_set(body, obj->keys[k], jclone(obj->items[k]));
    char *data = jdump(body, J_CANONICAL);
    jfree(body);
    size_t n = strlen(data);
    uint8_t *msg = xmalloc(n + 10), d[32];
    memcpy(msg, "bkhd-meta:", 10);
    memcpy(msg + 10, data, n);
    hmac_sha256(c->mac_key, 32, msg, n + 10, d);
    free(msg);
    free(data);
    hex_encode(d, 32, out);
}

int crypto_verify(const Crypto *c, const JVal *obj, const char *what)
{
    char sig[65];
    crypto_sign(c, obj, sig);
    const char *mac = jget_str(obj, "mac");
    if (!mac || strlen(mac) != 64 || !ct_equal(mac, sig, 64)) {
        set_err("%s del backup alterato o danneggiato (firma non valida)", what);
        return FAIL;
    }
    return OK;
}

char *crypto_aad(const char *backup_id, int64_t index, int64_t offset, int64_t length, const char *compression,
                 int sparse)
{
    return xasprintf("bkhd2:%s:%lld:%lld:%lld:%s:%d", backup_id, (long long)index, (long long)offset,
                     (long long)length, compression, sparse ? 1 : 0);
}

/* --- AES-256-GCM a flusso ------------------------------------------------------------------------ */
int gcm_start(Gcm *g, const uint8_t key[32], const uint8_t nonce[12], const char *aad, int encrypt)
{
    mbedtls_gcm_init(&g->g);
    if (mbedtls_gcm_setkey(&g->g, MBEDTLS_CIPHER_ID_AES, key, 256) ||
        mbedtls_gcm_starts(&g->g, encrypt ? MBEDTLS_GCM_ENCRYPT : MBEDTLS_GCM_DECRYPT, nonce, 12) ||
        mbedtls_gcm_update_ad(&g->g, (const unsigned char *)aad, strlen(aad))) {
        mbedtls_gcm_free(&g->g);
        set_err("inizializzazione della cifratura non riuscita");
        return FAIL;
    }
    return OK;
}

size_t gcm_update(Gcm *g, const uint8_t *in, size_t n, uint8_t *out)
{
    size_t done = 0, total = 0;
    while (done < n) {
        size_t k = n - done > (1u << 24) ? (1u << 24) : n - done, olen = 0;
        mbedtls_gcm_update(&g->g, in + done, k, out + total, k + 16, &olen);
        done += k;
        total += olen;
    }
    return total;
}

size_t gcm_finish(Gcm *g, uint8_t *out, uint8_t tag[16])
{
    size_t olen = 0;
    mbedtls_gcm_finish(&g->g, out, 16, &olen, tag, 16);
    return olen;
}

void gcm_free(Gcm *g)
{
    mbedtls_gcm_free(&g->g);
}
