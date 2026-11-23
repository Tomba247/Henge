/* HV_EL0_WINDOW=bits: guest user memory mapped straight into this process. Off unless the knob is set.

   Each CPU reserves a window of 2^bits bytes. A guest user VA v below that is host address win_base + v, so a
   translated EL0 load or store becomes a range check and the access itself (translate.c, put_window), with no
   software TLB lookup. The window starts with nothing mapped. The first access to a page faults; the handler
   (hv_window_signal) looks the page up in the CPU's software TLB, the record of what the guest's page tables say:
   if the entry allows the access, the guest page's host memory is mapped into the window there (vm_remap, the
   same RAM, not a copy) and the access runs again; if not, the access goes to its TLB path instead, which walks
   the tables, faults into the guest, or reaches devices exactly as before. The instruction after a windowed access
   is ADR XZR, <its TLB path>: a no-op that tells the handler where that is.

   The window is a second TLB, so it is emptied when the software TLB is: a whole flush, a process switch (but for
   chunks HV_SHARED_KEEP keeps), a TLBI by page or range (from any CPU), and read-only where a page becomes code,
   so writes to it still take the slow path that notices code changing. Pages it holds are kept in a table (by
   guest page) under a lock, so a drop from another CPU and this CPU's fault handler do not cross.

   What a fault costs is a signal and a vm_remap, and what a drop costs is an mmap, so (2026-11-23, after a phone
   spent a quarter of its busy time in those calls):
   - HV_WINDOW_PAGES=n (4096; 64 to 1048576): how many pages a window holds. Full, it gives up a sixteenth of them
     (bounded eviction, in the order a hand goes round them) rather than all; HV_WINDOW_EVICT=0 drops them all
     as before.
   - HV_WINDOW_AROUND=n (16; 0 or 1: off, at most 16): a fault also maps the pages next to it, inside the aligned
     group of n pages, in one vm_remap: those not in the window yet whose TLB entries (this CPU's, now) allow the
     access that faulted and whose host memory runs on from the faulting page's. Each gets its own protection:
     read-only where its entry does not allow writes or the page holds translated code.
   - Pages given up next to each other go in one mmap.
   Every call and every drop is counted per window (hv_window_counts; the stats block prints them). */
#include "window.h"

#include <mach/mach.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/ucontext.h>

#include "machine.h"

#define WPAGE (1ull << HV_TLB_PAGE_BITS)
/* One never-mapped page past the window: an access that starts in its last page and runs past the end faults
   there and takes the TLB path, instead of reaching whatever the host has mapped next. */
#define WGUARD WPAGE
#define AROUND_MAX 16u

/* The host's part: a second mapping of the same memory, and what a fault's context says. Darwin is what the app
   runs on. Linux is for the tests only (tests/test_window.c, natively or under qemu-aarch64): mremap with an old
   size of 0 makes the second mapping, of shared memory only (or mmap of the file behind it), and the faulting instruction says whether it was a
   store (a windowed access is a plain load or store, single or pair: translate.c). hengelinux is built without
   this file (host/linux/window_stub.c). */
#ifdef __APPLE__
#define UC_PC(uc) ((uc)->uc_mcontext->__ss.__pc)
#define UC_X28(uc) ((uc)->uc_mcontext->__ss.__x[28])
static int fault_is_write(ucontext_t *uc) { return (uc->uc_mcontext->__es.__esr >> 6) & 1; /* WnR */ }
static int alias_pages(void *dst, const uint8_t *src, uint64_t len) {
    vm_address_t at = (vm_address_t)(uintptr_t)dst;
    vm_prot_t cur, max;
    return vm_remap(mach_task_self(), &at, len, 0, VM_FLAGS_FIXED | VM_FLAGS_OVERWRITE, mach_task_self(), (vm_address_t)(uintptr_t)src, FALSE,
                    &cur, &max, VM_INHERIT_NONE) == KERN_SUCCESS ? 0 : -1;
}
#else
#define UC_PC(uc) ((uc)->uc_mcontext.pc)
#define UC_X28(uc) ((uc)->uc_mcontext.regs[28])
static int fault_is_write(ucontext_t *uc) {
    uint32_t insn = *(const uint32_t *)(uintptr_t)uc->uc_mcontext.pc;
    if ((insn & 0x3a000000u) == 0x28000000u) return !(insn >> 22 & 1);     /* pairs: L */
    return insn >> 26 & 1 ? !(insn >> 22 & 1) : !(insn >> 22 & 3);          /* single: opc, by V */
}
/* Memory the test made from a file (memfd): mapped again from the file, which qemu-aarch64 can do (it refuses an
   mremap with an old size of 0). Anything else: mremap. */
static struct { const uint8_t *host; uint64_t size; int fd; } backing[8];
static int nbacking;
void hv_window_linux_backing(const uint8_t *host, uint64_t size, int fd) {
    if (nbacking < 8) backing[nbacking].host = host, backing[nbacking].size = size, backing[nbacking].fd = fd, nbacking++;
}
static int alias_pages(void *dst, const uint8_t *src, uint64_t len) {
    for (int i = 0; i < nbacking; i++)
        if ((uint64_t)(src - backing[i].host) < backing[i].size && (uint64_t)(src - backing[i].host) + len <= backing[i].size)
            return mmap(dst, len, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED, backing[i].fd, (off_t)(src - backing[i].host)) == MAP_FAILED ? -1 : 0;
    return mremap((void *)(uintptr_t)src, 0, len, MREMAP_MAYMOVE | MREMAP_FIXED, dst) == MAP_FAILED ? -1 : 0;
}
#endif
#define BATCH 256u /* pages given up in one go at most (their addresses sit on the signal stack) */

typedef struct { uint64_t va; const uint8_t *host; uint32_t rw; } Ent;

typedef struct HvWindow {
    uint8_t *base;
    uint64_t size;
    int lock;
    uint32_t n, cap;      /* pages held, and at most */
    uint32_t mask;        /* idx has mask + 1 slots: at least twice cap */
    uint32_t fmask;       /* filt has fmask + 1 counters: four times cap */
    uint32_t hand;        /* where eviction goes on from, in ent */
    Ent *ent;             /* the pages held, packed: 0 .. n-1 */
    uint32_t *idx;        /* by guest page: position in ent + 1 (0: empty); open addressing, no tombstones */
    uint16_t *filt;       /* by host page: how many pages held hash here (hv_window_protect looks no further at 0) */
    uint64_t c[HV_WC_N];  /* counters, under the lock */
} HvWindow;

static uint32_t win_pages = 4096, win_evict = 1, win_around = AROUND_MAX;
static int win_knobs_read;

static void read_knobs(void) {
    if (win_knobs_read) return;
    win_knobs_read = 1;
    const char *v;
    if ((v = getenv("HV_WINDOW_PAGES"))) {
        long k = atol(v);
        if (k < 64) k = 64;
        if (k > 1 << 20) k = 1 << 20;
        win_pages = (uint32_t)k;
    }
    if ((v = getenv("HV_WINDOW_EVICT"))) win_evict = atoi(v) != 0;
    if ((v = getenv("HV_WINDOW_AROUND"))) {
        long k = atol(v);
        win_around = k < 2 ? 1 : k > (long)AROUND_MAX ? AROUND_MAX : (uint32_t)k;
        while (win_around & (win_around - 1)) win_around &= win_around - 1; /* a power of two: the group is aligned */
    }
}

int hv_window_enabled(void) {
    return hv_knob[HV_K_EL0WINDOW];
}

static void lock(HvWindow *w) {
    while (__atomic_exchange_n(&w->lock, 1, __ATOMIC_ACQUIRE))
        while (__atomic_load_n(&w->lock, __ATOMIC_RELAXED)) {}
}
static void unlock(HvWindow *w) { __atomic_store_n(&w->lock, 0, __ATOMIC_RELEASE); }

static uint32_t hash(const HvWindow *w, uint64_t va) { return (uint32_t)((va >> HV_TLB_PAGE_BITS) * 0x9e3779b97f4a7c15ull >> 40) & w->mask; }
static uint32_t fhash(const HvWindow *w, const uint8_t *host) {
    return (uint32_t)(((uint64_t)(uintptr_t)host >> HV_TLB_PAGE_BITS) * 0xd6e8feb86659fd93ull >> 40) & w->fmask;
}

/* Position in ent of guest page va, or -1. */
static int find(const HvWindow *w, uint64_t va) {
    for (uint32_t i = hash(w, va), k = 0; k <= w->mask; i = (i + 1) & w->mask, k++) {
        uint32_t x = w->idx[i];
        if (!x) return -1;
        if (w->ent[x - 1].va == va) return (int)(x - 1);
    }
    return -1;
}

/* The idx slot that holds position pos. */
static uint32_t slot_of(const HvWindow *w, uint32_t pos) {
    uint32_t i = hash(w, w->ent[pos].va);
    while (w->idx[i] != pos + 1) i = (i + 1) & w->mask;
    return i;
}

/* Forget the page at pos (its mapping is the caller's to remove): its idx slot goes, later entries of the run
   move back (no tombstones), and the last page takes its place in ent. */
static void erase(HvWindow *w, uint32_t pos) {
    uint32_t i = slot_of(w, pos);
    uint16_t *f = &w->filt[fhash(w, w->ent[pos].host)];
    if (*f && *f != 0xffff) (*f)--; /* a counter that filled stays full until everything is dropped */
    w->idx[i] = 0;
    for (uint32_t j = (i + 1) & w->mask; w->idx[j]; j = (j + 1) & w->mask) {
        uint32_t h = hash(w, w->ent[w->idx[j] - 1].va);
        /* j's entry moves back to i unless its home lies cyclically in (i, j] */
        int stays = i <= j ? h > i && h <= j : h > i || h <= j;
        if (!stays) {
            w->idx[i] = w->idx[j];
            w->idx[j] = 0;
            i = j;
        }
    }
    uint32_t last = --w->n;
    if (pos != last) {
        w->idx[slot_of(w, last)] = pos + 1;
        w->ent[pos] = w->ent[last];
    }
}

static void insert(HvWindow *w, uint64_t va, const uint8_t *host, int rw) {
    uint32_t pos = w->n++;
    w->ent[pos] = (Ent){va, host, (uint32_t)rw};
    uint32_t j = hash(w, va);
    while (w->idx[j]) j = (j + 1) & w->mask;
    w->idx[j] = pos + 1;
    uint16_t *f = &w->filt[fhash(w, host)];
    if (*f != 0xffff) (*f)++;
}

/* Unmap vas[0..k): sorted here, and pages next to each other go in one call. */
static void unmap_pages(HvWindow *w, uint64_t *vas, uint32_t k) {
    for (uint32_t i = 1; i < k; i++) { /* insertion sort: k is small, and often in order already */
        uint64_t v = vas[i];
        uint32_t j = i;
        for (; j && vas[j - 1] > v; j--) vas[j] = vas[j - 1];
        vas[j] = v;
    }
    for (uint32_t i = 0; i < k;) {
        uint32_t j = i + 1;
        while (j < k && vas[j] == vas[j - 1] + WPAGE) j++;
        mmap(w->base + vas[i], (uint64_t)(j - i) << HV_TLB_PAGE_BITS, PROT_NONE, MAP_FIXED | MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
        w->c[HV_WC_UNMAPS]++;
        i = j;
    }
    w->c[HV_WC_UNMAP_PAGES] += k;
}

/* Everything goes. Returns how many pages that was. */
static uint32_t drop_all_locked(HvWindow *w) {
    uint32_t n = w->n;
    if (!n) return 0;
    if (n > 64) {
        mmap(w->base, w->size, PROT_NONE, MAP_FIXED | MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
        w->c[HV_WC_UNMAPS]++, w->c[HV_WC_UNMAP_PAGES] += n;
        /* plain loops, kept from becoming memset: this file is built without SIMD, the C library is not */
        for (uint32_t i = 0; i <= w->mask; i++) { w->idx[i] = 0; __asm__ volatile(""); }
        for (uint32_t i = 0; i <= w->fmask; i++) { w->filt[i] = 0; __asm__ volatile(""); }
        w->n = 0;
    } else {
        uint64_t vas[64];
        for (uint32_t i = 0; i < n; i++) vas[i] = w->ent[i].va;
        while (w->n) erase(w, w->n - 1);
        unmap_pages(w, vas, n);
    }
    w->hand = 0;
    return n;
}

/* Room for `need` more pages: the oldest-placed sixteenth of the window goes (at least `need`), or all of it. */
static void make_room(HvWindow *w, uint32_t need) {
    if (w->n + need <= w->cap) return;
    if (!win_evict) {
        w->c[HV_WC_RESETS]++, w->c[HV_WC_RESET_PAGES] += drop_all_locked(w);
        return;
    }
    uint32_t k = w->cap / 16;
    if (k < need) k = need;
    if (k > BATCH) k = BATCH;
    if (k > w->n) k = w->n;
    uint64_t vas[BATCH];
    for (uint32_t i = 0; i < k; i++) {
        /* erase puts the last page, a newer one, where the hand is, and the hand steps past it: what was mapped
           last is not what goes next. */
        if (w->hand >= w->n) w->hand = 0;
        vas[i] = w->ent[w->hand].va;
        erase(w, w->hand);
        w->hand++;
    }
    unmap_pages(w, vas, k);
    w->c[HV_WC_EVICTIONS]++, w->c[HV_WC_EVICT_PAGES] += k;
}

static int alloc_tables(HvWindow *w) {
    read_knobs();
    w->cap = win_pages;
    uint32_t slots = 256;
    while (slots < w->cap * 2) slots <<= 1;
    w->mask = slots - 1;
    w->fmask = slots * 2 - 1;
    w->ent = calloc(w->cap, sizeof *w->ent);
    w->idx = calloc(slots, sizeof *w->idx);
    w->filt = calloc((size_t)slots * 2, sizeof *w->filt);
    return w->ent && w->idx && w->filt ? 0 : -1;
}

int hv_window_attach(HvCpu *cpu) {
    int bits = hv_window_enabled();
    if (!bits || !cpu->pc_state) return 0;
    if (cpu->pc_state->win) { /* a reset CPU: its window again, empty */
        HvWindow *w = cpu->pc_state->win;
        lock(w);
        drop_all_locked(w);
        unlock(w);
        cpu->win_base = (uint64_t)(uintptr_t)w->base;
        return 0;
    }
    if (bits < 30 || bits > 40) return -1;
    HvWindow *w = calloc(1, sizeof *w);
    if (!w) return -1;
    w->size = 1ull << bits;
    void *p = alloc_tables(w) == 0 ? mmap(NULL, w->size + WGUARD, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0) : MAP_FAILED;
    if (p == MAP_FAILED) {
        free(w->ent), free(w->idx), free(w->filt), free(w);
        return -1;
    }
    w->base = p;
    cpu->pc_state->win = w;
    cpu->win_base = (uint64_t)(uintptr_t)p;
    return 0;
}

int hv_window_reserve(HvMachine *m, int ncpus) {
    int bits = hv_window_enabled();
    if (!bits) return 0;
    /* An HvCpu is megabytes (its TLB tables): on the heap, not the stack, which on iOS is a 512 KB thread stack
       (a stack one killed the app here without a word, 2026-11-13). Only its pc_state is used. */
    HvCpu *probe = calloc(1, sizeof *probe);
    if (!probe) { hv_knob[HV_K_EL0WINDOW] = 0; return -1; }
    /* The phone gave three 16 GB windows and refused the fourth (2026-11-13): when they do not all fit, try again
       a size down, to 4 GB, before going without. Nothing is translated yet, so the size can still change. */
    for (; bits >= 32; bits--) {
        hv_knob[HV_K_EL0WINDOW] = bits;
        int i = 0;
        for (; i < ncpus && i < HV_MAX_CPUS; i++) {
            probe->pc_state = &m->percpu[i];
            if (hv_window_attach(probe) != 0) break;
        }
        if (i >= ncpus || i >= HV_MAX_CPUS) {
            free(probe);
            fprintf(stderr, "henge: user-memory windows: %d CPUs x %llu GB, %u pages each (%s when full), a fault maps up to %u pages\n", ncpus,
                    (unsigned long long)(1ull << bits) >> 30, win_pages, win_evict ? "a sixteenth goes" : "all go", win_around);
            return 0;
        }
        fprintf(stderr, "henge: no room for %d CPUs' user-memory windows of %d bits (CPU %d's was refused)\n", ncpus, bits, i);
        for (int j = 0; j < HV_MAX_CPUS; j++) {
            probe->pc_state = &m->percpu[j];
            hv_window_detach(probe);
        }
    }
    free(probe);
    fprintf(stderr, "henge: running without user-memory windows\n");
    hv_knob[HV_K_EL0WINDOW] = 0;
    return -1;
}

void hv_window_detach(HvCpu *cpu) {
    HvWindow *w = cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w) return;
    cpu->pc_state->win = NULL;
    cpu->win_base = 0;
    munmap(w->base, w->size + WGUARD);
    free(w->ent), free(w->idx), free(w->filt), free(w);
}

void hv_window_counts(HvMachine *m, uint64_t out[HV_WC_N], uint32_t *held) {
    for (int k = 0; k < HV_WC_N; k++) out[k] = 0;
    *held = 0;
    for (int i = 0; i < HV_MAX_CPUS; i++) {
        HvWindow *w = m->percpu[i].win;
        if (!w) continue;
        for (int k = 0; k < HV_WC_N; k++) out[k] += __atomic_load_n(&w->c[k], __ATOMIC_RELAXED);
        *held += __atomic_load_n(&w->n, __ATOMIC_RELAXED);
    }
}

void hv_window_drop_all(HvCpu *cpu, int why) {
    HvWindow *w = cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w || !w->n) return;
    lock(w);
    uint32_t n = drop_all_locked(w);
    if (n) w->c[why]++, w->c[why + 1] += n;
    unlock(w);
}

void hv_window_drop_except(HvCpu *cpu, uint64_t lo, const uint64_t *keep, uint32_t nchunks) {
    HvWindow *w = cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w || !w->n) return;
    lock(w);
    uint64_t vas[BATCH];
    uint32_t k = 0, total = 0;
    for (uint32_t pos = w->n; pos-- > 0;) { /* from the end: erase moves the last page into pos, one already looked at */
        uint64_t va = w->ent[pos].va, c = (va - lo) >> 25;
        if (c < nchunks && (keep[c / 64] >> (c % 64) & 1)) continue;
        vas[k++] = va;
        erase(w, pos);
        if (k == BATCH) unmap_pages(w, vas, k), total += k, k = 0;
    }
    if (k) unmap_pages(w, vas, k), total += k;
    if (total) w->c[HV_WC_SWITCH_DROPS]++, w->c[HV_WC_SWITCH_PAGES] += total;
    unlock(w);
}

void hv_window_drop_range(HvCpu *cpu, uint64_t va, uint64_t len) {
    HvWindow *w = cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w) return;
    va &= 0x00ffffffffffffffull; /* tagged: by the low 56 bits, as the TLB */
    if (va >= w->size) return;
    uint64_t first = va & ~(WPAGE - 1), end = va + len < w->size ? va + len : w->size;
    lock(w); /* before looking at n: the fault handler maps a page and counts it under the lock */
    if (!w->n) {
        unlock(w);
        return;
    }
    uint64_t vas[BATCH];
    uint32_t k = 0, total = 0;
    if ((end - first) >> HV_TLB_PAGE_BITS > w->n) { /* more pages in the range than in the window: through the window's */
        for (uint32_t pos = w->n; pos-- > 0;) {
            uint64_t p = w->ent[pos].va;
            if (!(p < end && p + WPAGE > first)) continue;
            vas[k++] = p;
            erase(w, pos);
            if (k == BATCH) unmap_pages(w, vas, k), total += k, k = 0;
        }
    } else {
        for (uint64_t p = first; p < end; p += WPAGE) {
            int pos = find(w, p);
            if (pos < 0) continue;
            vas[k++] = p;
            erase(w, (uint32_t)pos);
            if (k == BATCH) unmap_pages(w, vas, k), total += k, k = 0;
        }
    }
    if (k) unmap_pages(w, vas, k), total += k;
    if (total) w->c[HV_WC_TLBI_DROPS]++, w->c[HV_WC_TLBI_PAGES] += total;
    unlock(w);
}

void hv_window_protect(HvCpu *cpu, const uint8_t *host_page) {
    HvWindow *w = cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w || !w->n) return;
    lock(w); /* before the filter: a fault that saw the page without code has its entry in by the time this has the lock */
    if (w->filt[fhash(w, host_page)]) {
        w->c[HV_WC_CODE_SCANS]++;
        for (uint32_t i = 0; i < w->n; i++)
            if (w->ent[i].rw && w->ent[i].host == host_page) {
                mprotect(w->base + w->ent[i].va, WPAGE, PROT_READ);
                w->c[HV_WC_MPROTECTS]++, w->c[HV_WC_CODE_PAGES]++;
                w->ent[i].rw = 0;
            }
    }
    unlock(w);
}

static HvRam *ram_with(HvMachine *m, const uint8_t *host) {
    for (int i = 0; i < m->nram; i++)
        if ((uint64_t)(host - m->ram[i].host) < m->ram[i].size) return &m->ram[i];
    return NULL;
}

/* May guest page va, whose host memory is host in ram, be written through the window: the TLB entry allows writes
   and the page holds no translated code (as tlb_fill's write_guard). */
static int writable(const HvTlbEntry *e, uint64_t va, HvRam *ram, const uint8_t *host) {
    return e->write_tag + HV_TLB_ADJ == va && !ram->readonly &&
           !__atomic_load_n(&ram->code_pages[(uint64_t)(host - ram->host) >> HV_TLB_PAGE_BITS], __ATOMIC_SEQ_CST);
}

/* Map npages guest pages from va to the host memory from host_page, which runs on; rw[i]: page i read-write.
   None of them is in the window. 0 on success (all of them or none). */
static int map_run(HvWindow *w, uint64_t va, const uint8_t *host_page, uint32_t npages, const uint8_t *rw) {
    make_room(w, npages);
    uint64_t len = (uint64_t)npages << HV_TLB_PAGE_BITS;
    if (alias_pages(w->base + va, host_page, len) != 0) return -1;
    w->c[HV_WC_REMAPS]++, w->c[HV_WC_REMAP_PAGES] += npages;
    for (uint32_t i = 0; i < npages;) { /* the read-only ones, those next to each other in one call */
        if (rw[i]) { i++; continue; }
        uint32_t j = i + 1;
        while (j < npages && !rw[j]) j++;
        w->c[HV_WC_MPROTECTS]++;
        if (mprotect(w->base + va + ((uint64_t)i << HV_TLB_PAGE_BITS), (uint64_t)(j - i) << HV_TLB_PAGE_BITS, PROT_READ)) {
            mmap(w->base + va, len, PROT_NONE, MAP_FIXED | MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
            w->c[HV_WC_UNMAPS]++;
            return -1;
        }
        i = j;
    }
    for (uint32_t i = 0; i < npages; i++) insert(w, va + ((uint64_t)i << HV_TLB_PAGE_BITS), host_page + ((uint64_t)i << HV_TLB_PAGE_BITS), rw[i]);
    return 0;
}

/* The faulting page va (host memory host, in ram; rw: mapped writable) and what fault-around adds to it. */
static int map_fault(HvCpu *cpu, HvWindow *w, uint64_t va, const uint8_t *host, HvRam *ram, int rw, int write) {
    int pos = find(w, va);
    if (pos >= 0 && w->ent[pos].host == host) { /* already there: only a write to a read-only one gets here */
        if (rw && !w->ent[pos].rw) {
            w->c[HV_WC_MPROTECTS]++, w->c[HV_WC_UPGRADES]++;
            if (mprotect(w->base + va, WPAGE, PROT_READ | PROT_WRITE)) return -1;
            w->ent[pos].rw = 1;
        }
        return 0;
    }
    if (pos >= 0) { /* there for other host memory (not seen: a changed mapping is dropped first): it goes */
        uint64_t one = va;
        erase(w, (uint32_t)pos);
        unmap_pages(w, &one, 1);
    }
    uint8_t rws[AROUND_MAX];
    uint64_t lo = va, hi = va + WPAGE; /* the run: [lo, hi) */
    uint32_t at = AROUND_MAX;          /* the faulting page's place in tmp while the run grows either way */
    uint8_t tmp[AROUND_MAX * 2];
    tmp[at] = (uint8_t)rw;
    uint32_t first = at, last = at;
    if (win_around > 1) {
        uint64_t g0 = va & ~((uint64_t)win_around * WPAGE - 1), g1 = g0 + (uint64_t)win_around * WPAGE;
        if (g0 < WPAGE) g0 = WPAGE; /* page 0 is never mapped */
        if (g1 > w->size) g1 = w->size;
        for (int dir = -1; dir <= 1; dir += 2) {
            for (;;) {
                uint64_t p = dir < 0 ? lo - WPAGE : hi;
                if (dir < 0 ? lo <= g0 : hi >= g1) break;
                HvTlbEntry *e = hv_tlb_ent(cpu, (p >> HV_TLB_PAGE_BITS) & (HV_TLB_SIZE - 1), 0);
                if (e->read_tag + HV_TLB_ADJ != p || (write && e->write_tag + HV_TLB_ADJ != p)) break;
                const uint8_t *h = (const uint8_t *)(uintptr_t)(p + (uint64_t)e->addend);
                if (h != host + (p - va)) break;                       /* host memory does not run on */
                if ((uint64_t)(h - ram->host) >= ram->size) break;     /* nor stay in the same RAM */
                if (find(w, p) >= 0) break;                            /* in the window already */
                uint8_t r = (uint8_t)writable(e, p, ram, h);
                if (dir < 0) lo = p, tmp[--first] = r;
                else hi = p + WPAGE, tmp[++last] = r;
            }
        }
    }
    uint32_t npages = last - first + 1;
    for (uint32_t i = 0; i < npages; i++) rws[i] = tmp[first + i];
    if (map_run(w, lo, host - (va - lo), npages, rws) == 0) return 0;
    if (npages == 1) return -1;
    rws[0] = (uint8_t)rw;
    return map_run(w, va, host, 1, rws); /* the run was refused: the page alone */
}

int hv_window_signal(HvMachine *m, siginfo_t *info, void *ctx) {
    if (!m || !hv_window_enabled()) return 0;
    ucontext_t *uc = ctx;
    uint64_t pc = UC_PC(uc), x28 = UC_X28(uc);
    HvCpu *cpu = NULL;
    for (int i = 0; i < m->ncpus && i < HV_MAX_CPUS; i++)
        if (m->cpus[i] && (uint64_t)(uintptr_t)m->cpus[i] == x28) cpu = m->cpus[i];
    HvWindow *w = cpu && cpu->pc_state ? cpu->pc_state->win : NULL;
    if (!w) return 0;
    uint64_t addr = (uint64_t)(uintptr_t)info->si_addr;
    if (addr - (uint64_t)(uintptr_t)w->base >= w->size + WGUARD) return 0;
    uint64_t rx = (uint64_t)(uintptr_t)m->jit.base;
    if (pc - rx >= (uint64_t)m->jit.capacity * 4 || pc + 4 - rx >= (uint64_t)m->jit.capacity * 4) return 0;
    uint32_t marker = *(const uint32_t *)(uintptr_t)(pc + 4);
    if ((marker & 0x9f00001fu) != 0x1000001fu) return 0; /* ADR XZR, <the TLB path> */
    int64_t off = (int64_t)((((uint64_t)marker >> 5 & 0x7ffff) << 2) | (marker >> 29 & 3)) << 43 >> 43;
    uint64_t tlb_path = pc + 4 + (uint64_t)off;

    uint64_t va = (addr - (uint64_t)(uintptr_t)w->base) & ~(WPAGE - 1);
    int write = fault_is_write(uc);
    int done = 0;
    lock(w);
    HvTlbEntry *e = hv_tlb_ent(cpu, (va >> HV_TLB_PAGE_BITS) & (HV_TLB_SIZE - 1), 0);
    int r_ok = e->read_tag + HV_TLB_ADJ == va, w_ok = e->write_tag + HV_TLB_ADJ == va;
    if (va && va < w->size && !m->watch_va && !m->watch_pa && (write ? w_ok : r_ok)) {
        const uint8_t *host = (const uint8_t *)(uintptr_t)(va + (uint64_t)e->addend);
        HvRam *ram = ram_with(m, host);
        /* writable only where the TLB says so and the page holds no translated code */
        int rw = ram && writable(e, va, ram, host);
        if (ram && (rw || !write) && map_fault(cpu, w, va, host, ram, rw, write) == 0) done = 1;
    }
    w->c[HV_WC_FAULTS]++;
    if (!done) w->c[HV_WC_MISSES]++;
    unlock(w);
    if (m->stats_interval) {
        if (done) m->stats.win_maps++;
        else m->stats.win_misses++;
    }
    if (!done) UC_PC(uc) = tlb_path;
    return 1;
}
