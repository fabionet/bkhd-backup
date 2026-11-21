/* BKHD Backup per Windows - eventi del motore (vedi report.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "report.h"
#include <stdlib.h>

void rep_init(Reporter *r, void (*emit)(Reporter *, JVal *), void *ctx)
{
    r->emit = emit;
    r->ctx = ctx;
    r->last_progress = 0;
    r->bad_count = 0;
    InitializeCriticalSection(&r->lock);
}

void rep_emit(Reporter *r, JVal *ev)
{
    if (r && r->emit) {
        EnterCriticalSection(&r->lock);
        r->emit(r, ev);
        LeaveCriticalSection(&r->lock);
    }
    jfree(ev);
}

void rep_log(Reporter *r, const char *level, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    char *msg = xvasprintf(fmt, ap);
    va_end(ap);
    JVal *ev = jobj();
    jobj_set(ev, "type", jstr("log"));
    jobj_set(ev, "level", jstr(level ? level : "info"));
    jobj_set(ev, "msg", jstr(msg));
    free(msg);
    rep_emit(r, ev);
}

void rep_phase(Reporter *r, const char *phase, int64_t chunk, int64_t chunks, const char *label)
{
    JVal *ev = jobj();
    jobj_set(ev, "type", jstr("phase"));
    jobj_set(ev, "phase", jstr(phase));
    jobj_set(ev, "chunk", jint(chunk));
    jobj_set(ev, "chunks", jint(chunks));
    jobj_set(ev, "label", jstr(label));
    rep_emit(r, ev);
}

static JVal *jdouble(double d)
{
    JVal *v = jint(0);
    v->t = J_FLOAT;
    v->d = d;
    return v;
}

void rep_progress(Reporter *r, int force, const char *phase, double frac, double speed, int64_t chunk,
                  int64_t chunks, double overall)
{
    if (!r)
        return;
    double t = mono_seconds();
    EnterCriticalSection(&r->lock);
    int skip = !force && t - r->last_progress < 0.4;
    if (!skip)
        r->last_progress = t;
    LeaveCriticalSection(&r->lock);
    if (skip)
        return;
    JVal *ev = jobj();
    jobj_set(ev, "type", jstr("progress"));
    jobj_set(ev, "phase", jstr(phase));
    jobj_set(ev, "frac", jdouble(frac));
    jobj_set(ev, "speed", jdouble(speed));
    jobj_set(ev, "chunk", jint(chunk));
    jobj_set(ev, "chunks", jint(chunks));
    jobj_set(ev, "overall", jdouble(overall > 1.0 ? 1.0 : overall));
    rep_emit(r, ev);
}
