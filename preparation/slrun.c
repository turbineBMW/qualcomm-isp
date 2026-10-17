/* SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com> */
/* SPDX-License-Identifier: MIT */
/* slrun: minimal PE loader that runs the IPE striping library exported by the
 * Windows camera DLL (QcDeviceMFT8380.dll, ARM64 PE) natively on aarch64 Linux.
 *
 *   slrun <dll> version
 *   slrun <dll> run <configio.bin> <iq.bin> <titanVersion> <hwVersion> <out.bin> [maxCores] [w h]
 *
 * Only KERNEL32 imports are reachable from the striping exports; they get stubs.
 * The library allocates through the StripingLibMemOp callbacks we pass.
 * x18 (Windows TEB register) points at a zeroed fake TEB; __chkstk is patched
 * so it never probes. The two CamX log helpers are hooked and printed.
 * Build: clang -O1 -g -ffixed-x18 -o slrun slrun.c -lm -lcrypto
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <openssl/evp.h>
#include <math.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdarg.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <malloc.h>
#include <sys/mman.h>
#include <sys/stat.h>

#pragma pack(push, 1)
struct dos_hdr { uint16_t magic; uint8_t pad[58]; int32_t lfanew; };
struct file_hdr { uint16_t machine, nsec; uint32_t ts, symtab, nsyms; uint16_t optsz, chars; };
struct data_dir { uint32_t va, size; };
struct opt_hdr64 {
    uint16_t magic; uint8_t lmaj, lmin; uint32_t sizecode, sizeidata, sizeudata, entry, basecode;
    uint64_t imagebase; uint32_t secalign, filealign; uint16_t osmaj, osmin, imaj, imin, ssmaj, ssmin;
    uint32_t win32ver, sizeimage, sizehdrs, checksum; uint16_t subsys, dllchars;
    uint64_t stackres, stackcommit, heapres, heapcommit; uint32_t loaderflags, nrva; struct data_dir dir[16];
};
struct sec_hdr { char name[8]; uint32_t vsize, va, rawsize, rawptr, relocptr, lineptr; uint16_t nreloc, nline; uint32_t chars; };
struct imp_desc { uint32_t oft, ts, fwd, name, ft; };
struct exp_dir { uint32_t chars, ts; uint16_t maj, min; uint32_t name, base, nfunc, nnames, funcs, names, ords; };
struct tls_dir64 { uint64_t rawstart, rawend, indexaddr, callbacks; uint32_t zerofill, chars; };
#pragma pack(pop)

static uint8_t *g_base;          /* mapped image */
static uint64_t g_prefbase;      /* ImageBase from the header */
static uint32_t g_sizeimage;
static int g_verbose = 1;

#define IMG(rva) ((void *)(g_base + (rva)))
#define IN_IMAGE(p) ((uint8_t *)(p) >= g_base && (uint8_t *)(p) < g_base + g_sizeimage)

/* ---- x18 / TEB handling ------------------------------------------------- */
static uint8_t *g_teb;
#ifndef SHARP_OFF
#define SHARP_OFF 0x78
#endif
#define X18_SAVE   uint64_t _x18; __asm__ volatile("mov %0, x18" : "=r"(_x18))
#define X18_RESTORE __asm__ volatile("mov x18, %0" :: "r"(_x18))
static inline void set_x18(void *teb) { __asm__ volatile("mov x18, %0" :: "r"(teb)); }

/* ---- KERNEL32 stubs ---------------------------------------------------- */
static uint32_t g_lasterr;
static void *g_fls[256]; static uint32_t g_nfls = 1;

static uint32_t WINAPI_GetLastError(void) { return g_lasterr; }
static void WINAPI_SetLastError(uint32_t e) { g_lasterr = e; }
static void *WINAPI_HeapAlloc(void *h, uint32_t flags, size_t n) { X18_SAVE; void *p = (flags & 8) ? calloc(1, n) : malloc(n); X18_RESTORE; return p; }
static int WINAPI_HeapFree(void *h, uint32_t flags, void *p) { X18_SAVE; free(p); X18_RESTORE; return 1; }
static void *WINAPI_HeapReAlloc(void *h, uint32_t flags, void *p, size_t n) { X18_SAVE; void *q = realloc(p, n); X18_RESTORE; return q; }
static size_t WINAPI_HeapSize(void *h, uint32_t flags, void *p) { X18_SAVE; size_t n = malloc_usable_size(p); X18_RESTORE; return n; }
static void *WINAPI_GetProcessHeap(void) { return (void *)1; }
static void WINAPI_nop(void) {}
static int WINAPI_ret1(void) { return 1; }
static int WINAPI_ret0(void) { return 0; }
static uint32_t WINAPI_FlsAlloc(void *cb) { return g_nfls++; }
static void *WINAPI_FlsGetValue(uint32_t i) { return i < 256 ? g_fls[i] : NULL; }
static int WINAPI_FlsSetValue(uint32_t i, void *v) { if (i < 256) g_fls[i] = v; return 1; }
static void *WINAPI_EncodePointer(void *p) { return p; }
static int WINAPI_QueryPerformanceCounter(int64_t *p) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); *p = ts.tv_sec * 1000000000LL + ts.tv_nsec; return 1; }
static int WINAPI_QueryPerformanceFrequency(int64_t *p) { *p = 1000000000LL; return 1; }
static int WINAPI_WriteFile(void *h, const void *buf, uint32_t n, uint32_t *written, void *ov) {
    X18_SAVE; fprintf(stderr, "[dll WriteFile h=%p] %.*s\n", h, (int)n, (const char *)buf); X18_RESTORE;
    if (written) *written = n; return 1;
}
static void *WINAPI_CreateFileW(void) { return (void *)-1; }
static void WINAPI_RaiseException(uint32_t code) { fprintf(stderr, "RaiseException(%#x) from DLL\n", code); abort(); }
static void WINAPI_ExitProcess(uint32_t code) { fprintf(stderr, "DLL called ExitProcess(%u)\n", code); exit(100 + (code & 0xff)); }
static void unknown_import(void) {
    void *lr = __builtin_return_address(0);
    fprintf(stderr, "unknown import called from image+%#lx\n", (unsigned long)((uint8_t *)lr - g_base));
    abort();
}

struct stub { const char *name; void *fn; };
static const struct stub g_stubs[] = {
    {"GetLastError", WINAPI_GetLastError}, {"SetLastError", WINAPI_SetLastError},
    {"HeapAlloc", WINAPI_HeapAlloc}, {"HeapFree", WINAPI_HeapFree}, {"HeapReAlloc", WINAPI_HeapReAlloc},
    {"HeapSize", WINAPI_HeapSize}, {"GetProcessHeap", WINAPI_GetProcessHeap},
    {"EnterCriticalSection", WINAPI_nop}, {"LeaveCriticalSection", WINAPI_nop},
    {"DeleteCriticalSection", WINAPI_nop}, {"InitializeCriticalSection", WINAPI_nop},
    {"InitializeCriticalSectionEx", WINAPI_ret1}, {"TryEnterCriticalSection", WINAPI_ret1},
    {"InitializeSRWLock", WINAPI_nop}, {"AcquireSRWLockExclusive", WINAPI_nop},
    {"ReleaseSRWLockExclusive", WINAPI_nop}, {"TryAcquireSRWLockExclusive", WINAPI_ret1},
    {"InitializeConditionVariable", WINAPI_nop}, {"InitializeSListHead", WINAPI_nop},
    {"FlsAlloc", WINAPI_FlsAlloc}, {"FlsGetValue", WINAPI_FlsGetValue}, {"FlsSetValue", WINAPI_FlsSetValue},
    {"FlsFree", WINAPI_ret1}, {"TlsAlloc", WINAPI_FlsAlloc}, {"TlsGetValue", WINAPI_FlsGetValue},
    {"TlsSetValue", WINAPI_FlsSetValue}, {"TlsFree", WINAPI_ret1},
    {"EncodePointer", WINAPI_EncodePointer}, {"DecodePointer", WINAPI_EncodePointer},
    {"GetModuleHandleW", WINAPI_ret0}, {"GetModuleHandleA", WINAPI_ret0}, {"GetModuleHandleExW", WINAPI_ret0},
    {"GetProcAddress", WINAPI_ret0}, {"LoadLibraryExW", WINAPI_ret0}, {"LoadLibraryW", WINAPI_ret0},
    {"LoadLibraryA", WINAPI_ret0}, {"FreeLibrary", WINAPI_ret1},
    {"GetCurrentThreadId", WINAPI_ret1}, {"GetCurrentProcessId", WINAPI_ret1}, {"GetCurrentProcess", WINAPI_ret1},
    {"QueryPerformanceCounter", WINAPI_QueryPerformanceCounter},
    {"QueryPerformanceFrequency", WINAPI_QueryPerformanceFrequency},
    {"GetSystemTimeAsFileTime", WINAPI_nop}, {"GetTickCount64", WINAPI_ret0}, {"Sleep", WINAPI_nop},
    {"IsDebuggerPresent", WINAPI_ret0}, {"OutputDebugStringW", WINAPI_nop},
    {"WriteFile", WINAPI_WriteFile}, {"CreateFileW", WINAPI_CreateFileW}, {"CloseHandle", WINAPI_ret1},
    {"GetStdHandle", WINAPI_ret0}, {"GetFileType", WINAPI_ret0}, {"WriteConsoleW", WINAPI_ret1},
    {"GetConsoleMode", WINAPI_ret0}, {"RaiseException", WINAPI_RaiseException},
    {"ExitProcess", WINAPI_ExitProcess}, {"TerminateProcess", WINAPI_ExitProcess},
    {"VirtualProtect", WINAPI_ret1}, {"GetStartupInfoW", WINAPI_nop}, {"VerSetConditionMask", WINAPI_ret0},
    {"VerifyVersionInfoW", WINAPI_ret1}, {"RtlLookupFunctionEntry", WINAPI_ret0}, {"RtlPcToFileHeader", WINAPI_ret0},
    {"InterlockedPushEntrySList", WINAPI_ret0}, {"InterlockedFlushSList", WINAPI_ret0},
    {"InterlockedPopEntrySList", WINAPI_ret0}, {"QueryDepthSList", WINAPI_ret0},
    {"GetCurrentThread", WINAPI_ret1}, {"FlushFileBuffers", WINAPI_ret1}, {"SetStdHandle", WINAPI_ret1},
    {"GetACP", WINAPI_ret0}, {"IsValidCodePage", WINAPI_ret1}, {"GetOEMCP", WINAPI_ret0},
    {"GetCommandLineA", WINAPI_ret0}, {"GetCommandLineW", WINAPI_ret0},
    {"GetEnvironmentStringsW", WINAPI_ret0}, {"FreeEnvironmentStringsW", WINAPI_ret1},
    {"DisableThreadLibraryCalls", WINAPI_ret1}, {"SwitchToThread", WINAPI_ret0},
    {NULL, NULL}
};

/* ---- log hooks ---------------------------------------------------------- */
static int g_logs;
static long hook_log(void *ctx, const char *fmt, ...) {   /* image+0xcde40: log(ctx, fmt, ...) */
    X18_SAVE;
    if (g_verbose && IN_IMAGE(fmt)) {
        char buf[1024];
        if (strchr(fmt, 'f') || strchr(fmt, 'g') || strchr(fmt, 'e')) {
            fprintf(stderr, "[dll] %s", fmt);
        } else {
            va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
            fprintf(stderr, "[dll] %s", buf);
        }
        if (!strchr(fmt, '\n')) fputc('\n', stderr);
    }
    g_logs++;
    X18_RESTORE;
    return 0;
}
static long hook_assert(const char *file, const char *func, uint32_t line) {   /* image+0xcae560 */
    X18_SAVE;
    fprintf(stderr, "[dll assert/log2] caller image+%#lx x0=%p x1=%p w2=%u\n", (unsigned long)((uint8_t *)__builtin_return_address(0) - g_base), file, func, line);
    X18_RESTORE;
    return 0;
}

static void patch_jump(uint32_t rva, void *target) {
    uint32_t *p = IMG(rva);
    p[0] = 0x58000050;   /* ldr x16, #8 */
    p[1] = 0xd61f0200;   /* br x16 */
    memcpy(&p[2], &target, 8);
}

/* ---- loading ------------------------------------------------------------ */
static void *load_dll(const char *path, void *(*lookup_hook)(const char *)) {
    int fd = open(path, O_RDONLY); if (fd < 0) { perror(path); exit(1); }
    struct stat st; if (fstat(fd, &st) || st.st_size <= 0) { fprintf(stderr, "empty or unreadable DLL\n"); exit(2); }
    uint8_t *file = mmap(NULL, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0); close(fd);
    if (file == MAP_FAILED) { perror("mmap DLL"); exit(2); }
    unsigned char digest[EVP_MAX_MD_SIZE]; unsigned digest_size = 0;
    const unsigned char expected[32] = {
        0xc2,0x41,0xb7,0xfb,0xb2,0xec,0x54,0xe4,0x39,0x75,0x2a,0x1e,0xa7,0xad,0x25,0xda,
        0x10,0xca,0x74,0x00,0x12,0xa5,0x4b,0xd0,0xe7,0xa8,0x7e,0xa9,0x4a,0x14,0x1c,0x35
    };
    if (!EVP_Digest(file, st.st_size, digest, &digest_size, EVP_sha256(), NULL) ||
        digest_size != 32 || memcmp(digest, expected, 32)) {
        fprintf(stderr, "unsupported camera DLL SHA-256; no code was loaded\n"); exit(2);
    }
    struct dos_hdr *dos = (void *)file;
    struct file_hdr *fh = (void *)(file + dos->lfanew + 4);
    struct opt_hdr64 *oh = (void *)(fh + 1);
    struct sec_hdr *sh = (void *)((uint8_t *)oh + fh->optsz);
    if (fh->machine != 0xaa64 || oh->magic != 0x20b) { fprintf(stderr, "not an ARM64 PE32+\n"); exit(1); }
    g_prefbase = oh->imagebase; g_sizeimage = oh->sizeimage;
    g_base = mmap((void *)g_prefbase, g_sizeimage, PROT_READ | PROT_WRITE | PROT_EXEC,
                  MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (g_base == MAP_FAILED || (uint64_t)g_base != g_prefbase) {
        g_base = mmap(NULL, g_sizeimage, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (g_base == MAP_FAILED) { perror("mmap"); exit(1); }
    }
    memcpy(g_base, file, oh->sizehdrs);
    for (int i = 0; i < fh->nsec; i++)
        if (sh[i].rawsize) memcpy(g_base + sh[i].va, file + sh[i].rawptr, sh[i].rawsize);
    int64_t delta = (int64_t)((uint64_t)g_base - g_prefbase);
    if (delta) {   /* IMAGE_REL_BASED_DIR64 only */
        uint32_t off = 0; struct data_dir d = oh->dir[5];
        while (off < d.size) {
            uint32_t *blk = IMG(d.va + off); uint16_t *ent = (uint16_t *)(blk + 2);
            for (uint32_t k = 0; k < (blk[1] - 8) / 2; k++)
                if ((ent[k] >> 12) == 10) *(uint64_t *)IMG(blk[0] + (ent[k] & 0xfff)) += delta;
            off += blk[1];
        }
    }
    if (g_verbose) fprintf(stderr, "image at %p (preferred %#lx, delta %ld), %u bytes\n", g_base, (unsigned long)g_prefbase, (long)delta, g_sizeimage);
    /* imports */
    struct data_dir id = oh->dir[1]; int nunk = 0;
    for (struct imp_desc *desc = IMG(id.va); desc->name; desc++) {
        const char *dll = IMG(desc->name);
        uint64_t *oft = IMG(desc->oft ? desc->oft : desc->ft), *ft = IMG(desc->ft);
        for (int k = 0; oft[k]; k++) {
            const char *name = (oft[k] >> 63) ? "<ordinal>" : (const char *)IMG((oft[k] & 0x7fffffff) + 2);
            void *fn = NULL;
            if (!strcasecmp(dll, "KERNEL32.dll"))
                for (const struct stub *s = g_stubs; s->name; s++) if (!strcmp(s->name, name)) { fn = s->fn; break; }
            if (!fn && lookup_hook) fn = lookup_hook(name);
            if (!fn) { fn = unknown_import; nunk++; }
            ft[k] = (uint64_t)fn;
        }
    }
    /* TLS: fake TEB with a TLS pointer array, index 0 -> a copy of the TLS template */
    g_teb = calloc(1, 0x10000);
    if (oh->dir[9].size) {
        struct tls_dir64 *tls = IMG(oh->dir[9].va);
        size_t n = (tls->rawend - tls->rawstart) + tls->zerofill;
        void *blk = calloc(1, n + 64);
        memcpy(blk, g_base + (tls->rawstart - g_prefbase), tls->rawend - tls->rawstart);
        void **arr = calloc(64, sizeof(void *)); arr[0] = blk;
        *(void **)(g_teb + 0x58) = arr;
        *(uint32_t *)(g_base + (tls->indexaddr - g_prefbase)) = 0;
    }
    munmap(file, st.st_size);
    if (g_verbose) fprintf(stderr, "imports resolved (%d unknown -> trap)\n", nunk);
    return g_base;
}

static void *export_addr(const char *name) {
    struct dos_hdr *dos = (void *)g_base; struct file_hdr *fh = (void *)(g_base + dos->lfanew + 4);
    struct opt_hdr64 *oh = (void *)(fh + 1); struct exp_dir *ed = IMG(oh->dir[0].va);
    uint32_t *names = IMG(ed->names), *funcs = IMG(ed->funcs); uint16_t *ords = IMG(ed->ords);
    for (uint32_t i = 0; i < ed->nnames; i++)
        if (!strcmp(IMG(names[i]), name)) return IMG(funcs[ords[i]]);
    return NULL;
}

/* ---- striping library API (camx ipeStripingLib.h) ----------------------- */
typedef int32_t (*sl_alloc_t)(void **ptr, size_t size);
typedef int32_t (*sl_free_t)(void **ptr);
struct sl_memop { sl_alloc_t alloc; sl_free_t free; };
struct sl_zoom { uint32_t fullWidth, fullHeight; int32_t windowLeft, windowTop; uint32_t windowWidth, windowHeight; };
#pragma pack(push, 4)
struct sl_frame { uint32_t valid; const void *iq, *ica1, *ica2; const struct sl_zoom *zoom, *prevZoom; };   /* 44 B, one per batch frame */
struct sl_exec { const void *iq, *ica1, *ica2; const struct sl_zoom *zoom, *prevZoom; uint32_t maxNumOfCoresToUse; const void *configIO; struct sl_frame frames[3]; uint8_t pad[256]; };   /* extraPassParams[3] at +0x34 (fw DWARF _IPEStripingLibExecuteParams) */
#pragma pack(pop)
typedef const char *(*fn_version_t)(void);
typedef int32_t (*fn_create_t)(const void *cfg, struct sl_memop *mem, uint32_t titan, uint32_t hw, void **handle, uint32_t *bufsize, uint32_t *cdmsize);
typedef int32_t (*fn_scratch_t)(void *cfg, uint32_t *scratch);
typedef int32_t (*fn_exec_t)(void *handle, const struct sl_exec *exe, void *out, void *meta, void *userargs);
typedef int32_t (*fn_destroy_t)(void **handle);

static int32_t sl_alloc(void **ptr, size_t size) { X18_SAVE; *ptr = calloc(1, size ? size : 1); if (g_verbose > 1) fprintf(stderr, "  alloc %zu -> %p\n", size, *ptr); X18_RESTORE; return *ptr ? 0 : 1; }
static int32_t sl_free(void **ptr) { X18_SAVE; free(*ptr); *ptr = NULL; X18_RESTORE; return 0; }

static void *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb"); if (!f) { perror(path); exit(1); }
    fseek(f, 0, SEEK_END); *len = ftell(f); fseek(f, 0, SEEK_SET);
    void *p = calloc(1, *len + 4096); if (fread(p, 1, *len, f) != *len) { perror("read"); exit(1); }
    fclose(f); return p;
}

static void hook_nclog(const char *fmt, ...) {
    if (!g_verbose || !fmt || getenv("SLRUN_NO_NCLOG")) return;
    /* integer/pointer varargs sit in x1..x7 under both conventions; doubles do not, so only expand float-free formats */
    int floats = 0;
    for (const char *c = fmt; *c; c++) if (c[0] == '%' && strpbrk(c + 1, "fgeEGaA") && strchr("fgeEGaAdiuxXsclp%", c[1 + strspn(c + 1, "0123456789.-+ #lhzjt")]) && strchr("fgeEGaA", c[1 + strspn(c + 1, "0123456789.-+ #lhzjt")])) floats = 1;
    fputs("nclib: ", stderr);
    if (floats) fputs(fmt, stderr); else { va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); }
    if (!fmt[0] || fmt[strlen(fmt) - 1] != '\n') fputc('\n', stderr);
}

static void hook_invalid_parameter(void) {
    void **fp = __builtin_frame_address(0);
    fprintf(stderr, "CRT _invalid_parameter called from image+%#lx; frame chain:", (unsigned long)((uint8_t *)__builtin_return_address(0) - g_base));
    for (int i = 0; i < 8 && fp && fp[1]; i++) { fprintf(stderr, " %#lx", (unsigned long)((uint8_t *)fp[1] - g_base)); fp = (void **)fp[0]; }
    fprintf(stderr, "\n"); exit(9);
}

/* AWB oracle only: the initializer requests this one decoded tuning module.
 * Logging/lookup glue is replaced; all reference geometry and CCT arithmetic
 * remain in the DLL. The synthetic controller has no per-device calibration. */
static uint8_t *g_awb_dist_module;
static void awb_no_log(void) {}
static void *awb_get_module(void *manager, const char *name, void *selector, unsigned mode) {
    (void)manager; (void)selector; (void)mode;
    if (strcmp(name, "SFDistWVV1")) { fprintf(stderr, "unexpected AWB module %s\n", name); exit(8); }
    return g_awb_dist_module;
}

static void *awb_dist_nodes(const uint8_t **p, const uint8_t *end, unsigned n, unsigned depth) {
    if (!n || n > (depth == 0 ? 20u : depth == 1 ? 30u : 5u)) return NULL;
    size_t stride = depth == 2 ? 12 : 24;
    uint8_t *nodes = calloc(n, stride);
    for (unsigned i = 0; i < n; i++) {
        if (end - *p < 12) return NULL;
        float a, b; memcpy(&a, *p, 4); memcpy(&b, *p + 4, 4);
        if (!isfinite(a) || !isfinite(b) || a > b) return NULL;
        memcpy(nodes + i * stride, *p, 12); *p += 12;
        if (depth == 2) {
            float weight; memcpy(&weight, nodes + i * stride + 8, 4);
            if (!isfinite(weight) || weight < 0) return NULL;
        } else {
            unsigned count; memcpy(&count, nodes + i * stride + 8, 4);
            void *child = awb_dist_nodes(p, end, count, depth + 1);
            if (!child) return NULL;
            memcpy(nodes + i * stride + 16, &child, sizeof(child));
        }
    }
    return nodes;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <dll> version | run <cfg> <iq> <titan> <hw> <out> [cores] [w h]\n", argv[0]); return 2; }
    setvbuf(stdout, NULL, _IONBF, 0);
    if (getenv("SLRUN_QUIET")) g_verbose = 0;
    if (getenv("SLRUN_DEBUG")) g_verbose = 2;
    load_dll(argv[1], NULL);
    /* __chkstk: ldr x17, [x18, #0x10] -> mov x17, xzr (never probe) */
    uint32_t *chk = IMG(0x1440);
    if (*chk == 0xf9400a51) *chk = 0xaa1f03f1; else fprintf(stderr, "warning: __chkstk not where expected (%#x)\n", *chk);
    /* CRT LSE-atomics probe (image+0x13f0, reached via __acrt_getptd from CRT math): `mrs x16, CurrentEL` is privileged
     * and SIGILLs in Linux EL0. Replace with `mov x16, #1; b +0x18` -> reports "LSE available" and skips the probe. */
    uint32_t *lse = (uint32_t *)(g_base + 0x1400);
    if (lse[0] == 0xd5384250) { lse[0] = 0xd2800030; lse[1] = 0x14000006; } else fprintf(stderr, "warning: LSE probe not where expected (%#x)\n", lse[0]);
    /* The DLL's statically linked CRT frexp (0xcc7098) sends Inf/NaN through _invoke_matherr -> __acrt_getptd, which needs
     * the CRT per-thread state we never initialised (DllMain is not run). NcLib ANR computes 1/(a-b) on equal LUT knots, so
     * route frexp to the host libm, and make _invalid_parameter (0xcaea60) report instead of jumping through a garbage pointer. */
    patch_jump(0xcc7098, (void *)frexp);
    patch_jump(0xcaea60, (void *)hook_invalid_parameter);
    /* NcLib's variadic logger (0x7b038) formats through the CRT vsnprintf -> ptd -> crash; print the format skeleton instead
     * (Windows ARM64 passes variadic doubles in x registers, so we do not expand the arguments). */
    patch_jump(0x7b038, (void *)hook_nclog);
    if (!getenv("SLRUN_NOHOOK")) { patch_jump(0xcde40, hook_log); patch_jump(0xcae560, hook_assert); }
    __builtin___clear_cache((char *)g_base, (char *)g_base + g_sizeimage);
    set_x18(g_teb);

    fn_version_t fver = export_addr("GetStripingLibraryVersion");
    printf("GetStripingLibraryVersion: %s\n", fver());
    if (!strcmp(argv[2], "version")) return 0;

    if (!strcmp(argv[2], "exposure-index")) {
        /* Positive exposure inputs only. Run the DLL's scale initializer and
         * exposure helper unchanged: log(round(gain*time*correction)/reference)
         * times 1/log10f(1.03f), with the helper's float conversions. */
        if (argc != 5) {
            fprintf(stderr, "exposure-index needs <rows: gain-f32 correction-f32 time-u64 reference-u64> <indices-f32>\n");
            return 2;
        }
        size_t n;
        uint8_t *rows = read_file(argv[3], &n);
        if (!n || n % 24 || n > 24 * 100000) return 2;
        float *indices = calloc(n / 24, 4);
        ((void (*)(void))IMG(0x16d0))();
        for (size_t i = 0; i < n / 24; i++) {
            float gain, correction; uint64_t time_ns, reference;
            memcpy(&gain, rows + i * 24, 4); memcpy(&correction, rows + i * 24 + 4, 4);
            memcpy(&time_ns, rows + i * 24 + 8, 8); memcpy(&reference, rows + i * 24 + 16, 8);
            if (!isfinite(gain) || gain <= 0 || gain > 256 || !isfinite(correction) ||
                correction <= 0 || correction > 16 || !time_ns || time_ns > 10000000000ULL ||
                !reference || reference > 10000000000ULL) return 2;
            uint8_t input[24 + 64] = {0};
            memset(input + 24, 0xa5, 64);
            memcpy(input, &gain, 4); memcpy(input + 8, &time_ns, 8);
            memcpy(input + 0x14, &correction, 4);
            ((void (*)(void *, uint64_t))IMG(0x3c1878))(input, reference);
            memcpy(indices + i, input + 0x10, 4);
            for (unsigned k = 24; k < sizeof(input); k++) if (input[k] != 0xa5) return 4;
            if (!isfinite(indices[i])) return 4;
        }
        FILE *f = fopen(argv[4], "wb");
        if (!f) return 5;
        size_t written = fwrite(indices, 1, n / 6, f);
        int closed = fclose(f);
        if (written != n / 6 || closed) return 5;
        printf("exposure-index rows=%zu scale=%.9g\n", n / 24, *(float *)IMG(0x1795a10));
        return 0;
    }

    if (!strcmp(argv[2], "awb-cct")) {
        if (argc != 7) {
            fprintf(stderr, "awb-cct needs <references80> <distance-tree> <ratios8n> <output12n>\n");
            return 2;
        }
        size_t nr, nd, ni;
        uint8_t *refs = read_file(argv[3], &nr), *dist = read_file(argv[4], &nd);
        uint8_t *input = read_file(argv[5], &ni);
        if (nr != 80 || nd < 4 || !ni || ni % 8 || ni > 8 * 100000) return 2;
        for (size_t i = 0; i < nr / 4; i++) {
            float v; memcpy(&v, refs + i * 4, 4);
            if (!isfinite(v) || v <= 0 || v > 16) return 2;
        }
        for (size_t i = 0; i < ni / 4; i++) {
            float v; memcpy(&v, input + i * 4, 4);
            if (!isfinite(v) || v <= 0 || v > 16) return 2;
        }
        unsigned count; memcpy(&count, dist, 4);
        const uint8_t *cursor = dist + 4;
        void *nodes = awb_dist_nodes(&cursor, dist + nd, count, 0);
        if (!nodes || cursor != dist + nd) return 2;
        g_awb_dist_module = calloc(1, 0x160);
        memcpy(g_awb_dist_module + 0x120 + 0x28, &count, 4);
        memcpy(g_awb_dist_module + 0x120 + 0x30, &nodes, 8);
        patch_jump(0x6f39f8, (void *)awb_get_module);
        void *logger = (void *)awb_no_log;
        memcpy(IMG(0x16a4230), &logger, 8);
        void **tls = *(void ***)(g_teb + 0x58);
        ((uint8_t *)tls[0])[0x14] = 1;
        __builtin___clear_cache((char *)g_base, (char *)g_base + g_sizeimage);

        uint8_t *controller = calloc(1, 0x194da0);
        uint8_t *obj = calloc(1, 0x1d0 + 64);
        uint8_t *ref_copy = calloc(1, 80);
        uint8_t *item = calloc(1, 0x190 + 64);
        uint8_t *output = calloc(ni / 8, 12);
        memset(obj + 0x1d0, 0xa5, 64); memset(item + 0x190, 0xa5, 64);
        memcpy(controller + 0x4f4d4, refs, 80);
        memcpy(obj + 0x130, &ref_copy, 8);
        memcpy(obj + 0x138, &controller, 8);
        typedef int (*awb_method_t)(void *, void *);
        int rc = ((awb_method_t)IMG(0x6c3080))(obj, g_awb_dist_module);
        if (rc || memcmp(ref_copy, refs, 80)) return 3;
        for (size_t i = 0; i < ni / 8; i++) {
            memset(item, 0, 0x190);
            memcpy(item + 0x2c, input + i * 8, 8);
            rc = ((awb_method_t)IMG(0x6c3bb0))(obj, item);
            if (rc) return 3;
            float distance, cct; unsigned decision;
            memcpy(&distance, item + 0x34, 4); memcpy(&cct, item + 0x38, 4);
            memcpy(&decision, item + 0x3c, 4);
            if (!isfinite(distance) || !isfinite(cct) || cct < 1500 || cct > 12000 || decision > 9) return 3;
            memcpy(output + i * 12, item + 0x34, 12);
        }
        for (unsigned i = 0; i < 64; i++)
            if (obj[0x1d0 + i] != 0xa5 || item[0x190 + i] != 0xa5) return 4;
        FILE *f = fopen(argv[6], "wb");
        if (!f) return 5;
        size_t n = ni / 8 * 12, written = fwrite(output, 1, n, f);
        int closed = fclose(f);
        printf("awb-cct points=%zu rc=0 (reference calibration only)\n", ni / 8);
        return written == n && !closed ? 0 : 5;
    }

    if (!strcmp(argv[2], "tone-tree")) {
        /* Validation oracle: use the DLL's complete generic tree walker and
         * interpolation engine, independently of the Python leaf selection.
         * Fixture: u32 branches/leaves, then per branch outer+HDR intervals,
         * followed by each lux interval and its region bytes. One HDR child. */
        if (argc != 9) return 2;
        size_t size = 0, len;
        uintptr_t callback = 0, ops = 0;
        if (!strcmp(argv[3], "tmc")) { size = 368; callback = 0x924440; ops = 0x167dc10; }
        if (!strcmp(argv[3], "ltm")) { size = 716; callback = 0x8f9030; ops = 0x167d378; }
        if (!size) return 2;
        uint8_t *fixture = read_file(argv[4], &len);
        if (len < 8) return 2;
        unsigned branches = *(uint32_t *)fixture, leaves = *(uint32_t *)(fixture + 4);
        if (branches != 2 || leaves != (!strcmp(argv[3], "tmc") ? 3 : 4) ||
            len != 8 + branches * (16 + leaves * (8 + size))) return 2;
        float values[3], keys[3] = {2, 5, 0};
        for (int i = 0; i < 3; i++) {
            char *end;
            values[i] = strtof(argv[5+i], &end);
            if (end == argv[5+i] || *end || !isfinite(values[i])) return 2;
        }
        uint8_t *core = calloc(1, 16), *outer = calloc(branches, 48);
        *(uint32_t *)core = branches; *(void **)(core + 8) = outer;
        size_t offset = 8;
        for (unsigned i = 0; i < branches; i++) {
            uint8_t *hdr = calloc(1, 48), *lux = calloc(leaves, 48);
            memcpy(outer + i*48 + 8, fixture + offset, 8);
            memcpy(hdr + 8, fixture + offset + 8, 8); offset += 16;
            *(uint32_t *)(outer + i*48 + 16) = 1;
            *(void **)(outer + i*48 + 24) = hdr;
            *(uint32_t *)(hdr + 16) = leaves; *(void **)(hdr + 24) = lux;
            for (unsigned j = 0; j < leaves; j++) {
                memcpy(lux + j*48 + 8, fixture + offset, 8); offset += 8;
                *(void **)(lux + j*48 + 40) = fixture + offset; offset += size;
            }
        }
        float triggers[3][2];
        void *vectors[3][3];
        for (unsigned i = 0; i < 3; i++) {
            triggers[i][0] = keys[i]; triggers[i][1] = values[i];
            vectors[i][0] = triggers[i]; vectors[i][1] = vectors[i][2] = triggers[i] + 2;
        }
        void *trigger_vector[3] = {vectors, vectors + 3, vectors + 3};
        uint8_t *nodes = calloc(1, 15*72 + 64), *output = calloc(1, 7*size + 64);
        memset(nodes + 15*72, 0xa5, 64); memset(output + 7*size, 0xa5, 64);
        for (unsigned i = 0; i < 7; i++) *(void **)(nodes + i*72 + 56) = output + i*size;
        *(uint32_t *)nodes = 1; *(uint32_t *)(nodes + 64) = 1;
        *(void **)(nodes + 48) = core;
        typedef int (*setup_tree_t)(void *, unsigned, void *, void *);
        typedef int (*blend_tree_t)(void *, unsigned, unsigned, void *);
        int rc = ((setup_tree_t)IMG(0x9a34e8))(nodes, 4, trigger_vector, IMG(ops));
        if (rc != 1) return 3;
        rc = ((blend_tree_t)IMG(0x9a3d08))(nodes, 7, 4, IMG(callback));
        if (rc != 1) return 3;
        for (unsigned i = 0; i < 64; i++)
            if (nodes[15*72+i] != 0xa5 || output[7*size+i] != 0xa5) return 4;
        FILE *f = fopen(argv[8], "wb");
        if (!f) return 5;
        size_t written = fwrite(*(void **)(nodes + 56), 1, size, f);
        int closed = fclose(f);
        return written == size && !closed ? 0 : 5;
    }

    if (!strcmp(argv[2], "interp")) {
        /* Windows DoInterpolation callbacks from the IQ operation table.
         * Preserve their nearest-neighbour rules, integer tables and pass IDs. */
        if (argc != 8) {
            fprintf(stderr, "interp needs <anr|tf|asf|ltm|tmc> <left> <right> <ratio> <out>\n");
            return 2;
        }
        uintptr_t rva = 0; size_t size = 0;
        if (!strcmp(argv[3], "anr")) { rva = 0x96cfd0; size = 8640; }
        if (!strcmp(argv[3], "tf")) { rva = 0x9513d0; size = 4688; }
        if (!strcmp(argv[3], "asf")) { rva = 0x944ac0; size = 4036; }
        if (!strcmp(argv[3], "ltm")) { rva = 0x8f9030; size = 716; }
        if (!strcmp(argv[3], "tmc")) { rva = 0x924440; size = 368; }
        char *end;
        float ratio = strtof(argv[6], &end);
        if (!size || end == argv[6] || *end || !isfinite(ratio) || ratio < 0 || ratio > 1) return 2;
        size_t na, nb;
        void *a = read_file(argv[4], &na), *b = read_file(argv[5], &nb);
        if (na != size || nb != size) { fprintf(stderr, "unexpected interpolation input size\n"); return 2; }
        uint8_t *out = malloc(size + 64);
        memset(out, 0xa5, size + 64);
        typedef int (*interpolate_t)(void *, void *, float, void *);
        int rc = ((interpolate_t)(g_base + rva))(a, b, ratio, out);
        printf("interp %s ratio=%.9g rc=%d bytes=%zu\n", argv[3], ratio, rc, size);
        if (rc != 1) return 3;
        for (size_t i = size; i < size + 64; i++) if (out[i] != 0xa5) return 4;
        FILE *f = fopen(argv[7], "wb");
        if (!f) { perror(argv[7]); return 5; }
        size_t written = fwrite(out, 1, size, f);
        int closed = fclose(f);
        return written == size && !closed ? 0 : 5;
    }

    if (!strcmp(argv[2], "tmc141") || !strcmp(argv[2], "tmc141-hist")) {
        /* TMC141 anchor path, optionally with histogram preprocessing and
         * contrast curves. Layout traced from RunInterpolation 0x923b90.
         * Explicit DRC/extra controls: this is not a calibrated AE implementation. */
        int histogram_mode = !strcmp(argv[2], "tmc141-hist");
        if (argc != (histogram_mode ? 11 : 8)) {
            fprintf(stderr, "tmc141 needs <region368> <descriptor84> <drcGain> <drcGainDark> <adrc.out>\n"
                            "tmc141-hist needs <region368> <descriptor84> <drcGain> <drcGainDark> <hist1024u32> <gamma257f> <previous-adrc|-> <adrc.out>\n");
            return 2;
        }
        size_t rn, dn;
        uint8_t *region = read_file(argv[3], &rn), *descriptor = read_file(argv[4], &dn);
        char *end1, *end2;
        float gain = strtof(argv[5], &end1), dark = strtof(argv[6], &end2);
        if (rn != 368 || dn != 84 || end1 == argv[5] || *end1 || end2 == argv[6] || *end2 ||
            !isfinite(gain) || !isfinite(dark) || gain < 1 || gain > 16 || dark < 1 || dark > 16) return 2;
        uint8_t *in = calloc(1, 0x600), *chrom = calloc(1, 0x200), *aux = calloc(1, 0x90);
        uint8_t *adrc = calloc(1, 0x8260 + 64);
        memset(adrc + 0x8260, 0xa5, 64);
        *(void **)in = chrom;
        *(uint32_t *)(in + 8) = 0x60800;
        *(float *)(in + 0xc) = gain;
        *(float *)(in + 0x18) = dark;
        *(void **)(in + 0x40) = adrc;
        /* Neutral experimental defaults for the extra frame controls. Their
         * AE source is not yet mapped; do not label this statistics-adaptive. */
        *(float *)(in + 0x480) = 1;
        *(float *)(in + 0x484) = 1;
        *(float *)(in + 0x48c) = 50;
        *(float *)(in + 0x490) = 1;
        *(uint32_t *)(chrom + 8) = 1;
        memcpy(chrom + 0x54, descriptor + 0x28, 0x24);
        unsigned table_offsets[] = {0x10a4, 0x50cc, 0x50e8, 0x5104, 0x5120, 0x513c, 0x5158,
                                    0x5174, 0x51b0, 0x51ec, 0x5228, 0x6228, 0x7228, 0x20a4};
        for (unsigned i = 0; i < sizeof(table_offsets) / sizeof(table_offsets[0]); i++)
            *(void **)(aux + 0x10 + i * 8) = adrc + table_offsets[i];
        *(uint32_t *)(aux + 0x80) = 1024;
        memcpy(adrc + 0x822c, chrom + 0x68, 16);
        *(uint32_t *)(adrc + 0x8254) = (*(uint32_t *)(chrom + 0x5c) >> 2) == 1;
        typedef int (*tmc_anchor_t)(void *, void *, void *);
        if (histogram_mode) {
            size_t hn, gn, pn;
            uint32_t *hist = (uint32_t *)read_file(argv[7], &hn);
            float *gamma = (float *)read_file(argv[8], &gn);
            if (hn != 4096 || gn != 1028) return 2;
            uint64_t total = 0;
            for (unsigned i = 0; i < 1024; i++) total += hist[i];
            if (!total || total > 100000000) return 2;
            for (unsigned i = 0; i < 257; i++)
                if (!isfinite(gamma[i]) || gamma[i] < 0 || gamma[i] > 1024 ||
                    (i && gamma[i] <= gamma[i - 1])) return 2;
            *(void **)(in + 0x38) = hist;
            memcpy(in + 0x48, gamma, gn);
            /* History is valid only within an unchanged exposure context.
             * Lux/current-previous exposure indices remain equal at zero. */
            if (strcmp(argv[9], "-")) {
                uint8_t *prev = read_file(argv[9], &pn);
                if (pn != 0x8260 || *(uint32_t *)(prev + 8) != 5) return 2;
                for (unsigned off = 0x10a4; off < 0x30a4; off += 4)
                    if (!isfinite(*(float *)(prev + off)) || *(float *)(prev + off) < 0) return 2;
                if (*(float *)(prev + 0x30a0) <= 0) return 2;
                *(void **)(in + 0x20) = prev + 0x10a4;
                *(void **)(in + 0x28) = prev + 0x20a4;
                *(uint32_t *)(in + 0x34) = 1;
                memcpy(adrc + 0x30a4, prev + 0x10a4, 4096);
                memcpy(adrc + 0x40a4, prev + 0x20a4, 4096);
            }
            if (((tmc_anchor_t)(g_base + 0x925160))(region, in, aux) != 1) return 3;
        } else {
            /* These are histogram weights, disabled in the no-statistics path. */
            memset(region + 0xfc, 0, 28);
            memset(region + 0x120, 0, 16);
        }
        int rc = ((tmc_anchor_t)(g_base + 0x9255f0))(region, in, aux);
        printf("tmc141 anchor rc=%d gain=%g dark=%g histogram=%d\n", rc, gain, dark, histogram_mode);
        if (rc != 1) return 3;
        *(uint32_t *)adrc = 1;
        *(uint32_t *)(adrc + 8) = 5;
        memcpy(adrc + 0x10, chrom + 0x54, 8);
        *(float *)(adrc + 0x50) = *(float *)(aux + 4);
        *(float *)(adrc + 0x54) = 1;
        *(float *)(adrc + 0x58) = 1;
        *(uint32_t *)(adrc + 0x74) = *(uint32_t *)(chrom + 0x5c) & 3;
        if (histogram_mode && !*(uint32_t *)(adrc + 0x8230) &&
            ((tmc_anchor_t)(g_base + 0x9279c0))(region, in, aux) != 1) return 3;
        /* Retain the RunInterpolation bookkeeping too. */
        unsigned region_offsets[] = {0, 4, 0x10, 0x14, 0x120, 0x124, 0x128, 0x12c};
        for (unsigned i = 0; i < 8; i++)
            memcpy(adrc + 0x50ac + i * 4, region + region_offsets[i], 4);
        memcpy(adrc + 0x109c, region + 0x120, 4);
        memcpy(adrc + 0x10a0, region + 0x128, 4);
        memcpy(adrc + 0x823c, region + 0x20, 4);
        memcpy(adrc + 0x8240, aux, 4);
        for (unsigned i = 0; i < 64; i++) if (adrc[0x8260 + i] != 0xa5) return 4;
        for (unsigned offset = 0x50cc; offset < 0x5228; offset += 4)
            if (!isfinite(*(float *)(adrc + offset))) { fprintf(stderr, "nonfinite TMC curve at %#x\n", offset); return 4; }
        for (unsigned curve = 0; curve < 3; curve++) {
            float *x = (float *)(adrc + 0x50cc + curve * 56), *y = x + 7;
            for (unsigned i = 0; i < 7; i++) {
                if (x[i] < 0 || x[i] > 1 || y[i] < 0 || y[i] > 1 ||
                    (i && (x[i] <= x[i - 1] || y[i] < y[i - 1]))) {
                    fprintf(stderr, "invalid TMC knots in curve %u at %u\n", curve, i); return 4;
                }
            }
        }
        if (histogram_mode) {
            for (unsigned off = 0x10a4; off < 0x30a4; off += 4)
                if (!isfinite(*(float *)(adrc + off)) || *(float *)(adrc + off) < 0) return 4;
            for (unsigned off = 0x20a8; off < 0x30a4; off += 4)
                if (*(float *)(adrc + off) < *(float *)(adrc + off - 4)) return 4;
            for (unsigned off = 0x5228; off < 0x8228; off += 4)
                if (!isfinite(*(float *)(adrc + off)) || *(float *)(adrc + off) < 0 ||
                    *(float *)(adrc + off) > 1) return 4;
        }
        const char *output = argv[histogram_mode ? 10 : 7];
        FILE *f = fopen(output, "wb");
        if (!f) { perror(output); return 5; }
        size_t written = fwrite(adrc, 1, 0x8260, f);
        int closed = fclose(f);
        return written == 0x8260 && !closed ? 0 : 5;
    }

    if (!strcmp(argv[2], "ltm161")) {
        /* Lab oracle: LTM161Setting, Titan680 packer and firmware IQ writer.
         * Output LUT pointers are caller-owned; save them in hardware selector
         * order (weight, LA, curve, scale, mask, LCE+, LCE-). Optional TMC141
         * ADRC data follows the Windows layout; omitted keeps the base path. */
        if (argc != 10 && argc != 11) {
            fprintf(stderr, "ltm161 needs <region> <reserve> <w> <h> <unpacked.out> <regs.out> <luts.out> [adrc]\n");
            return 2;
        }
        size_t rn, sn;
        void *region = read_file(argv[3], &rn), *reserve = read_file(argv[4], &sn);
        if (rn != 716 || sn != 720) { fprintf(stderr, "unexpected LTM region/reserve size\n"); return 2; }
        uint8_t *in = calloc(1, 0x1000), *en = calloc(1, 0x100), *unp = calloc(1, 0x3000);
        uint8_t *regs = calloc(1, 0x100), *module = calloc(1, 0x100), *obj = calloc(1, 0x100);
        uint32_t w = strtoul(argv[5], NULL, 0), h = strtoul(argv[6], NULL, 0);
        *(uint32_t *)(in + 0x10) = w; *(uint32_t *)(in + 0x14) = h;
        *(uint16_t *)(in + 0x152) = w - 1; *(uint16_t *)(in + 0x156) = h - 1;
        *(uint32_t *)(en + 4) = 1;
        if (argc == 11) {
            size_t an;
            void *adrc = read_file(argv[10], &an);
            if (an != 0x8260) { fprintf(stderr, "unexpected ADRC size\n"); return 2; }
            *(void **)(in + 0x140) = adrc;
        }
        unsigned ptr_off[] = {0x48, 0x68, 0x88, 0x98, 0x78, 0xa8, 0xb8};
        unsigned sizes[] = {96, 256, 256, 512, 256, 64, 64};
        void *lut[7];
        for (int i = 0; i < 7; i++) { lut[i] = calloc(1, 0x2000); *(void **)(unp + ptr_off[i]) = lut[i]; }
        if (getenv("SLRUN_TRAP")) raise(SIGTRAP);
        typedef int (*ltm_calc_t)(void *, void *, void *, void *, void *);
        int rc = ((ltm_calc_t)(g_base + 0x9dc1a0))(in, region, reserve, en, unp);
        printf("ltm161 calc rc=%d\n", rc);
        if (rc != 1) return 3;
        *(void **)(obj + 0x28) = regs; *(void **)(obj + 0x30) = module;
        typedef int (*ltm_pack_t)(void *, void *, void *);
        rc = ((ltm_pack_t)(g_base + 0xc2f060))(obj, unp, lut);
        printf("ltm161 pack rc=%d module=%#x\n", rc, *(uint32_t *)module);
        if (rc) return 4;
        uint8_t *iq = calloc(1, 0x1834);
        typedef int (*ltm_setup_t)(void *, void *);
        rc = ((ltm_setup_t)(g_base + 0xc2f440))(obj, iq);
        printf("ltm161 setup rc=%d\n", rc);
        if (rc) return 5;
        FILE *f = fopen(argv[7], "wb"); fwrite(unp, 1, 0x3000, f); fclose(f);
        f = fopen(argv[8], "wb"); fwrite(regs, 1, 0x48, f); fwrite(iq + 0x1238, 1, 104, f); fclose(f);
        f = fopen(argv[9], "wb"); for (int i = 0; i < 7; i++) fwrite(lut[i], 1, sizes[i], f); fclose(f);
        return 0;
    }

    if (!strcmp(argv[2], "asf34")) {
        /* slrun <dll> asf34 <region.bin 4036 B> <unpacked.out> <regs.out> [sharpness] [nonzero] [width] [height]
         * Calls the DLL's ASF 3.4 setting function (fn 0x9c4380: pInput, pRegion, pModuleEnable, pReserve, pUnpacked)
         * and the Titan680 packer (fn 0xc45210: this (with [this+0x20] = reg struct), pUnpacked). */
        size_t rlen; void *region = read_file(argv[3], &rlen);
        uint8_t *in = calloc(1, 0x400), *en = calloc(1, 0x100), *rsv = calloc(1, 0x100), *unp = calloc(1, 0x2000), *regs = calloc(1, 0x400);
        float sharp = argc > 6 ? strtof(argv[6], NULL) : 1.0f; int nz = argc > 7 ? atoi(argv[7]) : 1;
        for (int i = 0; i < 8; i++) in[0x61 + i] = nz;
        memcpy(in + SHARP_OFF, &sharp, 4);
        *(uint32_t *)(in + 0x44) = argc > 8 ? strtoul(argv[8], NULL, 0) : 1920;
        *(uint32_t *)(in + 0x48) = argc > 9 ? strtoul(argv[9], NULL, 0) : 1080;   /* frame size for radial geometry */
        *(uint32_t *)(en + 0x18) = 1; *(uint32_t *)(en + 0x1c) = 1;   /* layer 1, layer 2 */
        rsv[4] = 1;                                                      /* asf enable */
        typedef int (*setfn_t)(void *, void *, void *, void *, void *, float);
        int rc = ((setfn_t)(g_base + 0x9c4380))(in, region, en, rsv, unp, sharp);
        printf("asf34 setting rc=%d\n", rc);
        uint8_t *obj = calloc(1, 0x100); *(void **)(obj + 0x20) = regs;
        typedef int (*packfn_t)(void *, void *);
        rc = ((packfn_t)(g_base + 0xc45210))(obj, unp);
        printf("asf34 pack rc=%d\n", rc);
        FILE *f = fopen(argv[4], "wb"); fwrite(unp, 1, 0x1400, f); fclose(f);
        f = fopen(argv[5], "wb"); fwrite(regs, 1, 0x110, f); fclose(f);
        printf("wrote %s (unpacked 0x1400) and %s (regs 0x110)\n", argv[4], argv[5]);
        return 0;
    }

    if (!strcmp(argv[2], "anr")) {
        /* slrun <dll> anr <region.bin (n x 0x870)> <reserve.bin (4 + n x 0xe4)> <npasses> <w> <h> <bits> <regs.out> <fw.out> <chromatix.out>
         * ANR101Setting::CalculateHWSetting (fn 0x999480: pInput, pUnpacked, pReserve, pModuleEnable) scales the threshold
         * tables by pInput's per-pass scale/offset floats and maps region+reserve into NcLib ANR_Chromatix (0x954/pass, kona
         * ANR_Chromatix.h layout) at pInput+0x98. ANR_FlowProcessNcLib680 (fn 0xf3d8a0: chromatix, ctx, regs, fw, n, w, h,
         * wMargin, hMargin, bits -- same signature as kona's ANR_FlowProcessNcLib480) fills 327 registers/pass (0x51c) and the
         * firmware AnrParameters (0xe0). Input offsets from IPEANR101Titan680::PackIQRegisterSetting + the setting fn. */
        if (argc < 12) { fprintf(stderr, "anr needs <region> <reserve> <n> <w> <h> <bits> <regs.out> <fw.out> <chromatix.out>\n"); return 2; }
        size_t rlen, vlen; uint8_t *region = read_file(argv[3], &rlen), *reserve = read_file(argv[4], &vlen);
        uint32_t n = strtoul(argv[5], NULL, 0), w = strtoul(argv[6], NULL, 0), h = strtoul(argv[7], NULL, 0), bits = strtoul(argv[8], NULL, 0);
        uint8_t *in = calloc(1, 0x400), *en = calloc(1, 0x40), *chrom = calloc(1, 0x954 * 4), *fw = calloc(1, 0x100), *regs = calloc(1, 0x51c * 4), *ctx = calloc(1, 0x100);
        uint32_t dims[2] = { w, h };
        *(uint32_t *)(in + 0x50) = n;              /* numPasses */
        *(uint32_t *)(in + 0x60) = bits;           /* bitWidth -> hwBitsNumber */
        *(void **)(in + 0x78) = dims;              /* pImageDimensions {widthPixels, heightLines}; +0x80 margins NULL */
        *(void **)(in + 0x88) = fw;                /* pANRParameters (firmware struct out) */
        *(void **)(in + 0x98) = chrom;             /* pNCChromatix */
        *(uint32_t *)(in + 0xa8) = n;              /* maxSupportedPassesForUsecase: region index = pass */
        for (int i = 0; i < 4; i++) {              /* per-pass threshold scale = 1 (offsets at 0xfc/0x10c/0x13c/0x14c stay 0) */
            float one = 1.0f;
            memcpy(in + 0xdc + 4 * i, &one, 4); memcpy(in + 0xec + 4 * i, &one, 4);
            memcpy(in + 0x11c + 4 * i, &one, 4); memcpy(in + 0x12c + 4 * i, &one, 4);
        }
        *(uint32_t *)en = 1; *(uint32_t *)(en + 4) = 1;   /* enable_luma/chroma_noise_reduction */
        typedef int (*calcfn_t)(void *, void *, void *, void *);
        int rc = ((calcfn_t)(g_base + 0x999480))(in, region, reserve, en);
        printf("anr calc rc=%d\n", rc);
        /* SLRUN_CHROM_SET="off=val,off=val": override NcLib chromatix fields of pass 0 before the flow (lab probes) */
        if (getenv("SLRUN_CHROM_SET")) { char *spec = strdup(getenv("SLRUN_CHROM_SET")), *tok = strtok(spec, ",");
            while (tok) { char *eq = strchr(tok, '='); if (eq) { *eq = 0; uint32_t off = strtoul(tok, NULL, 0), val = strtoul(eq + 1, NULL, 0);
                *(uint32_t *)(chrom + off) = val; printf("chromatix[%#x] = %u\n", off, val); } tok = strtok(NULL, ","); } free(spec); }
        *(int32_t *)(ctx + 0x44) = -1; *(int32_t *)(ctx + 0x48) = -1;   /* NCLIB_CONTEXT_ANR: frameNumber, fdConfig(16 words), optical centre x/y = not set */
        typedef int (*flowfn_t)(void *, void *, void *, void *, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t);
        rc = ((flowfn_t)(g_base + 0xf3d8a0))(chrom, ctx, regs, fw, n, w, h, w, h, bits);
        printf("anr flow rc=%d\n", rc);
        FILE *f = fopen(argv[9], "wb"); fwrite(regs, 1, 0x51c * n, f); fclose(f);
        f = fopen(argv[10], "wb"); fwrite(fw, 1, 0xe0, f); fclose(f);
        f = fopen(argv[11], "wb"); fwrite(chrom, 1, 0x954 * n, f); fclose(f);
        printf("wrote %s (regs %u x 0x51c), %s (fw 0xe0), %s (chromatix %u x 0x954)\n", argv[9], n, argv[10], argv[11], n);
        return 0;
    }

    if (!strcmp(argv[2], "tf")) {
        /* slrun <dll> tf <region.bin (n x 0x494)> <reserve.bin (4 + n x 0x50)> <npasses> <w> <h> <hasRef> <regs.out> <tf.out> <refine.out> <chromatix.out>
         * TF211Setting::CalculateHWSetting (fn 0x9be7b0: pInput, pUnpacked, pReserve, pModuleEnable, pOutput) maps region+reserve
         * into NcLib TF_Chromatix (0x4e0/pass = kona TF_Chromatix.h) at pInput+0x80; TF_ProcessNcLibFull680 (fn 0xf4ee90:
         * chromatix[], NCLIB_CONTEXT_TF, numPasses, transformConfidence, regs[] (0x194/pass = 101 regs), RefinementParameters,
         * TfParameters, LmcParameters). Input offsets from IPETF211Titan680::PackIQRegisterSetting. */
        if (argc < 13) { fprintf(stderr, "tf needs <region> <reserve> <n> <w> <h> <hasRef> <regs.out> <tf.out> <refine.out> <chromatix.out>\n"); return 2; }
        size_t rlen, vlen; uint8_t *region = read_file(argv[3], &rlen), *reserve = read_file(argv[4], &vlen);
        uint32_t n = strtoul(argv[5], NULL, 0), w = strtoul(argv[6], NULL, 0), h = strtoul(argv[7], NULL, 0), hasref = strtoul(argv[8], NULL, 0);
        uint8_t *in = calloc(1, 0x400), *en = calloc(1, 0x40), *chrom = calloc(1, 0x4e0 * 4), *tf = calloc(1, 0x700), *refine = calloc(1, 0x100), *lmc = calloc(1, 0x100), *regs = calloc(1, 0x194 * 4), *ctx = calloc(1, 0x100);
        *(uint32_t *)(in + 0x20) = w; *(uint32_t *)(in + 0x24) = h;   /* fullPassIcaOutputFrameWidth/Height */
        *(uint32_t *)(in + 0x30) = n;                                   /* maxUsedPasses */
        *(uint32_t *)(in + 0x40) = 256;                                 /* perspectiveConfidence (kona default, no ICA) */
        in[0x4c] = hasref; in[0x4d] = 0; in[0x4e] = 0; in[0x4f] = 0;    /* hasTFRefInput, isDigitalZoom, useCase (0 = normal), configMF none */
        *(void **)(in + 0x58) = refine; *(void **)(in + 0x60) = tf; *(void **)(in + 0x68) = lmc; *(void **)(in + 0x80) = chrom;
        *(uint32_t *)en = 1;
        typedef int (*calcfn_t)(void *, void *, void *, void *, void *);
        int rc = ((calcfn_t)(g_base + 0x9be7b0))(in, region, reserve, en, NULL);
        printf("tf calc rc=%d\n", rc);
        /* NCLIB_CONTEXT_TF as the Titan680 packer fills it: +0 mfFrameNum, +4 numOfFrames, +8 (pInput+0x98), +0xc hasRef (u8),
         * +0x10 tfConfigOption, +0x18 isDigitalZoom, +0x1c/0x20 dz start, +0x24/0x28 dz size, +0x2c preferPaddingByReflection,
         * +0x30/0x34 full-pass ICA out w/h, +0x3c (pInput+0x94), +0x40 (pInput+0x184) */
        ctx[0xc] = hasref; *(uint32_t *)(ctx + 0x30) = w; *(uint32_t *)(ctx + 0x34) = h;
        { float one = 1.0f; memcpy(ctx + 0x14, &one, 4); memcpy(in + 0x1c, &one, 4); }   /* upscalingFactorMFSR_ICA = 1 (no MFSR) */
        for (uint32_t p = 0; p < n; p++) { *(uint32_t *)(tf + p * 0x198) = 1; *(uint32_t *)(chrom + p * 0x4e0 + 0x368) = 1; }   /* TF_Chromatix.master_en (IPETF211Titan680::SetTFEnable) */   /* fw moduleCfg.EN per pass (the packer copies TF_Chromatix.en here before the flow; the flow treats 0 as bypass) */
        typedef int (*flowfn_t)(void *, void *, uint32_t, uint32_t, void *, void *, void *, void *);
        /* SLRUN_CHROM_SET="off=val,off=val": override NcLib chromatix fields of pass 0 before the flow (lab probes) */
        if (getenv("SLRUN_CHROM_SET")) { char *spec = strdup(getenv("SLRUN_CHROM_SET")), *tok = strtok(spec, ",");
            while (tok) { char *eq = strchr(tok, '='); if (eq) { *eq = 0; uint32_t off = strtoul(tok, NULL, 0), val = strtoul(eq + 1, NULL, 0);
                *(uint32_t *)(chrom + off) = val; printf("chromatix[%#x] = %u\n", off, val); } tok = strtok(NULL, ","); } free(spec); }
        if (getenv("SLRUN_TRAP")) raise(SIGTRAP);   /* gdb: stop here with the image mapped, then break on DLL functions */
        rc = ((flowfn_t)(g_base + 0xf4ee90))(chrom, ctx, n, 256, regs, refine, tf, lmc);
        printf("tf flow rc=%d\n", rc);
        FILE *f = fopen(argv[9], "wb"); fwrite(regs, 1, 0x194 * n, f); fclose(f);
        f = fopen(argv[10], "wb"); fwrite(tf, 1, 0x660, f); fclose(f);
        f = fopen(argv[11], "wb"); fwrite(refine, 1, 0x70, f); fclose(f);
        f = fopen(argv[12], "wb"); fwrite(chrom, 1, 0x4e0 * n, f); fclose(f);
        printf("wrote regs (%u x 0x194), tf (0x660), refinement (0x70), chromatix (%u x 0x4e0)\n", n, n);
        return 0;
    }

    if (!strcmp(argv[2], "dsx")) {
        /* slrun <dll> dsx <region.bin (2824 B dsx10 region)> <inW> <inH> <outW> <outH> <downscale> <dsxreg.out> <packed.out>
         * Region = dsx10_rgn_data: 706 floats in the DSX_Chromatix field order minus param_calc_mode (kona DSX_Chromatix.h:
         * luma/chroma padding en, luma pad top/bot/left/right[24], chroma pad [8]x4, luma kernel h/v[192], chroma kernel h/v[96]).
         * DSX_ProcessNcLib (fn 0xe4a6c0: DSX_Chromatix*, DS4to1_Chromatix*, ICA_Chromatix*, NCLIB_CONTEXT_DSX*, buffer, DSX_REG*)
         * then IPEDSX101Titan680::PackIQRegisterSetting (fn 0xc157f0: this{+0x20 reg struct, +0x28 enable, +0x30 ptr}, DSX_REG*, x2 != 0). */
        if (argc != 12) { fprintf(stderr, "dsx needs <region> <inW> <inH> <outW> <outH> <downscale> <dsxreg.out> <packed.out> <ds4-coefficients.bin>\n"); return 2; }
        size_t rlen; float *region = read_file(argv[3], &rlen);
        uint32_t inW = strtoul(argv[4], NULL, 0), inH = strtoul(argv[5], NULL, 0), outW = strtoul(argv[6], NULL, 0), outH = strtoul(argv[7], NULL, 0);
        float ds = strtof(argv[8], NULL);
        /* region order (DLL DSX10Setting reads the enables at float 576): kernel weights [0..575] (luma h, luma v, chroma h,
         * chroma v), enables [576,577], luma padding [578..673], chroma padding [674..705]. DSX_Chromatix order: enables,
         * luma padding, chroma padding, param_calc_mode, kernel weights. */
        int32_t *chrom = calloc(707, 4);
        for (int i = 0; i < 130; i++) chrom[i] = (int32_t)lrintf(region[576 + i]);
        chrom[130] = 1;                                                                   /* param_calc_mode (reserve; kona forces 1) */
        for (int i = 0; i < 576; i++) chrom[131 + i] = (int32_t)lrintf(region[i]);
        size_t coefficients_size;
        uint32_t *ds4to1 = read_file(argv[11], &coefficients_size);
        if (coefficients_size != 12) { fprintf(stderr, "expected three extracted DS4 coefficients\n"); return 2; }
        uint8_t *ica = calloc(1, 0x1000), *buffer = calloc(1, 0x2000), *dsxreg = calloc(1, 0xb58);
        struct { uint32_t inW, inH, outW, outH; float dsX, dsY, offX, offY; uint32_t host, pad[7]; } ctx = { inW, inH, outW, outH, ds, ds, 0.0f, 0.0f, (uint32_t)(getenv("SLRUN_DSX_HOST") ? strtoul(getenv("SLRUN_DSX_HOST"), NULL, 0) : 1), {0} };   /* DLL NCLIB_CONTEXT_DSX has a trailing 'host' (0..2) */
        typedef int (*ncfn_t)(void *, void *, void *, void *, void *, void *);
        if (getenv("SLRUN_TRAP")) raise(SIGTRAP);
        int rc = ((ncfn_t)(g_base + 0xe4a6c0))(chrom, ds4to1, ica, &ctx, buffer, dsxreg);
        printf("dsx nclib rc=%d\n", rc);
        { FILE *f0 = fopen(argv[9], "wb"); fwrite(dsxreg, 1, 0xb58, f0); fclose(f0); }   /* save before the packer (lab: it may crash) */
        uint8_t *obj = calloc(1, 0x100), *regs = calloc(1, 0x800), *aux = calloc(1, 0x100), *luts = calloc(8, 0x400);
        void *lutp[8]; for (int i = 0; i < 8; i++) lutp[i] = luts + i * 0x400;          /* x2: LUT destination pointers (DMI sel 1..8, 0x400 each) */
        if (getenv("SLRUN_TRAP2")) raise(SIGTRAP);
        *(void **)(obj + 0x20) = regs; *(uint32_t *)(obj + 0x28) = 1; *(void **)(obj + 0x30) = aux;
        typedef int (*packfn_t)(void *, void *, void *);
        rc = ((packfn_t)(g_base + 0xc157f0))(obj, dsxreg, lutp);
        printf("dsx pack rc=%d aux=%u,%u\n", rc, *(uint32_t *)aux, *(uint32_t *)(aux + 4));
        FILE *f = fopen(argv[9], "wb"); fwrite(dsxreg, 1, 0xb58, f); fclose(f);
        f = fopen(argv[10], "wb"); fwrite(regs, 1, 0x200, f); fwrite(luts, 1, 8 * 0x400, f); fclose(f);
        printf("wrote %s (DSX_REG 0xb58) and %s (packed regs 0x200 + 8 LUTs x 0x400)\n", argv[9], argv[10]);
        return 0;
    }
    if (argc < 8) { fprintf(stderr, "run needs <cfg> <iq> <titan> <hw> <out>\n"); return 2; }

    size_t cfglen, iqlen;
    void *cfg = read_file(argv[3], &cfglen), *iq = read_file(argv[4], &iqlen);
    uint32_t titan = strtoul(argv[5], NULL, 0), hw = strtoul(argv[6], NULL, 0);
    uint32_t cores = argc > 8 ? strtoul(argv[8], NULL, 0) : 1;
    uint32_t w = argc > 10 ? strtoul(argv[9], NULL, 0) : 1920, h = argc > 10 ? strtoul(argv[10], NULL, 0) : 1080;
    printf("configio %zu bytes, iq %zu bytes, titan %#x hw %#x cores %u zoom full %ux%u\n", cfglen, iqlen, titan, hw, cores, w, h);

    fn_scratch_t fscr = export_addr("IPEStripingLibraryCalculateScratchBufSize");
    uint32_t scratch[64] = {0};
    int32_t rc = fscr(cfg, scratch);
    printf("CalculateScratchBufSize rc=%d scratchBufferSize=%u", rc, scratch[0]);
    for (int i = 1; i < 9; i++) printf(" %u", scratch[i]);
    printf("\n");

    fn_create_t fcreate = export_addr("IPEStripingLibraryContextCreate");
    struct sl_memop mem = { sl_alloc, sl_free };
    void *handle = NULL; uint32_t bufsize = 0, cdmsize = 0;
    rc = fcreate(cfg, &mem, titan, hw, &handle, &bufsize, &cdmsize);
    printf("ContextCreate rc=%d handle=%p bufferSize=%u (%#x) cdmBufferSize=%u (%#x)\n", rc, handle, bufsize, bufsize, cdmsize, cdmsize);
    if (rc || !handle) return 3;

    fn_exec_t fexec = export_addr("IPEStripingLibraryExecute");
    struct sl_zoom zoom = { w, h, 0, 0, w, h };
    void *ica = calloc(1, 8192), *ica2 = calloc(1, 8192);
    /* IcaParameters (fw DWARF, 588 B): zoomWindow @0, ifeZoomWindow @24, rest zero = ICA off */
    memcpy(ica, &zoom, sizeof zoom); memcpy((uint8_t *)ica + 24, &zoom, sizeof zoom);
    memcpy(ica2, &zoom, sizeof zoom); memcpy((uint8_t *)ica2 + 24, &zoom, sizeof zoom);
    struct sl_exec exe; memset(&exe, 0, sizeof exe); exe.iq = iq; exe.ica1 = ica; exe.ica2 = ica2; exe.zoom = &zoom; exe.prevZoom = &zoom; exe.maxNumOfCoresToUse = cores; exe.configIO = getenv("SLRUN_EXE_CFG") ? cfg : NULL;
    /* frames[] = extraPassParams[3] (fw DWARF): leave invalid, no extra passes */
    _Static_assert(sizeof(struct sl_frame) == 44, "frame"); _Static_assert(__builtin_offsetof(struct sl_exec, frames) == 0x34, "frames");
    size_t outlen = bufsize + (1 << 20); void *out = calloc(1, outlen); void *meta = calloc(1, 65536);
    rc = fexec(handle, &exe, out, meta, calloc(1, 4096));
    uint32_t total = ((uint32_t *)out)[0x38 / 4];
    printf("Execute rc=%d (%d log lines) header: recalculate=%u numStripesUsed=%u numStripesMax=%u totalSize=%u numPasses=%u\n",
           rc, g_logs, ((uint8_t *)out)[0], ((uint16_t *)out)[0x32 / 2], ((uint16_t *)out)[0x34 / 2], total, ((uint32_t *)out)[0x3c / 4]);
    size_t wlen = (total && total <= outlen) ? total : bufsize;
    FILE *f = fopen(argv[7], "wb"); fwrite(out, 1, wlen, f); fclose(f);
    printf("wrote %zu bytes to %s\n", wlen, argv[7]);
    fn_destroy_t fdestroy = export_addr("IPEStripingLibraryContextDestroy");
    rc = fdestroy(&handle);
    printf("ContextDestroy rc=%d\n", rc);
    return 0;
}
