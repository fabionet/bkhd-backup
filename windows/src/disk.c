/* BKHD Backup per Windows - dischi fisici, volumi e file immagine (vedi disk.h).
 * Copyright (C) 2026 fabionet - SPDX-License-Identifier: GPL-3.0-or-later */
#include "disk.h"
#include <winioctl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wctype.h>

/* strutture non sempre dichiarate con _WIN32_WINNT=XP */
#define BKHD_StorageAccessAlignmentProperty 6
typedef struct {
    DWORD Version, Size, BytesPerCacheLine, BytesOffsetForCacheAlignment, BytesPerLogicalSector,
        BytesPerPhysicalSector, BytesOffsetForSectorAlignment;
} BKHD_ALIGNMENT_DESCRIPTOR;

#define MAX_VOL_DISKS 8
typedef struct {
    wchar_t name[MAX_PATH];        /* \\?\Volume{...}\ */
    int disks[MAX_VOL_DISKS];
    int ndisks;
    char letters[128];
} VolInfo;

static wchar_t *strip_slash(const wchar_t *v)
{
    size_t n = wcslen(v);
    wchar_t *w = xmalloc((n + 1) * sizeof(wchar_t));
    wcscpy(w, v);
    if (n && w[n - 1] == L'\\')
        w[n - 1] = 0;
    return w;
}

static int volume_extents(HANDLE h, int *disks, int max)
{
    union {
        VOLUME_DISK_EXTENTS v;
        char raw[sizeof(VOLUME_DISK_EXTENTS) + 32 * sizeof(DISK_EXTENT)];
    } buf;
    DWORD got = 0;
    if (!DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, NULL, 0, &buf, sizeof buf, &got, NULL))
        return 0;
    int n = 0;
    for (DWORD i = 0; i < buf.v.NumberOfDiskExtents && n < max; i++) {
        int dn = (int)buf.v.Extents[i].DiskNumber, dup = 0;
        for (int k = 0; k < n; k++)
            dup |= disks[k] == dn;
        if (!dup)
            disks[n++] = dn;
    }
    return n;
}

static int enum_volumes(VolInfo **out)
{
    int n = 0, cap = 16;
    VolInfo *v = xcalloc((size_t)cap, sizeof *v);
    wchar_t name[MAX_PATH];
    HANDLE f = FindFirstVolumeW(name, MAX_PATH);
    if (f == INVALID_HANDLE_VALUE) {
        *out = v;
        return 0;
    }
    do {
        if (n == cap) {
            cap *= 2;
            v = xrealloc(v, (size_t)cap * sizeof *v);
        }
        VolInfo *vi = &v[n];
        memset(vi, 0, sizeof *vi);
        wcsncpy(vi->name, name, MAX_PATH - 1);
        wchar_t *dev = strip_slash(name);
        HANDLE h = CreateFileW(dev, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
        free(dev);
        if (h != INVALID_HANDLE_VALUE) {
            vi->ndisks = volume_extents(h, vi->disks, MAX_VOL_DISKS);
            CloseHandle(h);
        }
        wchar_t paths[1024];
        DWORD len = 0;
        if (GetVolumePathNamesForVolumeNameW(name, paths, 1024, &len)) {
            for (wchar_t *p = paths; *p; p += wcslen(p) + 1) {
                char *u = utf8(p);
                size_t ul = strlen(u);
                if (ul && u[ul - 1] == '\\')
                    u[ul - 1] = 0;
                if (strlen(vi->letters) + strlen(u) + 2 < sizeof vi->letters) {
                    if (vi->letters[0])
                        strcat(vi->letters, " ");
                    strcat(vi->letters, u);
                }
                free(u);
            }
        }
        n++;
    } while (FindNextVolumeW(f, name, MAX_PATH));
    FindVolumeClose(f);
    *out = v;
    return n;
}

/* volume (\\?\Volume{...}\) che contiene un percorso */
static int volume_of_path(const wchar_t *path, wchar_t *vol, DWORD vlen)
{
    wchar_t full[MAX_PATH * 2], root[MAX_PATH * 2];
    if (!GetFullPathNameW(path, MAX_PATH * 2, full, NULL))
        return -1;
    if (!GetVolumePathNameW(full, root, MAX_PATH * 2))
        return -1;
    if (!GetVolumeNameForVolumeMountPointW(root, vol, vlen))
        return -1;
    return 0;
}

static int system_disks(int *disks, int max)
{
    wchar_t win[MAX_PATH], vol[MAX_PATH];
    if (!GetSystemWindowsDirectoryW(win, MAX_PATH) || volume_of_path(win, vol, MAX_PATH))
        return 0;
    wchar_t *dev = strip_slash(vol);
    HANDLE h = CreateFileW(dev, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    free(dev);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    int n = volume_extents(h, disks, max);
    CloseHandle(h);
    return n;
}

static char *trim_dup(const char *s)
{
    while (*s == ' ')
        s++;
    char *r = xstrdup(s);
    size_t n = strlen(r);
    while (n && (r[n - 1] == ' ' || r[n - 1] == '\t'))
        r[--n] = 0;
    return r;
}

static const char *bus_name(int t)
{
    switch (t) {
    case 1: return "SCSI";
    case 2: return "ATAPI";
    case 3: return "ATA";
    case 4: return "1394";
    case 6: return "FC";
    case 7: return "USB";
    case 8: return "RAID";
    case 10: return "SAS";
    case 11: return "SATA";
    case 12: return "SD";
    case 13: return "MMC";
    case 17: return "NVMe";
    default: return "";
    }
}

/* informazioni comuni a dischi e volumi */
static void query_device(HANDLE h, DiskInfo *d)
{
    DWORD got;
    GET_LENGTH_INFORMATION len;
    if (DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, NULL, 0, &len, sizeof len, &got, NULL))
        d->size = len.Length.QuadPart;
    DISK_GEOMETRY geo;
    d->logical_sector = 512;
    if (DeviceIoControl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY, NULL, 0, &geo, sizeof geo, &got, NULL) &&
        geo.BytesPerSector >= 512 && geo.BytesPerSector <= 65536)
        d->logical_sector = (int)geo.BytesPerSector;
    d->physical_sector = d->logical_sector;
    STORAGE_PROPERTY_QUERY q;
    memset(&q, 0, sizeof q);
    q.PropertyId = (STORAGE_PROPERTY_ID)BKHD_StorageAccessAlignmentProperty;
    q.QueryType = PropertyStandardQuery;
    BKHD_ALIGNMENT_DESCRIPTOR al;
    memset(&al, 0, sizeof al);
    if (DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY, &q, sizeof q, &al, sizeof al, &got, NULL) &&
        got >= sizeof al && al.BytesPerPhysicalSector >= (DWORD)d->logical_sector && al.BytesPerPhysicalSector <= 65536)
        d->physical_sector = (int)al.BytesPerPhysicalSector;

    q.PropertyId = StorageDeviceProperty;
    static char desc[4096];
    memset(desc, 0, sizeof desc);
    if (DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY, &q, sizeof q, desc, sizeof desc, &got, NULL) &&
        got >= sizeof(STORAGE_DEVICE_DESCRIPTOR)) {
        STORAGE_DEVICE_DESCRIPTOR *sd = (STORAGE_DEVICE_DESCRIPTOR *)desc;
        char model[512] = "";
        if (sd->VendorIdOffset && sd->VendorIdOffset < got) {
            char *v = trim_dup(desc + sd->VendorIdOffset);
            snprintf(model, sizeof model, "%s", v);
            free(v);
        }
        if (sd->ProductIdOffset && sd->ProductIdOffset < got) {
            char *p = trim_dup(desc + sd->ProductIdOffset);
            size_t ml = strlen(model);
            snprintf(model + ml, sizeof model - ml, "%s%s", ml ? " " : "", p);
            free(p);
        }
        d->model = xstrdup(model);
        if (sd->SerialNumberOffset && sd->SerialNumberOffset != 0xFFFFFFFF && sd->SerialNumberOffset < got)
            d->serial = trim_dup(desc + sd->SerialNumberOffset);
        d->removable = sd->RemovableMedia ? 1 : 0;
        d->bus = xstrdup(bus_name((int)sd->BusType));
        if (sd->BusType == 7)
            d->removable = 1;
    }
    static char layout[65536];
    if (DeviceIoControl(h, IOCTL_DISK_GET_DRIVE_LAYOUT_EX, NULL, 0, layout, sizeof layout, &got, NULL)) {
        DRIVE_LAYOUT_INFORMATION_EX *l = (DRIVE_LAYOUT_INFORMATION_EX *)layout;
        if (l->PartitionStyle == PARTITION_STYLE_MBR) {
            d->pttype = xstrdup("dos");
            d->ptuuid = xasprintf("%08lx", (unsigned long)l->Mbr.Signature);
        } else if (l->PartitionStyle == PARTITION_STYLE_GPT) {
            GUID *g = &l->Gpt.DiskId;
            d->pttype = xstrdup("gpt");
            d->ptuuid = xasprintf("%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x", (unsigned long)g->Data1,
                                  g->Data2, g->Data3, g->Data4[0], g->Data4[1], g->Data4[2], g->Data4[3],
                                  g->Data4[4], g->Data4[5], g->Data4[6], g->Data4[7]);
        }
    }
}

int disk_list(DiskInfo **out, int *count)
{
    VolInfo *vols;
    int nv = enum_volumes(&vols);
    int sys[MAX_VOL_DISKS], nsys = system_disks(sys, MAX_VOL_DISKS);
    int n = 0;
    DiskInfo *list = xcalloc(64, sizeof *list);
    for (int i = 0; i < 64; i++) {
        wchar_t path[64];
        swprintf(path, 64, L"\\\\.\\PhysicalDrive%d", i);
        HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
        if (h == INVALID_HANDLE_VALUE)
            continue;
        DiskInfo *d = &list[n];
        memset(d, 0, sizeof *d);
        d->number = i;
        d->path = xasprintf("\\\\.\\PhysicalDrive%d", i);
        query_device(h, d);
        CloseHandle(h);
        if (d->size < MIB) {                        /* lettori vuoti, dispositivi senza supporto */
            free(d->path);
            free(d->model);
            free(d->serial);
            free(d->bus);
            free(d->pttype);
            free(d->ptuuid);
            continue;
        }
        StrBuf letters;
        sb_init(&letters);
        for (int v = 0; v < nv; v++)
            for (int k = 0; k < vols[v].ndisks; k++)
                if (vols[v].disks[k] == i) {
                    d->volumes++;
                    if (vols[v].letters[0]) {
                        if (letters.n)
                            sb_putc(&letters, ' ');
                        sb_puts(&letters, vols[v].letters);
                    }
                }
        d->letters = sb_steal(&letters);
        for (int k = 0; k < nsys; k++)
            d->is_system |= sys[k] == i;
        n++;
    }
    free(vols);
    *out = list;
    *count = n;
    return OK;
}

void disk_list_free(DiskInfo *d, int count)
{
    for (int i = 0; i < count; i++) {
        free(d[i].path);
        free(d[i].model);
        free(d[i].serial);
        free(d[i].bus);
        free(d[i].pttype);
        free(d[i].ptuuid);
        free(d[i].letters);
    }
    free(d);
}

char *disk_label(const DiskInfo *d)
{
    char *size = human((double)d->size);
    char *s = xasprintf("Disco %d  —  %s  —  %s%s%s%s%s%s", d->number, d->model && *d->model ? d->model : "disco",
                        size, d->letters && *d->letters ? "  [" : "", d->letters ? d->letters : "",
                        d->letters && *d->letters ? "]" : "", d->bus && *d->bus ? "  " : "", d->bus ? d->bus : "");
    if (d->is_system) {
        char *t = xasprintf("%s  (Windows)", s);
        free(s);
        s = t;
    }
    free(size);
    return s;
}

/* --- percorsi ------------------------------------------------------------------------------------ */
int path_is_device(const char *p)
{
    return p && p[0] == '\\' && p[1] == '\\' && p[2] == '.' && p[3] == '\\';
}

int device_disk_number(const char *dev)
{
    if (!path_is_device(dev))
        return -1;
    const char *p = dev + 4;
    if (!_strnicmp(p, "PhysicalDrive", 13))
        return atoi(p + 13);
    wchar_t *w = wide(dev);
    HANDLE h = CreateFileW(w, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    free(w);
    if (h == INVALID_HANDLE_VALUE)
        return -1;
    int disks[MAX_VOL_DISKS];
    int n = volume_extents(h, disks, MAX_VOL_DISKS);
    CloseHandle(h);
    return n ? disks[0] : -1;
}

int disk_of_path(const char *path, int *numbers, int max)
{
    if (path_is_device(path)) {
        int dn = device_disk_number(path);
        if (dn < 0 || max < 1)
            return 0;
        numbers[0] = dn;
        return 1;
    }
    wchar_t *w = wide(path), vol[MAX_PATH];
    /* la cartella potrebbe non esistere ancora: si risale fino a una cartella esistente */
    size_t len = wcslen(w);
    while (len > 3 && GetFileAttributesW(w) == INVALID_FILE_ATTRIBUTES) {
        wchar_t *slash = wcsrchr(w, L'\\');
        if (!slash || slash <= w + 2)
            break;
        *slash = 0;
        len = wcslen(w);
    }
    int r = volume_of_path(w, vol, MAX_PATH);
    free(w);
    if (r)
        return 0;
    wchar_t *dev = strip_slash(vol);
    HANDLE h = CreateFileW(dev, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    free(dev);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    int n = volume_extents(h, numbers, max);
    CloseHandle(h);
    return n;
}

char *in_use_reason(const char *dev)
{
    int dn = device_disk_number(dev);
    if (dn < 0)
        return NULL;
    if (_strnicmp(dev + 4, "PhysicalDrive", 13))
        return xstrdup("volume montato");
    VolInfo *vols;
    int nv = enum_volumes(&vols);
    int sys[MAX_VOL_DISKS], nsys = system_disks(sys, MAX_VOL_DISKS), is_sys = 0;
    for (int k = 0; k < nsys; k++)
        is_sys |= sys[k] == dn;
    StrBuf b;
    sb_init(&b);
    int mounted = 0;
    for (int v = 0; v < nv; v++)
        for (int k = 0; k < vols[v].ndisks; k++)
            if (vols[v].disks[k] == dn) {
                mounted++;
                if (vols[v].letters[0]) {
                    sb_puts(&b, b.n ? " " : "");
                    sb_puts(&b, vols[v].letters);
                }
            }
    free(vols);
    char *r = NULL;
    if (is_sys)
        r = xasprintf("contiene Windows in esecuzione%s%s", b.n ? ", volumi " : "", b.n ? b.s : "");
    else if (mounted)
        r = xasprintf("%d volumi montati%s%s", mounted, b.n ? ": " : "", b.n ? b.s : "");
    sb_free(&b);
    return r;
}

/* --- informazioni sulla sorgente ----------------------------------------------------------------- */
int source_info(const char *path, JVal **info)
{
    wchar_t *w = wide(path);
    int block = path_is_device(path);
    HANDLE h = CreateFileW(w, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    free(w);
    if (h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile aprire %s", path);
        return FAIL;
    }
    DiskInfo d;
    memset(&d, 0, sizeof d);
    if (block) {
        query_device(h, &d);
    } else {
        BY_HANDLE_FILE_INFORMATION fi;
        LARGE_INTEGER sz;
        if (!GetFileInformationByHandle(h, &fi) || (fi.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
            !GetFileSizeEx(h, &sz)) {
            CloseHandle(h);
            set_err("%s non e' un disco, un volume o un file immagine", path);
            return FAIL;
        }
        d.size = sz.QuadPart;
        d.logical_sector = d.physical_sector = 512;
    }
    CloseHandle(h);
    JVal *o = jobj();
    jobj_set(o, "path", jstr(path));
    jobj_set(o, "kind", jstr(block ? "block" : "file"));
    jobj_set(o, "size", jint(d.size));
    jobj_set(o, "logical_sector", jint(d.logical_sector));
    jobj_set(o, "physical_sector", jint(d.physical_sector));
    if (block) {
        jobj_set(o, "model", jstr(d.model ? d.model : ""));
        jobj_set(o, "serial", d.serial ? jstr(d.serial) : jnull());
        jobj_set(o, "pttype", d.pttype ? jstr(d.pttype) : jnull());
        jobj_set(o, "ptuuid", d.ptuuid ? jstr(d.ptuuid) : jnull());
        if (d.bus && *d.bus)
            jobj_set(o, "bus", jstr(d.bus));
    }
    jobj_set(o, "platform", jstr("windows"));
    free(d.model);
    free(d.serial);
    free(d.bus);
    free(d.pttype);
    free(d.ptuuid);
    *info = o;
    return OK;
}

/* --- lettura e scrittura ------------------------------------------------------------------------- */
int read_error_is_bad(DWORD e)
{
    switch (e) {
    case ERROR_CRC:                       /* 23 */
    case ERROR_SECTOR_NOT_FOUND:          /* 27 */
    case ERROR_READ_FAULT:                /* 30 */
    case ERROR_GEN_FAILURE:               /* 31 */
    case ERROR_SEM_TIMEOUT:               /* 121 */
    case 483:                             /* ERROR_DEVICE_HARDWARE_ERROR */
    case ERROR_IO_DEVICE:                 /* 1117 */
        return 1;
    default:
        return 0;
    }
}

int dev_open_read(const char *path, Dev *d)
{
    memset(d, 0, sizeof *d);
    d->block = path_is_device(path);
    wchar_t *w = wide(path);
    d->h = CreateFileW(w, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                       d->block ? FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN : FILE_FLAG_SEQUENTIAL_SCAN,
                       NULL);
    free(w);
    if (d->h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile aprire %s", path);
        return FAIL;
    }
    if (d->block) {
        DiskInfo di;
        memset(&di, 0, sizeof di);
        query_device(d->h, &di);
        d->size = di.size;
        d->sector = di.logical_sector;
        free(di.model);
        free(di.serial);
        free(di.bus);
        free(di.pttype);
        free(di.ptuuid);
    } else {
        LARGE_INTEGER sz;
        GetFileSizeEx(d->h, &sz);
        d->size = sz.QuadPart;
        d->sector = 512;
    }
    return OK;
}

/* test automatici: settori illeggibili simulati (variabile BKHD_TEST_BAD_SECTORS, offset in byte) */
static int test_bad(int64_t off, size_t n)
{
    static int loaded;
    static int64_t bad[64];
    static int nbad;
    if (!loaded) {
        char buf[1024];
        DWORD k = GetEnvironmentVariableA("BKHD_TEST_BAD_SECTORS", buf, sizeof buf);
        loaded = 1;
        if (k && k < sizeof buf)
            for (char *p = strtok(buf, ","); p && nbad < 64; p = strtok(NULL, ","))
                bad[nbad++] = _atoi64(p);
    }
    for (int i = 0; i < nbad; i++)
        if (bad[i] >= off && bad[i] < off + (int64_t)n)
            return 1;
    return 0;
}

int dev_read_at(Dev *d, int64_t off, void *buf, size_t n, DWORD *werr)
{
    *werr = 0;
    if (test_bad(off, n)) {
        *werr = ERROR_CRC;
        set_err("settore illeggibile (simulato)");
        return FAIL;
    }
    size_t got = 0;
    while (got < n) {
        OVERLAPPED ov;
        memset(&ov, 0, sizeof ov);
        uint64_t pos = (uint64_t)off + got;
        ov.Offset = (DWORD)pos;
        ov.OffsetHigh = (DWORD)(pos >> 32);
        DWORD want = (DWORD)(n - got > (64u << 20) ? (64u << 20) : n - got), r = 0;
        if (!ReadFile(d->h, (char *)buf + got, want, &r, &ov)) {
            *werr = GetLastError();
            if (*werr == ERROR_HANDLE_EOF)
                set_err("fine del dispositivo inattesa all'offset %lld", (long long)pos);
            else
                set_win_err(*werr, "lettura all'offset %lld non riuscita", (long long)pos);
            return FAIL;
        }
        if (r == 0) {
            set_err("fine del dispositivo inattesa all'offset %lld", (long long)pos);
            *werr = ERROR_HANDLE_EOF;
            return FAIL;
        }
        got += r;
    }
    return OK;
}

static int raw_write(Dev *d, int64_t off, const void *buf, size_t n)
{
    size_t done = 0;
    while (done < n) {
        OVERLAPPED ov;
        memset(&ov, 0, sizeof ov);
        uint64_t pos = (uint64_t)off + done;
        ov.Offset = (DWORD)pos;
        ov.OffsetHigh = (DWORD)(pos >> 32);
        DWORD want = (DWORD)(n - done > (64u << 20) ? (64u << 20) : n - done), w = 0;
        if (!WriteFile(d->h, (const char *)buf + done, want, &w, &ov) || w == 0) {
            set_win_err(GetLastError(), "scrittura all'offset %lld non riuscita", (long long)pos);
            return FAIL;
        }
        done += w;
    }
    return OK;
}

/* modifica parziale di un settore (lettura, modifica, riscrittura) */
static int patch_sector(Dev *d, int64_t sec_off, size_t in_off, const uint8_t *data, size_t n)
{
    DWORD e;
    if (dev_read_at(d, sec_off, d->bounce, (size_t)d->sector, &e))
        return FAIL;
    memcpy(d->bounce + in_off, data, n);
    return raw_write(d, sec_off, d->bounce, (size_t)d->sector);
}

int dev_write_at(Dev *d, int64_t off, const void *buf, size_t n)
{
    const uint8_t *p = buf;
    if (!d->block)
        return raw_write(d, off, buf, n);
    int64_t s = d->sector;
    size_t head = (size_t)(off % s);
    if (head) {                                    /* inizio non allineato al settore della destinazione */
        size_t k = (size_t)s - head < n ? (size_t)s - head : n;
        if (patch_sector(d, off - (int64_t)head, head, p, k))
            return FAIL;
        p += k;
        off += (int64_t)k;
        n -= k;
    }
    size_t mid = n - n % (size_t)s;
    if (mid) {
        if (raw_write(d, off, p, mid))
            return FAIL;
        p += mid;
        off += (int64_t)mid;
        n -= mid;
    }
    if (n)
        return patch_sector(d, off, 0, p, n);
    return OK;
}

int dev_flush(Dev *d)
{
    if (!FlushFileBuffers(d->h)) {
        set_win_err(GetLastError(), "svuotamento della cache del disco non riuscito");
        return FAIL;
    }
    return OK;
}

void dev_close(Dev *d)
{
    DWORD got;
    if (d->h && d->h != INVALID_HANDLE_VALUE) {
        if (d->block && d->bounce)              /* ripristino su disco: Windows rilegge le partizioni */
            DeviceIoControl(d->h, IOCTL_DISK_UPDATE_PROPERTIES, NULL, 0, NULL, 0, &got, NULL);
        CloseHandle(d->h);
    }
    for (int i = 0; i < d->nlocks; i++) {
        DeviceIoControl(d->locks[i], FSCTL_UNLOCK_VOLUME, NULL, 0, NULL, 0, &got, NULL);
        CloseHandle(d->locks[i]);
    }
    if (d->bounce)
        VirtualFree(d->bounce, 0, MEM_RELEASE);
    memset(d, 0, sizeof *d);
}

/* blocca e smonta tutti i volumi del disco 'dn' (o il solo volume 'only') prima di scriverci */
static int lock_volumes(Dev *d, int dn, const char *only)
{
    if (only) {
        wchar_t *w = wide(only);
        HANDLE h = CreateFileW(w, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                               OPEN_EXISTING, 0, NULL);
        free(w);
        DWORD got;
        if (h == INVALID_HANDLE_VALUE || !DeviceIoControl(h, FSCTL_LOCK_VOLUME, NULL, 0, NULL, 0, &got, NULL)) {
            DWORD e = GetLastError();
            if (h != INVALID_HANDLE_VALUE)
                CloseHandle(h);
            set_win_err(e, "il volume %s e' in uso (chiudi i programmi che lo usano)", only);
            return FAIL;
        }
        DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, NULL, 0, NULL, 0, &got, NULL);
        d->locks[d->nlocks++] = h;
        return OK;
    }
    VolInfo *vols;
    int nv = enum_volumes(&vols);
    for (int v = 0; v < nv; v++) {
        int on = 0;
        for (int k = 0; k < vols[v].ndisks; k++)
            on |= vols[v].disks[k] == dn;
        if (!on)
            continue;
        if (vols[v].ndisks > 1) {
            free(vols);
            set_err("un volume del disco di destinazione occupa anche altri dischi (volume dinamico/esteso): "
                    "ripristino non consentito");
            return FAIL;
        }
        wchar_t *dev = strip_slash(vols[v].name);
        HANDLE h = CreateFileW(dev, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                               OPEN_EXISTING, 0, NULL);
        free(dev);
        DWORD got;
        if (h == INVALID_HANDLE_VALUE || d->nlocks >= 64 ||
            !DeviceIoControl(h, FSCTL_LOCK_VOLUME, NULL, 0, NULL, 0, &got, NULL)) {
            DWORD e = GetLastError();
            if (h != INVALID_HANDLE_VALUE)
                CloseHandle(h);
            set_win_err(e, "il volume %s del disco di destinazione e' in uso (chiudi i programmi e le finestre "
                           "che lo usano)", vols[v].letters[0] ? vols[v].letters : "senza lettera");
            free(vols);
            return FAIL;
        }
        DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, NULL, 0, NULL, 0, &got, NULL);
        d->locks[d->nlocks++] = h;
    }
    free(vols);
    return OK;
}

int dev_open_restore(const char *path, int64_t min_size, Dev *d, const char *backup_dir)
{
    memset(d, 0, sizeof *d);
    d->block = path_is_device(path);
    wchar_t *w = wide(path);
    if (d->block) {
        int dn = device_disk_number(path);
        int sys[MAX_VOL_DISKS], nsys = system_disks(sys, MAX_VOL_DISKS);
        for (int k = 0; k < nsys; k++)
            if (dn >= 0 && sys[k] == dn) {
                free(w);
                set_err("%s contiene Windows in esecuzione: per ripristinare il disco di sistema avvia il PC "
                        "dalla pennetta BKHD Live", path);
                return FAIL;
            }
        if (backup_dir) {
            int bd[MAX_VOL_DISKS], nb = disk_of_path(backup_dir, bd, MAX_VOL_DISKS);
            for (int k = 0; k < nb; k++)
                if (bd[k] == dn) {
                    free(w);
                    set_err("il backup da ripristinare si trova sul disco di destinazione: impossibile");
                    return FAIL;
                }
        }
        int is_disk = !_strnicmp(path + 4, "PhysicalDrive", 13);
        if (lock_volumes(d, dn, is_disk ? NULL : path)) {
            free(w);
            dev_close(d);
            return FAIL;
        }
        d->h = CreateFileW(w, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                           FILE_FLAG_WRITE_THROUGH, NULL);
    } else {
        d->h = CreateFileW(w, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
    }
    free(w);
    if (d->h == INVALID_HANDLE_VALUE) {
        set_win_err(GetLastError(), "impossibile aprire %s in scrittura", path);
        dev_close(d);
        return FAIL;
    }
    if (d->block) {
        DiskInfo di;
        memset(&di, 0, sizeof di);
        query_device(d->h, &di);
        d->size = di.size;
        d->sector = di.logical_sector;
        free(di.model);
        free(di.serial);
        free(di.bus);
        free(di.pttype);
        free(di.ptuuid);
        if (d->size < min_size) {
            char *a = human((double)d->size), *b = human((double)min_size);
            set_err("il disco di destinazione (%s) e' piu' piccolo del backup (%s)", a, b);
            free(a);
            free(b);
            dev_close(d);
            return FAIL;
        }
        d->bounce = VirtualAlloc(NULL, (size_t)d->sector, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    } else {
        DWORD got;
        DeviceIoControl(d->h, FSCTL_SET_SPARSE, NULL, 0, NULL, 0, &got, NULL);   /* lo spazio vuoto non occupa disco */
        d->sector = 512;
        d->size = min_size;
    }
    return OK;
}
