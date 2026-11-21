/* BKHD Backup per Windows - interfaccia grafica (bkhd-backup.exe): schede Backup e Ripristino, avanzamento,
 * registro e annullamento. Il motore gira in un thread separato e invia gli eventi alla finestra.
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "disk.h"
#include "engine.h"
#include "rclone.h"
#include "report.h"
#include "resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WM_APP_EVENT (WM_APP + 1)     /* lParam: JVal* evento */
#define WM_APP_DONE (WM_APP + 2)      /* wParam: esito, lParam: char* messaggio */
#define WM_APP_LIST (WM_APP + 3)      /* wParam: 0 = ripristino, 1 = backup interrotti; lParam: JVal* */
#define WM_APP_REMOTES (WM_APP + 4)   /* lParam: StrList* */

static HINSTANCE g_inst;
static HWND g_main, g_tab, g_page[2];
static HFONT g_title_font;
static volatile LONG g_busy;
static HANDLE g_thread;
static int g_cloud_ok;                 /* rclone disponibile (non su Windows XP) */
static char *g_cloud_why;
static DiskInfo *g_disks;
static int g_ndisks;
static StrList g_sources;              /* percorso per ogni voce della sorgente */
static StrList g_targets;              /* percorso per ogni voce della destinazione */
static JVal *g_found;                  /* backup trovati (scheda Ripristino) */
static char *g_auto;                   /* prova automatica: file dei risultati (variabile BKHD_GUI_AUTOTEST) */
static int g_auto_step;
static char *g_found_base;             /* percorso usato per la ricerca */

typedef struct {
    const char *label;
    int64_t kib;
} Preset;

static const Preset PRESETS[] = {
    {"1,2 MB (floppy 5¼\")", 1200},
    {"1,44 MB (floppy 3½\")", 1440},
    {"2,88 MB (floppy 3½\" ED)", 2880},
    {"50 MB", 50 * 1024},
    {"100 MB", 100 * 1024},
    {"650 MB (CD)", 650 * 1024},
    {"700 MB (CD)", 700 * 1024},
    {"1 GB", 1024 * 1024},
    {"2 GB", 2 * 1024 * 1024},
    {"4 GB", 4 * 1024 * 1024},
    {"4,7 GB (DVD)", 4589843},
    {"8 GB", 8 * 1024 * 1024},
};
#define NPRESETS (int)(sizeof PRESETS / sizeof PRESETS[0])

/* --- piccoli aiuti ------------------------------------------------------------------------------- */
static char *get_text(HWND dlg, int id)
{
    HWND h = GetDlgItem(dlg, id);
    int n = GetWindowTextLengthW(h);
    wchar_t *w = xmalloc(((size_t)n + 1) * sizeof(wchar_t));
    GetWindowTextW(h, w, n + 1);
    char *s = utf8(w);
    free(w);
    /* spazi iniziali e finali */
    char *p = s;
    while (*p == ' ')
        p++;
    memmove(s, p, strlen(p) + 1);
    size_t l = strlen(s);
    while (l && s[l - 1] == ' ')
        s[--l] = 0;
    return s;
}

static void set_text(HWND dlg, int id, const char *text)
{
    wchar_t *w = wide(text);
    SetDlgItemTextW(dlg, id, w);
    free(w);
}

static char *env(const char *name)
{
    wchar_t *wn = wide(name), buf[2048];
    DWORD n = GetEnvironmentVariableW(wn, buf, 2048);
    free(wn);
    return n && n < 2048 ? utf8(buf) : NULL;
}

static void auto_log(const char *text)
{
    wchar_t *w = wide(g_auto);
    HANDLE h = CreateFileW(w, FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
    free(w);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD k;
        WriteFile(h, text, (DWORD)strlen(text), &k, NULL);
        WriteFile(h, "\n", 1, &k, NULL);
        CloseHandle(h);
    }
}

static int msg_box(HWND owner, const char *text, UINT flags)
{
    if (g_auto) {                               /* prova automatica: nessuna finestra modale */
        char *t = xasprintf("MESSAGGIO: %s", text);
        auto_log(t);
        free(t);
        return IDYES;
    }
    wchar_t *w = wide(text);
    int r = MessageBoxW(owner, w, L"BKHD Backup", flags);
    free(w);
    return r;
}

static void combo_add(HWND dlg, int id, const char *text)
{
    wchar_t *w = wide(text);
    SendDlgItemMessageW(dlg, id, CB_ADDSTRING, 0, (LPARAM)w);
    free(w);
}

static int checked(HWND dlg, int id)
{
    return IsDlgButtonChecked(dlg, id) == BST_CHECKED;
}

static void log_line(const char *text)
{
    HWND h = GetDlgItem(g_main, IDC_LOG);
    char *t = xasprintf("%s\r\n", text);
    wchar_t *w = wide(t);
    int n = GetWindowTextLengthW(h);
    if (n > 400000) {                       /* registro lungo: si tiene la parte recente */
        SendMessageW(h, EM_SETSEL, 0, 100000);
        SendMessageW(h, EM_REPLACESEL, FALSE, (LPARAM)L"");
        n = GetWindowTextLengthW(h);
    }
    SendMessageW(h, EM_SETSEL, (WPARAM)n, (LPARAM)n);
    SendMessageW(h, EM_REPLACESEL, FALSE, (LPARAM)w);
    free(w);
    free(t);
}

static void status(const char *text)
{
    set_text(g_main, IDC_STATUS, text);
}

/* --- elenchi di dischi e remote ------------------------------------------------------------------ */
static void load_disks(void)
{
    HWND b = g_page[0], r = g_page[1];
    if (g_disks)
        disk_list_free(g_disks, g_ndisks);
    disk_list(&g_disks, &g_ndisks);
    char *keep = NULL;
    int sel = (int)SendDlgItemMessageW(b, IDC_B_SOURCE, CB_GETCURSEL, 0, 0);
    if (sel >= 0 && (size_t)sel < g_sources.n)
        keep = xstrdup(g_sources.v[sel]);
    SendDlgItemMessageW(b, IDC_B_SOURCE, CB_RESETCONTENT, 0, 0);
    SendDlgItemMessageW(r, IDC_R_TARGET, CB_RESETCONTENT, 0, 0);
    sl_free(&g_sources);
    sl_free(&g_targets);
    int first_data = -1, keep_idx = -1;
    for (int i = 0; i < g_ndisks; i++) {
        char *l = disk_label(&g_disks[i]);
        combo_add(b, IDC_B_SOURCE, l);
        sl_push(&g_sources, g_disks[i].path);
        if (keep && !strcmp(keep, g_disks[i].path))
            keep_idx = i;
        if (first_data < 0 && !g_disks[i].is_system)
            first_data = i;
        if (!g_disks[i].is_system) {            /* il disco di Windows non puo' essere sovrascritto da qui */
            combo_add(r, IDC_R_TARGET, l);
            sl_push(&g_targets, g_disks[i].path);
        }
        free(l);
    }
    if (keep && keep_idx < 0 && !path_is_device(keep)) {     /* file immagine scelto prima */
        char *l = xasprintf("File immagine: %s", keep);
        combo_add(b, IDC_B_SOURCE, l);
        free(l);
        sl_push(&g_sources, keep);
        keep_idx = (int)g_sources.n - 1;
    }
    int pick = keep_idx >= 0 ? keep_idx : (g_ndisks ? (first_data >= 0 ? first_data : 0) : -1);
    SendDlgItemMessageW(b, IDC_B_SOURCE, CB_SETCURSEL, (WPARAM)pick, 0);
    SendDlgItemMessageW(r, IDC_R_TARGET, CB_SETCURSEL, 0, 0);
    if (!g_ndisks)
        status("Nessun disco trovato: avvia BKHD Backup come amministratore oppure scegli un file immagine.");
    free(keep);
}

static void suggest_name(void)
{
    HWND b = g_page[0];
    if (checked(b, IDC_B_RESUME))
        return;
    int sel = (int)SendDlgItemMessageW(b, IDC_B_SOURCE, CB_GETCURSEL, 0, 0);
    wchar_t host[64];
    DWORD hl = 64;
    char *h = GetComputerNameW(host, &hl) ? utf8(host) : xstrdup("pc");
    for (char *p = h; *p; p++) {
        if (*p >= 'A' && *p <= 'Z')
            *p = (char)(*p + 32);
        else if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '-'))
            *p = '-';
    }
    SYSTEMTIME st;
    GetLocalTime(&st);
    char what[32] = "immagine";
    if (sel >= 0 && (size_t)sel < g_sources.n && path_is_device(g_sources.v[sel])) {
        int dn = device_disk_number(g_sources.v[sel]);
        snprintf(what, sizeof what, "disco%d", dn);
    }
    char *name = xasprintf("%s-%s-%04u%02u%02u-%02u%02u", h, what, st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute);
    set_text(b, IDC_B_NAME, name);
    free(name);
    free(h);
}

static DWORD WINAPI remotes_thread(LPVOID p)
{
    (void)p;
    Rclone rc;
    StrList *l = xcalloc(1, sizeof *l);
    if (!rc_init(&rc, NULL, NULL)) {
        rc_listremotes(&rc, l);
        rc_free(&rc);
    }
    PostMessageW(g_main, WM_APP_REMOTES, 0, (LPARAM)l);
    return 0;
}

static void load_remotes(void)
{
    if (!g_cloud_ok)
        return;
    HANDLE t = CreateThread(NULL, 0, remotes_thread, NULL, 0, NULL);
    if (t)
        CloseHandle(t);
}

/* --- stato dei controlli ------------------------------------------------------------------------- */
static void update_backup_controls(void)
{
    HWND b = g_page[0];
    int busy = g_busy != 0, cloud = checked(b, IDC_B_CLOUD), enc = checked(b, IDC_B_ENCRYPT);
    static const int all[] = {IDC_B_SOURCE, IDC_B_REFRESH, IDC_B_IMAGE, IDC_B_CLOUD, IDC_B_LOCAL, IDC_B_PATH,
                              IDC_B_NAME, IDC_B_FIND, IDC_B_CHUNK, IDC_B_ZSTD, IDC_B_ENCRYPT, IDC_B_RETRY, IDC_B_SKIP,
                              IDC_B_RESUME, IDC_B_START};
    for (size_t i = 0; i < sizeof all / sizeof all[0]; i++)
        EnableWindow(GetDlgItem(b, all[i]), !busy);
    EnableWindow(GetDlgItem(b, IDC_B_CLOUD), !busy && g_cloud_ok);
    EnableWindow(GetDlgItem(b, IDC_B_REMOTE), !busy && cloud);
    EnableWindow(GetDlgItem(b, IDC_B_CONFIG), !busy && cloud);
    EnableWindow(GetDlgItem(b, IDC_B_FOLDER), !busy && !cloud);
    EnableWindow(GetDlgItem(b, IDC_B_BROWSE), !busy && !cloud);
    EnableWindow(GetDlgItem(b, IDC_B_PW1), !busy && enc);
    EnableWindow(GetDlgItem(b, IDC_B_PW2), !busy && enc);
}

static void update_restore_controls(void)
{
    HWND r = g_page[1];
    int busy = g_busy != 0, cloud = checked(r, IDC_R_CLOUD), verify = checked(r, IDC_R_VERIFY),
        disk = checked(r, IDC_R_DISK);
    static const int all[] = {IDC_R_CLOUD, IDC_R_LOCAL, IDC_R_PATH, IDC_R_SEARCH, IDC_R_LIST, IDC_R_PW, IDC_R_VERIFY,
                              IDC_R_START};
    for (size_t i = 0; i < sizeof all / sizeof all[0]; i++)
        EnableWindow(GetDlgItem(r, all[i]), !busy);
    EnableWindow(GetDlgItem(r, IDC_R_CLOUD), !busy && g_cloud_ok);
    EnableWindow(GetDlgItem(r, IDC_R_REMOTE), !busy && cloud);
    EnableWindow(GetDlgItem(r, IDC_R_CONFIG), !busy && cloud);
    EnableWindow(GetDlgItem(r, IDC_R_FOLDER), !busy && !cloud);
    EnableWindow(GetDlgItem(r, IDC_R_BROWSE), !busy && !cloud);
    EnableWindow(GetDlgItem(r, IDC_R_DISK), !busy && !verify);
    EnableWindow(GetDlgItem(r, IDC_R_FILE), !busy && !verify);
    EnableWindow(GetDlgItem(r, IDC_R_TARGET), !busy && !verify && disk);
    EnableWindow(GetDlgItem(r, IDC_R_REFRESH), !busy && !verify && disk);
    EnableWindow(GetDlgItem(r, IDC_R_IMG), !busy && !verify && !disk);
    EnableWindow(GetDlgItem(r, IDC_R_IMGBROWSE), !busy && !verify && !disk);
}

static void update_all(void)
{
    update_backup_controls();
    update_restore_controls();
    EnableWindow(GetDlgItem(g_main, IDC_CANCEL_JOB), g_busy != 0);
}

static void update_ram_info(void)
{
    HWND b = g_page[0];
    uint64_t avail = mem_available();
    char *a = human((double)avail);
    char *t = checked(b, IDC_B_CLOUD)
                  ? xasprintf("RAM disponibile: %s. Ogni pacchetto passa dalla RAM prima di essere caricato.%s", a,
                              sizeof(void *) == 4 ? " Versione a 32 bit: pacchetti sul cloud fino a 700 MB." : "")
                  : xasprintf("I pacchetti vengono scritti direttamente sulla destinazione (RAM disponibile: %s).", a);
    set_text(b, IDC_B_INFO, t);
    free(t);
    free(a);
}

/* --- motore in un thread ------------------------------------------------------------------------- */
enum { JOB_BACKUP, JOB_RESTORE, JOB_LIST };

typedef struct {
    int kind, list_ctx;
    BackupOpts b;
    RestoreOpts r;
    ListOpts l;
    StrList keep;              /* stringhe di proprieta' del lavoro */
    char *pw;
} Job;

static const char *keep(Job *j, char *s)          /* prende possesso di s */
{
    if (!s)
        return NULL;
    sl_push(&j->keep, s);
    free(s);
    return j->keep.v[j->keep.n - 1];
}

static void gui_emit(Reporter *r, JVal *ev)
{
    (void)r;
    PostMessageW(g_main, WM_APP_EVENT, 0, (LPARAM)jclone(ev));
}

static DWORD WINAPI job_thread(LPVOID p)
{
    Job *j = p;
    Reporter rep;
    rep_init(&rep, gui_emit, NULL);
    char *msg = NULL;
    int r;
    if (j->kind == JOB_LIST) {
        JVal *items = NULL;
        r = engine_list(&j->l, &rep, &items);
        if (r)
            msg = xstrdup(last_err());
        PostMessageW(g_main, WM_APP_LIST, (WPARAM)j->list_ctx, (LPARAM)(r ? NULL : items));
    } else if (j->kind == JOB_BACKUP) {
        r = engine_backup(&j->b, &rep, &msg);
    } else {
        r = engine_restore(&j->r, &rep, &msg);
    }
    if (r == FAIL)
        msg = xstrdup(last_err());
    if (j->pw) {
        SecureZeroMemory(j->pw, strlen(j->pw));
        free(j->pw);
    }
    sl_free(&j->keep);
    int kind = j->kind;
    free(j);
    PostMessageW(g_main, WM_APP_DONE, (WPARAM)((r & 0xFF) | (kind << 8)), (LPARAM)msg);
    return 0;
}

static void start_job(Job *j)
{
    InterlockedExchange(&g_cancel, 0);
    InterlockedExchange(&g_busy, 1);
    update_all();
    SendDlgItemMessageW(g_main, IDC_PROG1, PBM_SETPOS, 0, 0);
    SendDlgItemMessageW(g_main, IDC_PROG2, PBM_SETPOS, 0, 0);
    g_thread = CreateThread(NULL, 0, job_thread, j, 0, NULL);
}

/* --- eventi dal motore --------------------------------------------------------------------------- */
static double jd(const JVal *o, const char *k)
{
    JVal *v = jobj_get(o, k);
    return v ? (v->t == J_FLOAT ? v->d : v->t == J_INT ? (double)v->i : 0) : 0;
}

static void on_event(JVal *ev)
{
    const char *type = jget_str(ev, "type");
    if (!type)
        return;
    if (!strcmp(type, "log")) {
        const char *lv = jget_str(ev, "level");
        char *t = xasprintf("%s%s", lv && !strcmp(lv, "warning") ? "ATTENZIONE: " : lv && !strcmp(lv, "error") ? "ERRORE: " : "",
                            jget_str(ev, "msg"));
        log_line(t);
        free(t);
    } else if (!strcmp(type, "phase")) {
        char *t = xasprintf("Pacchetto %.0f/%.0f: %s", jd(ev, "chunk"), jd(ev, "chunks"), jget_str(ev, "label"));
        status(t);
        free(t);
        SendDlgItemMessageW(g_main, IDC_PROG1, PBM_SETPOS, 0, 0);
    } else if (!strcmp(type, "progress")) {
        SendDlgItemMessageW(g_main, IDC_PROG1, PBM_SETPOS, (WPARAM)(jd(ev, "frac") * 1000), 0);
        SendDlgItemMessageW(g_main, IDC_PROG2, PBM_SETPOS, (WPARAM)(jd(ev, "overall") * 1000), 0);
        char *sp = human(jd(ev, "speed"));
        char *t = xasprintf("Pacchetto %.0f/%.0f  ·  %s/s  ·  totale %.1f%%", jd(ev, "chunk"), jd(ev, "chunks"), sp,
                            100 * jd(ev, "overall"));
        set_text(g_main, IDC_LBL_TOTAL, "Totale");
        status(t);
        free(t);
        free(sp);
    } else if (!strcmp(type, "chunk_done")) {
        const char *m = jget_str(ev, "msg");
        if (m)
            log_line(m);
        SendDlgItemMessageW(g_main, IDC_PROG2, PBM_SETPOS, (WPARAM)(jd(ev, "overall") * 1000), 0);
        JVal *eta = jobj_get(ev, "eta");
        if (eta) {
            int s = (int)jd(ev, "eta");
            char *t = xasprintf("%.0f/%.0f pacchetti  ·  tempo stimato rimanente %d h %02d min", jd(ev, "chunk"),
                                jd(ev, "chunks"), s / 3600, (s / 60) % 60);
            status(t);
            free(t);
        }
    } else if (!strcmp(type, "start")) {
        const char *mode = jget_str(ev, "mode");
        log_line(!strcmp(mode, "backup") ? "--- Backup avviato ---" : !strcmp(mode, "verify") ? "--- Verifica avviata ---"
                                                                                              : "--- Ripristino avviato ---");
    }
}

static void fill_found_list(void)
{
    HWND lv = GetDlgItem(g_page[1], IDC_R_LIST);
    ListView_DeleteAllItems(lv);
    for (size_t k = 0; g_found && k < g_found->n; k++) {
        JVal *m = g_found->items[k];
        JVal *src = jobj_get(m, "source");
        int64_t size = 0, done = 0, cc = 0;
        jget_int(src, "size", &size);
        jget_int(m, "_chunks_done", &done);
        jget_int(m, "chunk_count", &cc);
        const char *created = jget_str(m, "created");
        char *hs = human((double)size);
        const char *st = jget_str(m, "status");
        JVal *enc = jobj_get(m, "encryption");
        char date[32] = "";
        if (created && strlen(created) >= 16)        /* 2026-11-21T09:18 -> 21/11/2026 09:18 */
            snprintf(date, sizeof date, "%.2s/%.2s/%.4s %.5s", created + 8, created + 5, created, created + 11);
        char *cols[6] = {xstrdup(jget_str(m, "name")), xstrdup(date),
                         xstrdup(st && !strcmp(st, "complete") ? "completo" : "interrotto"), xstrdup(hs),
                         xasprintf("%lld/%lld", (long long)done, (long long)cc),
                         xstrdup(enc && enc->t == J_OBJ ? "sì" : "no")};
        for (int c = 0; c < 6; c++) {
            wchar_t *w = wide(cols[c]);
            if (c == 0) {
                LVITEMW it;
                memset(&it, 0, sizeof it);
                it.mask = LVIF_TEXT | LVIF_PARAM;
                it.iItem = (int)k;
                it.pszText = w;
                it.lParam = (LPARAM)k;
                SendMessageW(lv, LVM_INSERTITEMW, 0, (LPARAM)&it);
            } else {
                LVITEMW it;
                memset(&it, 0, sizeof it);
                it.iSubItem = c;
                it.pszText = w;
                SendMessageW(lv, LVM_SETITEMTEXTW, (WPARAM)k, (LPARAM)&it);
            }
            free(w);
            free(cols[c]);
        }
        free(hs);
    }
    if (g_found && g_found->n) {
        ListView_SetItemState(lv, 0, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED);
    }
}

static INT_PTR CALLBACK pick_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_INITDIALOG) {
        JVal *items = (JVal *)lp;
        for (size_t k = 0; k < items->n; k++) {
            JVal *m = items->items[k];
            int64_t done = 0, cc = 0;
            jget_int(m, "_chunks_done", &done);
            jget_int(m, "chunk_count", &cc);
            const char *created = jget_str(m, "created");
            char *t = xasprintf("%s   (%.16s, %lld/%lld pacchetti)", jget_str(m, "name"), created ? created : "",
                                (long long)done, (long long)cc);
            wchar_t *w = wide(t);
            SendDlgItemMessageW(dlg, IDC_P_LIST, LB_ADDSTRING, 0, (LPARAM)w);
            free(w);
            free(t);
        }
        SendDlgItemMessageW(dlg, IDC_P_LIST, LB_SETCURSEL, 0, 0);
        return TRUE;
    }
    if (msg == WM_COMMAND) {
        if (LOWORD(wp) == IDOK || (LOWORD(wp) == IDC_P_LIST && HIWORD(wp) == LBN_DBLCLK)) {
            EndDialog(dlg, SendDlgItemMessageW(dlg, IDC_P_LIST, LB_GETCURSEL, 0, 0));
            return TRUE;
        }
        if (LOWORD(wp) == IDCANCEL) {
            EndDialog(dlg, -1);
            return TRUE;
        }
    }
    return FALSE;
}

static void apply_interrupted(JVal *m)
{
    HWND b = g_page[0];
    set_text(b, IDC_B_NAME, jget_str(m, "name"));
    CheckDlgButton(b, IDC_B_RESUME, BST_CHECKED);
    int64_t cs = 0;
    jget_int(m, "chunk_size", &cs);
    int found = -1;
    for (int i = 0; i < NPRESETS; i++)
        if (PRESETS[i].kib * 1024 - (PRESETS[i].kib * 1024) % 512 == cs)
            found = i;
    if (found >= 0) {
        SendDlgItemMessageW(b, IDC_B_CHUNK, CB_SETCURSEL, (WPARAM)found, 0);
    } else {
        char *t = xasprintf("%lldK", (long long)(cs / 1024));
        set_text(b, IDC_B_CHUNK, t);
        free(t);
    }
    const char *comp = jget_str(m, "compression");
    CheckDlgButton(b, IDC_B_ZSTD, comp && !strcmp(comp, "zstd") ? BST_CHECKED : BST_UNCHECKED);
    JVal *enc = jobj_get(m, "encryption");
    int protected_ = enc && enc->t == J_OBJ;
    CheckDlgButton(b, IDC_B_ENCRYPT, protected_ ? BST_CHECKED : BST_UNCHECKED);
    const char *mode = jget_str(m, "bad_sector_mode");
    int skip = mode && !strcmp(mode, "skip");
    CheckRadioButton(b, IDC_B_RETRY, IDC_B_SKIP, skip ? IDC_B_SKIP : IDC_B_RETRY);
    update_backup_controls();
    log_line(protected_ ? "Backup interrotto selezionato: e' protetto, inserisci la password e premi «Avvia backup»."
                        : "Backup interrotto selezionato: controlla il disco di origine e premi «Avvia backup».");
}

static void on_list(int ctx, JVal *items)
{
    if (!items)
        return;
    if (ctx == 0) {
        jfree(g_found);
        g_found = items;
        fill_found_list();
        char *t = xasprintf("Trovati %u backup.", (unsigned)items->n);
        status(t);
        free(t);
        return;
    }
    if (!items->n) {
        msg_box(g_main, "Nessun backup interrotto in questa destinazione.", MB_ICONINFORMATION);
    } else {
        INT_PTR k = DialogBoxParamW(g_inst, MAKEINTRESOURCEW(IDD_PICK), g_main, pick_proc, (LPARAM)items);
        if (k >= 0 && (size_t)k < items->n)
            apply_interrupted(items->items[k]);
    }
    jfree(items);
}

/* --- azioni -------------------------------------------------------------------------------------- */
static char *chunk_kib_text(HWND b, int64_t *kib)
{
    /* conta il testo mostrato: una voce dell'elenco oppure una misura scritta a mano (es. 3M, 1440K, 4G) */
    char *t = get_text(b, IDC_B_CHUNK);
    for (int i = 0; i < NPRESETS; i++) {
        size_t n = strlen(PRESETS[i].label);
        if (!strncmp(t, PRESETS[i].label, n) && (t[n] == 0 || t[n] == ' ')) {
            *kib = PRESETS[i].kib;
            free(t);
            return NULL;
        }
    }
    *kib = parse_size_kib(t);
    return t;
}

static char *dest_remote(HWND dlg, int cloud_id, int remote_id, int folder_id)
{
    if (checked(dlg, cloud_id)) {
        char *r = get_text(dlg, remote_id);
        if (*r && !strchr(r, ':')) {             /* nome del remote senza i due punti */
            char *t = xasprintf("%s:", r);
            free(r);
            r = t;
        }
        return r;
    }
    return get_text(dlg, folder_id);
}

static void on_find_interrupted(void)
{
    HWND b = g_page[0];
    Job *j = xcalloc(1, sizeof *j);
    j->kind = JOB_LIST;
    j->list_ctx = 1;
    j->l.remote = keep(j, dest_remote(b, IDC_B_CLOUD, IDC_B_REMOTE, IDC_B_FOLDER));
    j->l.path = keep(j, get_text(b, IDC_B_PATH));
    j->l.interrupted = 1;
    if (!*j->l.remote) {
        msg_box(g_main, "Scegli prima il cloud o la cartella di destinazione.", MB_ICONWARNING);
        sl_free(&j->keep);
        free(j);
        return;
    }
    status("Ricerca dei backup interrotti...");
    start_job(j);
}

static void on_start_backup(void)
{
    HWND b = g_page[0];
    int sel = (int)SendDlgItemMessageW(b, IDC_B_SOURCE, CB_GETCURSEL, 0, 0);
    if (sel < 0 || (size_t)sel >= g_sources.n) {
        msg_box(g_main, "Scegli il disco da copiare.", MB_ICONWARNING);
        return;
    }
    Job *j = xcalloc(1, sizeof *j);
    j->kind = JOB_BACKUP;
    backup_opts_default(&j->b);
    j->b.source = keep(j, xstrdup(g_sources.v[sel]));
    j->b.remote = keep(j, dest_remote(b, IDC_B_CLOUD, IDC_B_REMOTE, IDC_B_FOLDER));
    j->b.path = keep(j, get_text(b, IDC_B_PATH));
    j->b.name = keep(j, get_text(b, IDC_B_NAME));
    char *bad = chunk_kib_text(b, &j->b.chunk_kib);
    j->b.compression = checked(b, IDC_B_ZSTD) ? "zstd" : "none";
    j->b.encrypt = checked(b, IDC_B_ENCRYPT);
    j->b.resume = checked(b, IDC_B_RESUME);
    j->b.bad_sectors = checked(b, IDC_B_SKIP) ? "skip" : "retry";
    const char *problem = NULL;
    if (!*j->b.remote)
        problem = checked(b, IDC_B_CLOUD) ? "Scegli il cloud (oppure premi «Configura cloud…»)."
                                          : "Scegli il disco o la cartella di destinazione.";
    else if (!checked(b, IDC_B_CLOUD) && !path_is_local(j->b.remote))
        problem = "La destinazione deve essere un percorso completo, es. E:\\Backup oppure \\\\server\\cartella.";
    else if (j->b.chunk_kib < 0)
        problem = "Dimensione dei pacchetti non valida (esempi: 650M, 1440K, 4G).";
    else if (!valid_name(j->b.name))
        problem = "Nome del backup non valido: usa lettere, numeri, punto, trattino e trattino basso.";
    free(bad);
    if (!problem && j->b.encrypt) {
        char *p1 = get_text(b, IDC_B_PW1), *p2 = get_text(b, IDC_B_PW2);
        if (!j->b.resume && strcmp(p1, p2))
            problem = "Le due password non coincidono.";
        else if (strlen(p1) < 8)
            problem = "La password deve avere almeno 8 caratteri.";
        j->pw = p1;
        j->b.password = p1;
        SecureZeroMemory(p2, strlen(p2));
        free(p2);
        if (j->b.resume)
            j->b.encrypt = 0;                     /* la ripresa usa la cifratura gia' registrata */
    }
    if (problem) {
        msg_box(g_main, problem, MB_ICONWARNING);
        if (j->pw)
            free(j->pw);
        sl_free(&j->keep);
        free(j);
        return;
    }
    for (int i = 0; i < g_ndisks; i++)
        if (!strcmp(g_disks[i].path, j->b.source) && g_disks[i].is_system &&
            msg_box(g_main, "Stai copiando il disco su cui gira Windows: i file aperti possono cambiare durante la copia. "
                            "Per una copia perfetta avvia il PC dalla pennetta BKHD Live.\n\nContinuare comunque?",
                    MB_ICONWARNING | MB_YESNO) != IDYES) {
            if (j->pw)
                free(j->pw);
            sl_free(&j->keep);
            free(j);
            return;
        }
    SetDlgItemTextW(b, IDC_B_PW1, L"");
    SetDlgItemTextW(b, IDC_B_PW2, L"");
    start_job(j);
}

static void on_search(void)
{
    HWND r = g_page[1];
    Job *j = xcalloc(1, sizeof *j);
    j->kind = JOB_LIST;
    j->list_ctx = 0;
    j->l.remote = keep(j, dest_remote(r, IDC_R_CLOUD, IDC_R_REMOTE, IDC_R_FOLDER));
    j->l.path = keep(j, get_text(r, IDC_R_PATH));
    if (!*j->l.remote) {
        msg_box(g_main, "Scegli il cloud o la cartella in cui cercare i backup.", MB_ICONWARNING);
        sl_free(&j->keep);
        free(j);
        return;
    }
    free(g_found_base);
    g_found_base = xstrdup(j->l.path);
    status("Ricerca dei backup...");
    start_job(j);
}

typedef struct {
    const char *text, *word;
} ConfirmCtx;

static INT_PTR CALLBACK confirm_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    static ConfirmCtx *c;
    if (msg == WM_INITDIALOG) {
        c = (ConfirmCtx *)lp;
        set_text(dlg, IDC_C_TEXT, c->text);
        return TRUE;
    }
    if (msg == WM_COMMAND) {
        if (LOWORD(wp) == IDC_C_EDIT && HIWORD(wp) == EN_CHANGE) {
            char *t = get_text(dlg, IDC_C_EDIT);
            EnableWindow(GetDlgItem(dlg, IDOK), !_stricmp(t, c->word));
            free(t);
        } else if (LOWORD(wp) == IDOK || LOWORD(wp) == IDCANCEL) {
            EndDialog(dlg, LOWORD(wp));
        }
        return TRUE;
    }
    return FALSE;
}

static void on_start_restore(void)
{
    HWND r = g_page[1];
    HWND lv = GetDlgItem(r, IDC_R_LIST);
    int sel = ListView_GetNextItem(lv, -1, LVNI_SELECTED);
    if (!g_found || sel < 0 || (size_t)sel >= g_found->n) {
        msg_box(g_main, "Premi «Cerca backup» e scegli il backup da ripristinare.", MB_ICONWARNING);
        return;
    }
    JVal *m = g_found->items[sel];
    Job *j = xcalloc(1, sizeof *j);
    j->kind = JOB_RESTORE;
    restore_opts_default(&j->r);
    j->r.remote = keep(j, dest_remote(r, IDC_R_CLOUD, IDC_R_REMOTE, IDC_R_FOLDER));
    const char *rel = jget_str(m, "_rel");
    j->r.path = keep(j, rel && *rel ? xasprintf("%s/%s", g_found_base ? g_found_base : "", rel)
                                    : xstrdup(g_found_base ? g_found_base : ""));
    j->r.verify_only = checked(r, IDC_R_VERIFY);
    JVal *enc = jobj_get(m, "encryption");
    const char *st = jget_str(m, "status");
    if (st && strcmp(st, "complete")) {
        if (msg_box(g_main, "Questo backup e' incompleto (interrotto): verranno ripristinati solo i pacchetti "
                            "salvati. Continuare?", MB_ICONWARNING | MB_YESNO) != IDYES)
            goto cancel;
        j->r.allow_incomplete = 1;
    }
    if (enc && enc->t == J_OBJ) {
        j->pw = get_text(r, IDC_R_PW);
        if (!*j->pw) {
            msg_box(g_main, "Questo backup e' protetto: inserisci la password.", MB_ICONWARNING);
            goto cancel;
        }
        j->r.password = j->pw;
        j->r.expect_encrypted = 1;
    }
    if (!j->r.verify_only) {
        int64_t size = 0;
        jget_int(jobj_get(m, "source"), "size", &size);
        char *hs = human((double)size);
        if (checked(r, IDC_R_DISK)) {
            int t = (int)SendDlgItemMessageW(r, IDC_R_TARGET, CB_GETCURSEL, 0, 0);
            if (t < 0 || (size_t)t >= g_targets.n) {
                free(hs);
                msg_box(g_main, "Scegli il disco di destinazione.", MB_ICONWARNING);
                goto cancel;
            }
            j->r.target = keep(j, xstrdup(g_targets.v[t]));
            int dn = device_disk_number(j->r.target);
            char *word = xasprintf("DISCO %d", dn);
            char *text = xasprintf("TUTTI i dati del Disco %d verranno sostituiti con il backup «%s» (%s). L'operazione "
                                   "non si puo' annullare.\n\nPer confermare scrivi qui sotto: %s",
                                   dn, jget_str(m, "name"), hs, word);
            ConfirmCtx c = {text, word};
            INT_PTR ok = DialogBoxParamW(g_inst, MAKEINTRESOURCEW(IDD_CONFIRM), g_main, confirm_proc, (LPARAM)&c);
            free(text);
            free(word);
            free(hs);
            if (ok != IDOK)
                goto cancel;
        } else {
            char *img = get_text(r, IDC_R_IMG);
            free(hs);
            if (!path_is_local(img)) {
                free(img);
                msg_box(g_main, "Scegli il file immagine di destinazione (percorso completo).", MB_ICONWARNING);
                goto cancel;
            }
            wchar_t *w = wide(img);
            DWORD a = GetFileAttributesW(w);
            free(w);
            if (a != INVALID_FILE_ATTRIBUTES) {
                char *q = xasprintf("Il file %s esiste gia' e verra' sovrascritto. Continuare?", img);
                int ans = msg_box(g_main, q, MB_ICONWARNING | MB_YESNO);
                free(q);
                if (ans != IDYES) {
                    free(img);
                    goto cancel;
                }
            }
            j->r.target = keep(j, img);
        }
        j->r.yes = 1;
    }
    SetDlgItemTextW(r, IDC_R_PW, L"");
    start_job(j);
    return;
cancel:
    if (j->pw) {
        SecureZeroMemory(j->pw, strlen(j->pw));
        free(j->pw);
    }
    sl_free(&j->keep);
    free(j);
}

static void browse_folder(HWND dlg, int edit_id)
{
    BROWSEINFOW bi;
    memset(&bi, 0, sizeof bi);
    bi.hwndOwner = g_main;
    bi.lpszTitle = L"Scegli il disco o la cartella dei backup";
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
    LPITEMIDLIST pidl = SHBrowseForFolderW(&bi);
    if (!pidl)
        return;
    wchar_t path[MAX_PATH];
    if (SHGetPathFromIDListW(pidl, path))
        SetDlgItemTextW(dlg, edit_id, path);
    CoTaskMemFree(pidl);
}

static void choose_image_source(void)
{
    wchar_t file[MAX_PATH * 2] = L"";
    OPENFILENAMEW of;
    memset(&of, 0, sizeof of);
    of.lStructSize = sizeof of;
    of.hwndOwner = g_main;
    of.lpstrFilter = L"Immagini disco (*.img;*.iso;*.raw;*.dd;*.bin)\0*.img;*.iso;*.raw;*.dd;*.bin\0Tutti i file\0*.*\0";
    of.lpstrFile = file;
    of.nMaxFile = MAX_PATH * 2;
    of.Flags = OFN_FILEMUSTEXIST | OFN_HIDEREADONLY | OFN_EXPLORER;
    if (!GetOpenFileNameW(&of))
        return;
    char *p = utf8(file), *l = xasprintf("File immagine: %s", p);
    combo_add(g_page[0], IDC_B_SOURCE, l);
    sl_push(&g_sources, p);
    SendDlgItemMessageW(g_page[0], IDC_B_SOURCE, CB_SETCURSEL, (WPARAM)(g_sources.n - 1), 0);
    free(l);
    free(p);
    suggest_name();
}

static void choose_image_target(void)
{
    wchar_t file[MAX_PATH * 2] = L"ripristino.img";
    OPENFILENAMEW of;
    memset(&of, 0, sizeof of);
    of.lStructSize = sizeof of;
    of.hwndOwner = g_main;
    of.lpstrFilter = L"Immagini disco (*.img)\0*.img\0Tutti i file\0*.*\0";
    of.lpstrFile = file;
    of.nMaxFile = MAX_PATH * 2;
    of.lpstrDefExt = L"img";
    of.Flags = OFN_HIDEREADONLY | OFN_EXPLORER;
    if (GetSaveFileNameW(&of))
        SetDlgItemTextW(g_page[1], IDC_R_IMG, file);
}

static void on_config_cloud(void)
{
    Rclone rc;
    if (rc_init(&rc, NULL, NULL)) {
        msg_box(g_main, last_err(), MB_ICONWARNING);
        return;
    }
    if (rc_open_config_console(&rc)) {
        msg_box(g_main, last_err(), MB_ICONERROR);
    } else {
        msg_box(g_main, "Si e' aperta la finestra di configurazione di rclone: segui le istruzioni per collegare il tuo "
                        "cloud (Google Drive, OneDrive, Dropbox, Mega, ...).\n\nQuando hai finito chiudi quella finestra "
                        "e premi OK qui.", MB_ICONINFORMATION);
        load_remotes();
    }
    rc_free(&rc);
}

static void about(void)
{
    char *t = xasprintf("BKHD Backup per Windows %s (%s)\n\nBackup settore per settore di interi dischi direttamente sul "
                        "cloud (rclone, un pacchetto alla volta in RAM) oppure su un disco o una cartella di rete. I backup "
                        "sono compatibili con BKHD Backup per Linux e con la pennetta BKHD Live.\n\n© 2026 %s\nLicenza GNU "
                        "GPL versione 3 o successiva.\nhttps://github.com/fabionet/bkhd-backup\n\nComponenti: zstd (BSD), "
                        "Mbed TLS (Apache 2.0), rclone (MIT).",
                        BKHD_VERSION, sizeof(void *) == 8 ? "64 bit" : "32 bit, CPU con SSE", BKHD_AUTHOR);
    msg_box(g_main, t, MB_ICONINFORMATION);
    free(t);
}

/* --- finestre ------------------------------------------------------------------------------------ */
static void theme_page(HWND page)
{
    typedef HRESULT(WINAPI * EnableTex)(HWND, DWORD);
    HMODULE ux = LoadLibraryW(L"uxtheme.dll");
    if (ux) {
        EnableTex f = (EnableTex)(void *)GetProcAddress(ux, "EnableThemeDialogTexture");
        if (f)
            f(page, 6 /* ETDT_ENABLETAB */);
    }
}

static INT_PTR CALLBACK backup_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    (void)lp;
    if (msg == WM_INITDIALOG) {
        theme_page(dlg);
        return TRUE;
    }
    if (msg != WM_COMMAND)
        return FALSE;
    switch (LOWORD(wp)) {
    case IDC_B_CLOUD:
    case IDC_B_LOCAL:
        update_backup_controls();
        update_ram_info();
        break;
    case IDC_B_ENCRYPT:
        update_backup_controls();
        break;
    case IDC_B_SOURCE:
        if (HIWORD(wp) == CBN_SELCHANGE)
            suggest_name();
        break;
    case IDC_B_REFRESH:
        load_disks();
        break;
    case IDC_B_IMAGE:
        choose_image_source();
        break;
    case IDC_B_BROWSE:
        browse_folder(dlg, IDC_B_FOLDER);
        break;
    case IDC_B_CONFIG:
        on_config_cloud();
        break;
    case IDC_B_FIND:
        on_find_interrupted();
        break;
    case IDC_B_START:
        on_start_backup();
        break;
    }
    return TRUE;
}

static INT_PTR CALLBACK restore_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_INITDIALOG) {
        theme_page(dlg);
        HWND lv = GetDlgItem(dlg, IDC_R_LIST);
        SendMessageW(lv, LVM_SETEXTENDEDLISTVIEWSTYLE, 0, LVS_EX_FULLROWSELECT | LVS_EX_GRIDLINES);
        static const wchar_t *cols[] = {L"Nome", L"Data", L"Stato", L"Disco", L"Pacchetti", L"Protetto"};
        static const int widths[] = {210, 110, 75, 80, 75, 60};
        for (int c = 0; c < 6; c++) {
            LVCOLUMNW col;
            memset(&col, 0, sizeof col);
            col.mask = LVCF_TEXT | LVCF_WIDTH | LVCF_SUBITEM;
            col.pszText = (wchar_t *)cols[c];
            col.cx = widths[c];
            col.iSubItem = c;
            SendMessageW(lv, LVM_INSERTCOLUMNW, (WPARAM)c, (LPARAM)&col);
        }
        return TRUE;
    }
    if (msg == WM_NOTIFY) {
        NMHDR *nm = (NMHDR *)lp;
        if (nm->idFrom == IDC_R_LIST && nm->code == NM_DBLCLK)
            on_start_restore();
        return FALSE;
    }
    if (msg != WM_COMMAND)
        return FALSE;
    switch (LOWORD(wp)) {
    case IDC_R_CLOUD:
    case IDC_R_LOCAL:
    case IDC_R_DISK:
    case IDC_R_FILE:
    case IDC_R_VERIFY:
        update_restore_controls();
        break;
    case IDC_R_BROWSE:
        browse_folder(dlg, IDC_R_FOLDER);
        break;
    case IDC_R_CONFIG:
        on_config_cloud();
        break;
    case IDC_R_SEARCH:
        on_search();
        break;
    case IDC_R_REFRESH:
        load_disks();
        break;
    case IDC_R_IMGBROWSE:
        choose_image_target();
        break;
    case IDC_R_START:
        on_start_restore();
        break;
    }
    return TRUE;
}

static void show_page(int i)
{
    ShowWindow(g_page[0], i == 0 ? SW_SHOW : SW_HIDE);
    ShowWindow(g_page[1], i == 1 ? SW_SHOW : SW_HIDE);
}

static void init_main(HWND dlg)
{
    g_main = dlg;
    g_tab = GetDlgItem(dlg, IDC_TAB);
    HICON ic = LoadIconW(g_inst, MAKEINTRESOURCEW(IDI_APP));
    SendMessageW(dlg, WM_SETICON, ICON_BIG, (LPARAM)ic);
    SendMessageW(dlg, WM_SETICON, ICON_SMALL, (LPARAM)LoadImageW(g_inst, MAKEINTRESOURCEW(IDI_APP), IMAGE_ICON, 16, 16, 0));
    LOGFONTW lf;
    HFONT base = (HFONT)SendMessageW(dlg, WM_GETFONT, 0, 0);
    GetObjectW(base, sizeof lf, &lf);
    lf.lfHeight = lf.lfHeight * 9 / 5;
    lf.lfWeight = FW_BOLD;
    g_title_font = CreateFontIndirectW(&lf);
    SendDlgItemMessageW(dlg, IDC_TITLE, WM_SETFONT, (WPARAM)g_title_font, TRUE);
    SendDlgItemMessageW(dlg, IDC_PROG1, PBM_SETRANGE32, 0, 1000);
    SendDlgItemMessageW(dlg, IDC_PROG2, PBM_SETRANGE32, 0, 1000);
    SendDlgItemMessageW(dlg, IDC_LOG, EM_SETLIMITTEXT, 0x7FFFFFFE, 0);
    TCITEMW ti;
    memset(&ti, 0, sizeof ti);
    ti.mask = TCIF_TEXT;
    ti.pszText = L"  Backup  ";
    SendMessageW(g_tab, TCM_INSERTITEMW, 0, (LPARAM)&ti);
    ti.pszText = L"  Ripristino  ";
    SendMessageW(g_tab, TCM_INSERTITEMW, 1, (LPARAM)&ti);
    g_page[0] = CreateDialogParamW(g_inst, MAKEINTRESOURCEW(IDD_BACKUP), dlg, backup_proc, 0);
    g_page[1] = CreateDialogParamW(g_inst, MAKEINTRESOURCEW(IDD_RESTORE), dlg, restore_proc, 0);
    RECT rc;
    GetWindowRect(g_tab, &rc);
    MapWindowPoints(NULL, dlg, (POINT *)&rc, 2);
    TabCtrl_AdjustRect(g_tab, FALSE, &rc);
    for (int i = 0; i < 2; i++)
        SetWindowPos(g_page[i], HWND_TOP, rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top, 0);
    show_page(0);

    HWND b = g_page[0], r = g_page[1];
    for (int i = 0; i < NPRESETS; i++) {
        char *l = (sizeof(void *) == 4 && PRESETS[i].kib > 700 * 1024) ? xasprintf("%s (solo su disco)", PRESETS[i].label)
                                                                     : xstrdup(PRESETS[i].label);
        combo_add(b, IDC_B_CHUNK, l);
        free(l);
    }
    SendDlgItemMessageW(b, IDC_B_CHUNK, CB_SETCURSEL, sizeof(void *) == 4 ? 5 : 7, 0);   /* 650 MB / 1 GB */
    set_text(b, IDC_B_PATH, "BKHD");
    set_text(r, IDC_R_PATH, "BKHD");
    CheckRadioButton(b, IDC_B_RETRY, IDC_B_SKIP, IDC_B_RETRY);
    CheckRadioButton(r, IDC_R_DISK, IDC_R_FILE, IDC_R_FILE);

    Rclone rcl;
    g_cloud_ok = !rc_init(&rcl, NULL, NULL);
    if (g_cloud_ok)
        rc_free(&rcl);
    else
        g_cloud_why = xstrdup(last_err());
    CheckRadioButton(b, IDC_B_CLOUD, IDC_B_LOCAL, g_cloud_ok ? IDC_B_CLOUD : IDC_B_LOCAL);
    CheckRadioButton(r, IDC_R_CLOUD, IDC_R_LOCAL, g_cloud_ok ? IDC_R_CLOUD : IDC_R_LOCAL);
    load_disks();
    suggest_name();
    update_all();
    update_ram_info();
    if (!g_cloud_ok) {
        char *t = xasprintf("Cloud non disponibile: %s.", g_cloud_why);
        log_line(t);
        free(t);
    } else {
        load_remotes();
    }
    g_auto = env("BKHD_GUI_AUTOTEST");
    if (g_auto)
        SetTimer(dlg, 1, 1500, NULL);
    char *hello = xasprintf("BKHD Backup %s per Windows (%s). Scegli il disco, la destinazione e premi «Avvia backup».",
                            BKHD_VERSION, sizeof(void *) == 8 ? "64 bit" : "32 bit");
    status(hello);
    free(hello);
}

/* prova automatica: backup protetto di un file immagine, ricerca e ripristino, come farebbe una persona */
static void auto_step(void)
{
    HWND b = g_page[0], r = g_page[1];
    char *src = env("BKHD_GUI_SOURCE"), *dst = env("BKHD_GUI_DEST"), *pw = env("BKHD_GUI_PASSWORD");
    if (g_auto_step == 0) {
        combo_add(b, IDC_B_SOURCE, "File immagine di prova");
        sl_push(&g_sources, src ? src : "");
        SendDlgItemMessageW(b, IDC_B_SOURCE, CB_SETCURSEL, (WPARAM)(g_sources.n - 1), 0);
        CheckRadioButton(b, IDC_B_CLOUD, IDC_B_LOCAL, IDC_B_LOCAL);
        set_text(b, IDC_B_FOLDER, dst ? dst : "");
        set_text(b, IDC_B_NAME, "prova-gui");
        set_text(b, IDC_B_CHUNK, "3M");
        CheckDlgButton(b, IDC_B_ZSTD, BST_CHECKED);
        CheckDlgButton(b, IDC_B_ENCRYPT, BST_CHECKED);
        update_backup_controls();
        set_text(b, IDC_B_PW1, pw ? pw : "");
        set_text(b, IDC_B_PW2, pw ? pw : "");
        auto_log("PASSO: avvio backup");
        g_auto_step = 1;
        SendMessageW(GetDlgItem(b, IDC_B_START), BM_CLICK, 0, 0);
    } else if (g_auto_step == 2) {
        TabCtrl_SetCurSel(g_tab, 1);
        show_page(1);
        CheckRadioButton(r, IDC_R_CLOUD, IDC_R_LOCAL, IDC_R_LOCAL);
        update_restore_controls();
        set_text(r, IDC_R_FOLDER, dst ? dst : "");
        auto_log("PASSO: ricerca backup");
        g_auto_step = 3;
        SendMessageW(GetDlgItem(r, IDC_R_SEARCH), BM_CLICK, 0, 0);
    } else if (g_auto_step == 4) {
        CheckRadioButton(r, IDC_R_DISK, IDC_R_FILE, IDC_R_FILE);
        update_restore_controls();
        char *img = xasprintf("%s\\ripristinato.img", dst ? dst : "");
        set_text(r, IDC_R_IMG, img);
        free(img);
        set_text(r, IDC_R_PW, pw ? pw : "");
        auto_log("PASSO: avvio ripristino");
        g_auto_step = 5;
        SendMessageW(GetDlgItem(r, IDC_R_START), BM_CLICK, 0, 0);
    }
    free(src);
    free(dst);
    free(pw);
}

static void auto_done(int kind, int code, const char *m)
{
    char *t = xasprintf("FINE %s: %s %s", kind == JOB_BACKUP ? "backup" : kind == JOB_RESTORE ? "ripristino" : "elenco",
                        code == OK ? "OK" : "ERRORE", m ? m : "");
    auto_log(t);
    free(t);
    if (code != OK) {
        auto_log("AUTOTEST-ERRORE");
        PostMessageW(g_main, WM_CLOSE, 0, 0);
    } else if (kind == JOB_BACKUP && g_auto_step == 1) {
        g_auto_step = 2;
        SetTimer(g_main, 1, 4000, NULL);         /* pausa per la fotografia della schermata */
    } else if (kind == JOB_LIST && g_auto_step == 3) {
        g_auto_step = 4;
        SetTimer(g_main, 1, 1500, NULL);
    } else if (kind == JOB_RESTORE && g_auto_step == 5) {
        auto_log("AUTOTEST-OK");
        SetTimer(g_main, 2, 4000, NULL);
    }
}

static INT_PTR CALLBACK main_proc(HWND dlg, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_TIMER:
        KillTimer(dlg, wp);
        if (wp == 2)
            PostMessageW(dlg, WM_CLOSE, 0, 0);
        else
            auto_step();
        return TRUE;
    case WM_INITDIALOG:
        init_main(dlg);
        return TRUE;
    case WM_NOTIFY: {
        NMHDR *nm = (NMHDR *)lp;
        if (nm->idFrom == IDC_TAB && nm->code == TCN_SELCHANGE)
            show_page(TabCtrl_GetCurSel(g_tab));
        return TRUE;
    }
    case WM_COMMAND:
        if (LOWORD(wp) == IDC_CANCEL_JOB && g_busy) {
            InterlockedExchange(&g_cancel, 1);
            status("Annullamento in corso: attendo la fine dell'operazione sul pacchetto corrente...");
            EnableWindow(GetDlgItem(dlg, IDC_CANCEL_JOB), FALSE);
        } else if (LOWORD(wp) == IDC_ABOUT) {
            about();
        } else if (LOWORD(wp) == IDCANCEL) {
            SendMessageW(dlg, WM_CLOSE, 0, 0);
        }
        return TRUE;
    case WM_APP_EVENT:
        on_event((JVal *)lp);
        jfree((JVal *)lp);
        return TRUE;
    case WM_APP_LIST:
        on_list((int)wp, (JVal *)lp);
        return TRUE;
    case WM_APP_REMOTES: {
        StrList *l = (StrList *)lp;
        for (int p = 0; p < 2; p++) {
            int id = p == 0 ? IDC_B_REMOTE : IDC_R_REMOTE;
            char *cur = get_text(g_page[p], id);
            SendDlgItemMessageW(g_page[p], id, CB_RESETCONTENT, 0, 0);
            for (size_t k = 0; k < l->n; k++)
                combo_add(g_page[p], id, l->v[k]);
            if (*cur)
                set_text(g_page[p], id, cur);
            else if (l->n)
                SendDlgItemMessageW(g_page[p], id, CB_SETCURSEL, 0, 0);
            free(cur);
        }
        if (!l->n)
            log_line("Nessun cloud configurato: premi «Configura cloud…» per collegarne uno, oppure salva su un disco.");
        sl_free(l);
        free(l);
        return TRUE;
    }
    case WM_APP_DONE: {
        int code = (signed char)(wp & 0xFF), kind = (int)(wp >> 8);
        char *m = (char *)lp;
        if (g_thread) {
            WaitForSingleObject(g_thread, 5000);
            CloseHandle(g_thread);
            g_thread = NULL;
        }
        InterlockedExchange(&g_busy, 0);
        update_all();
        if (kind == JOB_LIST) {
            if (code == FAIL) {
                status("Ricerca non riuscita.");
                msg_box(dlg, m ? m : "errore", MB_ICONERROR);
            }
        } else if (code == OK) {
            SendDlgItemMessageW(dlg, IDC_PROG1, PBM_SETPOS, 1000, 0);
            SendDlgItemMessageW(dlg, IDC_PROG2, PBM_SETPOS, 1000, 0);
            status(m ? m : "Completato.");
            log_line(m ? m : "Completato.");
            msg_box(dlg, m ? m : "Completato.", MB_ICONINFORMATION);
            if (kind == JOB_BACKUP) {
                CheckDlgButton(g_page[0], IDC_B_RESUME, BST_UNCHECKED);
                suggest_name();
            }
        } else if (code == CANCELLED) {
            const char *t = kind == JOB_BACKUP ? "Operazione annullata. Il backup puo' essere ripreso con «Cerca backup "
                                                 "interrotti» e «Riprendi»."
                                               : "Operazione annullata.";
            status(t);
            log_line(t);
        } else {
            char *t = xasprintf("ERRORE: %s", m ? m : "errore");
            status(t);
            log_line(t);
            msg_box(dlg, m ? m : "errore", MB_ICONERROR);
            free(t);
        }
        if (g_auto)
            auto_done(kind, code, m);
        free(m);
        return TRUE;
    }
    case WM_CLOSE:
        if (g_busy) {
            if (msg_box(dlg, "Un'operazione e' in corso. Vuoi annullarla e chiudere il programma?\n\nUn backup annullato "
                             "si puo' riprendere in seguito.", MB_ICONWARNING | MB_YESNO) != IDYES)
                return TRUE;
            InterlockedExchange(&g_cancel, 1);
            if (g_thread)
                WaitForSingleObject(g_thread, 60000);
        }
        DestroyWindow(dlg);
        return TRUE;
    case WM_DESTROY:
        PostQuitMessage(0);
        return TRUE;
    }
    return FALSE;
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    (void)prev;
    (void)cmd;
    g_inst = inst;
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX);
    INITCOMMONCONTROLSEX icc = {sizeof icc, ICC_TAB_CLASSES | ICC_LISTVIEW_CLASSES | ICC_PROGRESS_CLASS | ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&icc);
    OleInitialize(NULL);
    HWND dlg = CreateDialogParamW(inst, MAKEINTRESOURCEW(IDD_MAIN), NULL, main_proc, 0);
    if (!dlg) {
        MessageBoxW(NULL, L"Impossibile creare la finestra principale.", L"BKHD Backup", MB_ICONERROR);
        return 1;
    }
    ShowWindow(dlg, show);
    MSG m;
    while (GetMessageW(&m, NULL, 0, 0) > 0) {
        if (!IsDialogMessageW(dlg, &m)) {
            TranslateMessage(&m);
            DispatchMessageW(&m);
        }
    }
    OleUninitialize();
    return 0;
}
