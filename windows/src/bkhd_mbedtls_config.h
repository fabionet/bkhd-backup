/* Configurazione minima di Mbed TLS per BKHD Backup: solo AES, GCM e SHA-256.
 * Niente istruzioni AES-NI, PadLock o assembly: il programma a 32 bit deve girare su CPU con solo SSE. */
#ifndef BKHD_MBEDTLS_CONFIG_H
#define BKHD_MBEDTLS_CONFIG_H

#define MBEDTLS_AES_C
#define MBEDTLS_GCM_C
#define MBEDTLS_CIPHER_C
#define MBEDTLS_SHA256_C
#define MBEDTLS_AES_ROM_TABLES
#define MBEDTLS_PLATFORM_C

#endif
