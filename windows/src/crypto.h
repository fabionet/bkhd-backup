/* BKHD Backup per Windows - SHA-256, HMAC, scrypt e AES-256-GCM (compatibili con il motore Linux).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_CRYPTO_H
#define BKHD_CRYPTO_H
#include <stdint.h>
#include <stddef.h>
#include "json.h"
#include "mbedtls/gcm.h"
#include "mbedtls/sha256.h"

/* hash di flusso: SHA-256 semplice oppure HMAC-SHA256 (backup protetti) */
typedef struct {
    mbedtls_sha256_context inner;
    uint8_t opad[64];
    int hmac;
} Hash;

void hash_init(Hash *h, const uint8_t *hmac_key);      /* chiave NULL = SHA-256; altrimenti 32 byte */
void hash_update(Hash *h, const void *data, size_t n);
void hash_final_hex(Hash *h, char out[65]);
void hmac_sha256(const uint8_t *key, size_t klen, const void *data, size_t n, uint8_t out[32]);
int scrypt_kdf(const uint8_t *pw, size_t pwlen, const uint8_t *salt, size_t saltlen, uint64_t N, uint32_t r,
               uint32_t p, uint8_t *out, size_t outlen);

#define MIN_PASSWORD 8

typedef struct {
    uint8_t enc_key[32], mac_key[32];
    JVal *meta;                       /* {"alg", "kdf", "salt", "n", "r", "p", "check"} */
} Crypto;

int crypto_create(Crypto *c, const char *password);
int crypto_open(Crypto *c, const char *password, const JVal *meta);
void crypto_free(Crypto *c);
void crypto_sign(const Crypto *c, const JVal *obj, char out[65]);
int crypto_verify(const Crypto *c, const JVal *obj, const char *what);
char *crypto_aad(const char *backup_id, int64_t index, int64_t offset, int64_t length, const char *compression,
                 int sparse);

typedef struct {
    mbedtls_gcm_context g;
} Gcm;

int gcm_start(Gcm *g, const uint8_t key[32], const uint8_t nonce[12], const char *aad, int encrypt);
/* out deve avere spazio per n + 16 byte; restituisce i byte scritti */
size_t gcm_update(Gcm *g, const uint8_t *in, size_t n, uint8_t *out);
size_t gcm_finish(Gcm *g, uint8_t *out, uint8_t tag[16]);
void gcm_free(Gcm *g);
int ct_equal(const void *a, const void *b, size_t n);

#endif
