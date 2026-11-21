/* BKHD Backup per Windows - eventi del motore (stessi tipi del motore Linux: log, phase, progress,
 * chunk_done, start, done, error), mostrati dalla riga di comando o dall'interfaccia grafica.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BKHD_REPORT_H
#define BKHD_REPORT_H
#include "util.h"
#include "json.h"

typedef struct Reporter Reporter;
struct Reporter {
    void (*emit)(Reporter *r, JVal *ev);   /* l'evento resta di chi chiama */
    void *ctx;
    double last_progress;
    int64_t bad_count;
    CRITICAL_SECTION lock;
};

void rep_init(Reporter *r, void (*emit)(Reporter *, JVal *), void *ctx);
void rep_emit(Reporter *r, JVal *ev);              /* libera ev */
void rep_log(Reporter *r, const char *level, const char *fmt, ...);
void rep_phase(Reporter *r, const char *phase, int64_t chunk, int64_t chunks, const char *label);
void rep_progress(Reporter *r, int force, const char *phase, double frac, double speed, int64_t chunk,
                  int64_t chunks, double overall);

#endif
