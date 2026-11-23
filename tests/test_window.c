// HV_EL0_WINDOW (core/window.c): guest user memory mapped into the host, checked where it can go wrong: a page
// remapped, its permission changed, code written through it, a TLBI from another CPU, a process switch, a full
// window (bounded eviction and the old drop-everything), and fault-around (runs of pages, mixed protection, host
// memory that does not run on). Programs run at EL0 through the JIT; what they read and write is compared with
// what the page tables say they must. The knobs come from the environment (HV_WINDOW_PAGES, HV_WINDOW_EVICT,
// HV_WINDOW_AROUND), so the same test is run once per setting.
//
// On a Mac this is the app's code path (vm_remap). On Linux (natively, or under qemu-aarch64) window.c's second
// mappings are mremap's, of shared memory.
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "cpu.h"
#include "dispatch.h"
#include "machine.h"
#include "mmu.h"
#include "sysreg.h"
#include "window.h"

#define P(n) extern const char n[], n##_end[]
P(wrd); P(wwr); P(wwr32); P(wpair); P(wrdsweep); P(wwrsweep); P(wwr32sweep); P(wrdloop); P(wvsweep);
extern const char window_programs_begin[], window_programs_finish[];

#define PG 16384ull
#define DATA_PA 0x40000000ull
#define DATA_PAGES 1024u
#define PT_PA 0x80000000ull
#define PT_SIZE (4u << 20)
#define VBAR 0x70000000ull /* nothing is mapped there: a run stops when an exception gets to it */

#define AP_EL0_RW (1ull << 6)
#define AP_RO (3ull << 6)
#define PXN (1ull << 53)
#define UXN (1ull << 54)

static HvMachine m;
static HvCpu cpu_a, cpu_b;
static uint8_t *dmem, *pt;
static uint64_t pt_next;
static int failures, checks;
static uint32_t k_pages = 4096, k_evict = 1, k_around = 16;

#define CHECK(cond, ...) \
    do { \
        checks++; \
        if (!(cond)) { \
            failures++; \
            printf("  FAIL %s:%d: ", __func__, __LINE__); \
            printf(__VA_ARGS__); \
            printf("\n"); \
        } \
    } while (0)

static uint64_t *word(uint32_t page) { return (uint64_t *)(dmem + page * PG); }
static uint64_t pa_of(uint32_t page) { return DATA_PA + page * PG; }

static uint64_t pt_alloc(void) {
    uint64_t pa = PT_PA + pt_next;
    pt_next += PG;
    if (pt_next > PT_SIZE) abort();
    memset(pt + (pa - PT_PA), 0, PG);
    return pa;
}
static uint64_t *pt_host(uint64_t pa) { return (uint64_t *)(pt + (pa - PT_PA)); }

/* 16 KB granule, 48-bit VA. The level 3 entry for va under root, tables made on the way. */
static uint64_t *pte(uint64_t root, uint64_t va) {
    uint64_t table = root;
    for (int level = 0; level < 3; level++) {
        int shift = 14 + 11 * (3 - level);
        uint64_t *e = &pt_host(table)[(va >> shift) & 0x7ff];
        if (!(*e & 1)) *e = pt_alloc() | 3;
        table = *e & 0x0000ffffffffc000ull;
    }
    return &pt_host(table)[(va >> 14) & 0x7ff];
}
static uint64_t *l2e(uint64_t root, uint64_t va) { /* the level 2 entry (a 32 MB chunk's table) */
    uint64_t table = root;
    for (int level = 0; level < 2; level++) {
        int shift = 14 + 11 * (3 - level);
        uint64_t *e = &pt_host(table)[(va >> shift) & 0x7ff];
        if (!(*e & 1)) *e = pt_alloc() | 3;
        table = *e & 0x0000ffffffffc000ull;
    }
    return &pt_host(table)[(va >> 25) & 0x7ff];
}
static void map(uint64_t root, uint64_t va, uint64_t pa, uint64_t bits) {
    __atomic_store_n(pte(root, va), pa | 3 | 3u << 8 | 1u << 10 | bits, __ATOMIC_SEQ_CST);
}
static void map_code(uint64_t root) {
    uint64_t code = (uint64_t)(uintptr_t)window_programs_begin;
    for (uint64_t off = 0; off < (uint64_t)(window_programs_finish - window_programs_begin); off += PG) map(root, code + off, code + off, AP_RO | PXN);
}

static void mmu_on(HvCpu *cpu, uint64_t ttbr) {
    cpu->mair_el1 = 0xff;
    cpu->tcr_el1 = 16 | 2u << 14 | 1u << 23 | 5ull << 32 | 1ull << 36; /* T0SZ 16, TG0 16K, EPD1, IPS 48 bits, 16-bit ASIDs */
    cpu->ttbr0_el1 = ttbr;
    cpu->sctlr_el1 |= 1 | 1u << 2 | 1u << 12 | 1u << 14 | 1u << 15 | 1u << 26;
    cpu->vbar_el1 = VBAR;
    cpu->cpacr_el1 = 3u << 20;
    hv_tlb_flush(cpu);
}

/* Run [start, end) at EL0. 0: got to the end; 2: took an exception (ESR_EL1, FAR_EL1 say which); -1: neither. */
static int run(HvCpu *cpu, const char *start, const char *end) {
    cpu->el = 0;
    cpu->pc = (uint64_t)(uintptr_t)start;
    m.stop_pc = (uint64_t)(uintptr_t)end;
    m.stop_pc2 = VBAR + 0x400;
    int r = hv_run(cpu);
    if (r != HV_EXIT_HALT) {
        printf("  run: dispatcher returned %d at pc %llx esr %llx far %llx\n", r, (unsigned long long)cpu->pc, (unsigned long long)cpu->esr_el1,
               (unsigned long long)cpu->far_el1);
        return -1;
    }
    return cpu->pc == m.stop_pc ? 0 : 2;
}
#define RUN(cpu, name) run(cpu, name, name##_end)

/* As the guest's kernel would: at EL1, through the same code an MSR or SYS instruction gets to. */
static void sys(HvCpu *cpu, uint32_t insn, uint64_t x) {
    uint32_t el = cpu->el;
    uint64_t x9 = cpu->x[9];
    cpu->el = 1;
    cpu->x[9] = x;
    if ((insn & 0xfff80000u) == 0xd5080000u) hv_sysreg_sys(cpu, insn | 9, 0);
    else hv_sysreg_msr(cpu, insn | 9, 0);
    cpu->x[9] = x9;
    cpu->el = el;
}
static void tlbi_va(HvCpu *cpu, uint64_t va, uint64_t asid) { sys(cpu, 0xd5088320u, asid << 48 | va >> 12); } /* TLBI VAE1IS */
static void tlbi_all(HvCpu *cpu) { sys(cpu, 0xd5088300u, 0); }                                              /* TLBI VMALLE1IS */
static void set_ttbr0(HvCpu *cpu, uint64_t v) { sys(cpu, 0xd5182000u, v); }                                /* MSR TTBR0_EL1 */

static uint64_t rd(HvCpu *cpu, uint64_t va) {
    cpu->x[0] = va, cpu->x[1] = 0xdeadull;
    int r = RUN(cpu, wrd);
    CHECK(r == 0, "read of %llx: run %d esr %llx", (unsigned long long)va, r, (unsigned long long)cpu->esr_el1);
    return cpu->x[1];
}
static int wr(HvCpu *cpu, uint64_t va, uint64_t v) {
    cpu->x[0] = va, cpu->x[1] = v;
    return RUN(cpu, wwr);
}

static void counts(uint64_t c[HV_WC_N], uint32_t *held) { hv_window_counts(&m, c, held); }

static void on_fault(int sig, siginfo_t *info, void *ctx) {
    if (hv_window_signal(&m, info, ctx)) return;
    static const char msg[] = "test_window: a fault that is not the window's\n";
    if (write(2, msg, sizeof msg - 1)) {}
    (void)sig;
    _exit(3);
}

/* ---- the tests ---- */

static void tlb_only(uint64_t va, uint32_t n);

static void test_basic(uint64_t root) {
    const uint64_t va = 0x10000000;
    map(root, va, pa_of(0), AP_EL0_RW | PXN | UXN);
    uint64_t c0[HV_WC_N], c1[HV_WC_N];
    uint32_t held;
    counts(c0, &held);
    for (int i = 0; i < 3; i++) CHECK(rd(&cpu_a, va) == 0x1000, "read %d", i);
    counts(c1, &held);
    CHECK(c1[HV_WC_FAULTS] > c0[HV_WC_FAULTS], "a windowed access faulted at least once (the knob is on)");
    CHECK(c1[HV_WC_REMAP_PAGES] > c0[HV_WC_REMAP_PAGES] && held >= 1, "the page went into the window (held %u)", held);
    uint64_t f = c1[HV_WC_FAULTS];
    CHECK(rd(&cpu_a, va) == 0x1000, "read again");
    counts(c1, &held);
    CHECK(c1[HV_WC_FAULTS] == f, "a page in the window does not fault");
    for (int i = 0; i < 3; i++) {
        CHECK(wr(&cpu_a, va, 0xabc0 + (uint64_t)i) == 0, "write %d", i);
        CHECK(*word(0) == 0xabc0 + (uint64_t)i, "write %d reached the page: %llx", i, (unsigned long long)*word(0));
    }
    word(0)[1] = 0x1111, word(0)[2] = 0, word(0)[3] = 0;
    cpu_a.x[0] = va;
    CHECK(RUN(&cpu_a, wpair) == 0, "pair");
    CHECK(word(0)[2] == 0x1111 && word(0)[3] == 0xabc2, "pair load and store: %llx %llx", (unsigned long long)word(0)[2], (unsigned long long)word(0)[3]);
    *word(0) = 0x1000;
}

static void test_remap(uint64_t root) {
    const uint64_t va = 0x10000000;
    CHECK(rd(&cpu_a, va) == 0x1000, "before");
    for (int i = 0; i < 6; i++) {
        uint32_t page = i & 1 ? 0 : 1;
        map(root, va, pa_of(page), AP_EL0_RW | PXN | UXN);
        tlbi_va(&cpu_a, va, 1);
        for (int k = 0; k < 3; k++) CHECK(rd(&cpu_a, va) == 0x1000 + page, "remap %d read %d", i, k);
        CHECK(wr(&cpu_a, va + 8, 0x77 + (uint64_t)i) == 0 && word(page)[1] == 0x77 + (uint64_t)i, "remap %d: a write goes to the new page", i);
    }
}

static void test_permission(uint64_t root) {
    const uint64_t va = 0x10000000;
    map(root, va, pa_of(0), AP_EL0_RW | PXN | UXN);
    tlbi_va(&cpu_a, va, 1);
    for (int k = 0; k < 3; k++) CHECK(wr(&cpu_a, va, 0x500 + (uint64_t)k) == 0, "write while read-write");
    for (int round = 0; round < 3; round++) {
        map(root, va, pa_of(0), AP_RO | PXN | UXN);
        tlbi_va(&cpu_a, va, 1);
        *word(0) = 0x1000;
        for (int k = 0; k < 3; k++) {
            CHECK(rd(&cpu_a, va) == 0x1000, "read while read-only");
            int r = wr(&cpu_a, va, 0xbad);
            CHECK(r == 2, "a write to a read-only page is the guest's fault (run %d)", r);
            CHECK(cpu_a.esr_el1 >> 26 == 0x24 && (cpu_a.esr_el1 >> 6 & 1) && (cpu_a.esr_el1 & 0x3c) == 0x0c, "a permission fault on a write: esr %llx",
                  (unsigned long long)cpu_a.esr_el1);
            CHECK(cpu_a.far_el1 == va, "far %llx", (unsigned long long)cpu_a.far_el1);
            CHECK(*word(0) == 0x1000, "and nothing was written: %llx", (unsigned long long)*word(0));
        }
        map(root, va, pa_of(0), AP_EL0_RW | PXN | UXN);
        tlbi_va(&cpu_a, va, 1);
        for (int k = 0; k < 3; k++) CHECK(wr(&cpu_a, va, 0x600 + (uint64_t)k) == 0 && *word(0) == 0x600 + (uint64_t)k, "read-write again");
    }
    *word(0) = 0x1000;
}

#define MOVZ_X5(v) (0xd2800005u | (uint32_t)(v) << 5)
#define B_NEXT 0x14000001u

static uint64_t run_code_at(uint64_t va) {
    cpu_a.x[5] = 0;
    int r = run(&cpu_a, (const char *)(uintptr_t)va, (const char *)(uintptr_t)(va + 8));
    CHECK(r == 0, "code at %llx ran (%d)", (unsigned long long)va, r);
    return cpu_a.x[5];
}

/* Code in a page the program can write: a store through the window must still be seen as a change of code. */
static void test_smc(uint64_t root) {
    const uint64_t g = 0x20000000, code = g + 5 * PG; /* a group of 16 pages, the code in its sixth */
    for (uint32_t i = 0; i < 16; i++) {
        map(root, g + i * PG, pa_of(16 + i), AP_EL0_RW | PXN);
        *word(16 + i) = 0x1000 + 16 + i;
    }
    uint32_t prog[2] = {MOVZ_X5(111), B_NEXT};
    memcpy(word(21), prog, sizeof prog);
    CHECK(run_code_at(code) == 111, "the code as written");
    /* the page into the window by reads, alone and then with its neighbours */
    for (int k = 0; k < 3; k++) {
        uint64_t v = rd(&cpu_a, code);
        CHECK((uint32_t)v == MOVZ_X5(111), "the code page reads through the window: %llx", (unsigned long long)v);
    }
    for (int k = 0; k < 3; k++) {
        cpu_a.x[0] = g, cpu_a.x[2] = 16, cpu_a.x[3] = PG, cpu_a.x[4] = 0;
        CHECK(RUN(&cpu_a, wrdsweep) == 0, "sweep over the group");
    }
    for (uint32_t v = 222; v <= 999; v += 111) {
        cpu_a.x[0] = code, cpu_a.x[1] = MOVZ_X5(v);
        CHECK(RUN(&cpu_a, wwr32) == 0, "write of new code");
        uint64_t got = run_code_at(code);
        CHECK(got == v, "after writing code for %u it ran as %llu", v, (unsigned long long)got);
        for (int k = 0; k < 2; k++) rd(&cpu_a, code); /* into the window again, now that it is code again */
    }
    /* the same store reaching the code page in a sweep over its neighbours, the group brought in by one fault */
    for (uint32_t v = 1001; v <= 1005; v++) {
        tlbi_all(&cpu_a);
        tlb_only(g, 16);
        for (int k = 0; k < 2; k++) {
            cpu_a.x[0] = g, cpu_a.x[2] = 16, cpu_a.x[3] = PG, cpu_a.x[4] = 0;
            RUN(&cpu_a, wrdsweep);
        }
        cpu_a.x[0] = g, cpu_a.x[1] = MOVZ_X5(v), cpu_a.x[2] = 16, cpu_a.x[3] = PG;
        CHECK(RUN(&cpu_a, wwr32sweep) == 0, "write sweep over the group");
        for (uint32_t i = 0; i < 16; i++) CHECK(*(uint32_t *)word(16 + i) == MOVZ_X5(v), "page %u took the sweep's write", i);
        uint64_t got = run_code_at(code);
        CHECK(got == v, "after a sweep wrote code for %u it ran as %llu", v, (unsigned long long)got);
    }
}

/* A TLBI from another CPU while this one reads the page through its window. */
static uint64_t g_root;
static volatile int g_seq, g_stop;
#define RVA 0x12000000ull

static void *remote(void *arg) {
    (void)arg;
    hv_self = &cpu_b;
    for (int k = 1; k <= 300; k++) {
        __atomic_add_fetch(&g_seq, 1, __ATOMIC_SEQ_CST); /* odd: changing */
        map(g_root, RVA, pa_of(k & 1 ? 3 : 2), AP_EL0_RW | PXN | UXN);
        tlbi_va(&cpu_b, RVA, 1);
        __atomic_add_fetch(&g_seq, 1, __ATOMIC_SEQ_CST); /* even: page 3 after an odd k, page 2 after an even one */
        usleep(200);
    }
    __atomic_store_n(&g_stop, 1, __ATOMIC_SEQ_CST);
    return NULL;
}

static void test_remote_tlbi(uint64_t root) {
    g_root = root;
    map(root, RVA, pa_of(2), AP_EL0_RW | PXN | UXN);
    tlbi_va(&cpu_a, RVA, 1);
    CHECK(rd(&cpu_a, RVA) == 0x1002 && rd(&cpu_a, RVA) == 0x1002, "before");
    uint64_t c0[HV_WC_N], c1[HV_WC_N];
    uint32_t held;
    counts(c0, &held);
    pthread_t t;
    pthread_create(&t, NULL, remote, NULL);
    int settled = 0, runs = 0, bad = 0;
    while (!__atomic_load_n(&g_stop, __ATOMIC_SEQ_CST)) {
        int s0 = __atomic_load_n(&g_seq, __ATOMIC_SEQ_CST);
        cpu_a.x[0] = RVA, cpu_a.x[2] = 2000, cpu_a.x[4] = 0, cpu_a.x[6] = ~0ull;
        if (RUN(&cpu_a, wrdloop) != 0) { bad++; break; }
        int s1 = __atomic_load_n(&g_seq, __ATOMIC_SEQ_CST);
        uint64_t any = cpu_a.x[4], all = cpu_a.x[6];
        runs++;
        if ((any | 1) != 0x1003 || (all | 1) != 0x1003) bad++; /* only ever one of the two pages */
        if (s0 == s1 && !(s0 & 1)) { /* nothing changed during the run: the page the last TLBI left */
            uint64_t want = (s0 / 2) & 1 ? 0x1003 : 0x1002;
            settled++;
            if (any != want || all != want) {
                bad++;
                printf("  after change %d: read %llx/%llx, the tables say %llx\n", s0 / 2, (unsigned long long)any, (unsigned long long)all, (unsigned long long)want);
            }
        }
    }
    pthread_join(t, NULL);
    counts(c1, &held);
    CHECK(!bad, "%d wrong reads in %d runs", bad, runs);
    CHECK(settled > 0, "some runs fell between changes (%d of %d)", settled, runs);
    CHECK(rd(&cpu_a, RVA) == 0x1002, "the last change (300, even) left page 2");
    printf("  remote TLBI: %d runs, %d between changes, %llu window drops by TLBI\n", runs, settled, (unsigned long long)(c1[HV_WC_TLBI_DROPS] - c0[HV_WC_TLBI_DROPS]));
}

/* Two address spaces with different pages at the same addresses, and one chunk they share. */
static void test_switch(uint64_t root_a) {
    const uint64_t lo = 0x14000000, sh = 0x180000000ull + (3ull << 25), both = 0x180000000ull + (9ull << 25);
    uint64_t root_b = pt_alloc();
    map_code(root_b);
    map(root_a, lo, pa_of(40), AP_EL0_RW | PXN | UXN), map(root_b, lo, pa_of(41), AP_EL0_RW | PXN | UXN);
    map(root_a, sh, pa_of(42), AP_EL0_RW | PXN | UXN), map(root_b, sh, pa_of(43), AP_EL0_RW | PXN | UXN);
    map(root_a, both, pa_of(44), AP_EL0_RW | PXN | UXN);
    *l2e(root_b, both) = *l2e(root_a, both); /* the same level 3 table: what HV_SHARED_KEEP keeps across a switch */
    uint64_t ta = root_a | 1ull << 48, tb = root_b | 2ull << 48;
    uint64_t c0[HV_WC_N], c1[HV_WC_N];
    uint32_t held;
    counts(c0, &held);
    for (int i = 0; i < 40; i++) {
        int b = i & 1;
        set_ttbr0(&cpu_a, b ? tb : ta);
        for (int k = 0; k < 2; k++) {
            CHECK(rd(&cpu_a, lo) == 0x1000u + 40 + (uint64_t)b, "switch %d: the low page", i);
            CHECK(rd(&cpu_a, sh) == 0x1000u + 42 + (uint64_t)b, "switch %d: the shared-region page that differs", i);
            CHECK(rd(&cpu_a, both) == 0x1000u + (i < 20 ? 44 : 45), "switch %d: the page both reach through one table", i);
        }
        CHECK(wr(&cpu_a, lo + 16, 0x9000 + (uint64_t)i) == 0 && word(40 + (uint32_t)b)[2] == 0x9000 + (uint64_t)i, "switch %d: a write lands in this process's page", i);
        if (i == 19) { /* the shared table changes under both */
            map(root_a, both, pa_of(45), AP_EL0_RW | PXN | UXN);
            tlbi_va(&cpu_a, both, 1), tlbi_va(&cpu_a, both, 2);
        }
    }
    set_ttbr0(&cpu_a, ta);
    counts(c1, &held);
    printf("  process switches: %llu window drops (%llu pages)\n", (unsigned long long)(c1[HV_WC_SWITCH_DROPS] - c0[HV_WC_SWITCH_DROPS]),
           (unsigned long long)(c1[HV_WC_SWITCH_PAGES] - c0[HV_WC_SWITCH_PAGES]));
}

static uint64_t sweep_read(uint64_t va, uint32_t n) {
    cpu_a.x[0] = va, cpu_a.x[2] = n, cpu_a.x[3] = PG, cpu_a.x[4] = 0;
    CHECK(RUN(&cpu_a, wrdsweep) == 0, "read sweep");
    return cpu_a.x[4];
}

/* The TLB learns a run of pages first (by loads that do not go through the window), then a windowed access
   faults in the middle of it: with fault-around the whole group comes in one call. */
static void tlb_only(uint64_t va, uint32_t n) {
    cpu_a.x[0] = va, cpu_a.x[2] = n, cpu_a.x[3] = PG;
    CHECK(RUN(&cpu_a, wvsweep) == 0, "SIMD sweep");
}

static void test_around(uint64_t root) {
    const uint64_t va = 0x34000000;
    const uint32_t n = 64;
    uint64_t want = 0;
    for (uint32_t i = 0; i < n; i++) {
        map(root, va + i * PG, pa_of(720 + i), AP_EL0_RW | PXN | UXN);
        *word(720 + i) = 0x8000 + i;
        want += 0x8000 + i;
    }
    uint64_t c0[HV_WC_N], c1[HV_WC_N], c2[HV_WC_N];
    uint32_t held;
    counts(c0, &held);
    tlb_only(va, n);
    counts(c1, &held);
    CHECK(c1[HV_WC_FAULTS] == c0[HV_WC_FAULTS], "SIMD loads do not go through the window (%llu faults)", (unsigned long long)(c1[HV_WC_FAULTS] - c0[HV_WC_FAULTS]));
    CHECK(rd(&cpu_a, va + 37 * PG) == 0x8000 + 37, "a read in the middle of a group");
    counts(c2, &held);
    CHECK(c2[HV_WC_REMAPS] - c1[HV_WC_REMAPS] == 1 && c2[HV_WC_REMAP_PAGES] - c1[HV_WC_REMAP_PAGES] == k_around, "one call brought %llu pages (around %u)",
          (unsigned long long)(c2[HV_WC_REMAP_PAGES] - c1[HV_WC_REMAP_PAGES]), k_around);
    CHECK(sweep_read(va, n) == want, "then the sweep");
    counts(c2, &held);
    CHECK(c2[HV_WC_REMAP_PAGES] - c1[HV_WC_REMAP_PAGES] == n, "every page mapped once: %llu", (unsigned long long)(c2[HV_WC_REMAP_PAGES] - c1[HV_WC_REMAP_PAGES]));
    CHECK(c2[HV_WC_REMAPS] - c1[HV_WC_REMAPS] == n / k_around, "in %llu calls (%u expected)", (unsigned long long)(c2[HV_WC_REMAPS] - c1[HV_WC_REMAPS]), n / k_around);
    CHECK(c2[HV_WC_FAULTS] - c1[HV_WC_FAULTS] == n / k_around, "and as many faults: %llu", (unsigned long long)(c2[HV_WC_FAULTS] - c1[HV_WC_FAULTS]));
    /* the pages came in writable: a write sweep needs nothing more */
    cpu_a.x[0] = va, cpu_a.x[1] = 0x60000, cpu_a.x[2] = n, cpu_a.x[3] = PG;
    CHECK(RUN(&cpu_a, wwrsweep) == 0, "write sweep");
    int wrong = 0;
    for (uint32_t i = 0; i < n; i++) wrong += *word(720 + i) != 0x60000 + i;
    CHECK(!wrong, "%d pages did not take the write", wrong);
    /* one page of the group changes: only it goes, and comes back as the new page */
    map(root, va + 5 * PG, pa_of(790), AP_EL0_RW | PXN | UXN);
    tlbi_va(&cpu_a, va + 5 * PG, 1);
    CHECK(rd(&cpu_a, va + 5 * PG) == 0x1000 + 790 && rd(&cpu_a, va + 4 * PG) == 0x60000 + 4 && rd(&cpu_a, va + 6 * PG) == 0x60000 + 6, "a page of the group remapped");
}

/* More pages than a small window holds; with a large one, each page is mapped once. */
static void test_sweep(uint64_t root, uint32_t n) {
    const uint64_t va = 0x30000000;
    uint64_t want = 0;
    for (uint32_t i = 0; i < n; i++) {
        map(root, va + i * PG, pa_of(100 + i), AP_EL0_RW | PXN | UXN);
        *word(100 + i) = 0x1000 + 100 + i;
        want += 0x1000 + 100 + i;
    }
    uint64_t c[5][HV_WC_N];
    uint32_t held = 0;
    counts(c[0], &held);
    for (int k = 1; k <= 3; k++) {
        uint64_t got = sweep_read(va, n);
        CHECK(got == want, "sweep %d of %u pages: sum %llx, expected %llx", k, n, (unsigned long long)got, (unsigned long long)want);
        counts(c[k], &held);
        CHECK(held <= k_pages, "the window holds %u pages, at most %u", held, k_pages);
    }
#define D(k, f) (c[k][f] - c[(k) - 1][f])
    printf("  sweep of %u pages (window %u, around %u, evict %u): faults %llu/%llu/%llu, remaps %llu/%llu/%llu of %llu/%llu/%llu pages, evictions %llu (%llu pages), resets %llu\n",
           n, k_pages, k_around, k_evict, (unsigned long long)D(1, HV_WC_FAULTS), (unsigned long long)D(2, HV_WC_FAULTS), (unsigned long long)D(3, HV_WC_FAULTS),
           (unsigned long long)D(1, HV_WC_REMAPS), (unsigned long long)D(2, HV_WC_REMAPS), (unsigned long long)D(3, HV_WC_REMAPS),
           (unsigned long long)D(1, HV_WC_REMAP_PAGES), (unsigned long long)D(2, HV_WC_REMAP_PAGES), (unsigned long long)D(3, HV_WC_REMAP_PAGES),
           (unsigned long long)(c[3][HV_WC_EVICTIONS] - c[0][HV_WC_EVICTIONS]), (unsigned long long)(c[3][HV_WC_EVICT_PAGES] - c[0][HV_WC_EVICT_PAGES]),
           (unsigned long long)(c[3][HV_WC_RESETS] - c[0][HV_WC_RESETS]));
    if (n + 64 <= k_pages && n <= HV_TLB_SIZE) { /* everything fits */
        /* A first touch faults twice: the TLB does not know the page (to the TLB path, which fills it), then the
           access again maps it. The pages ahead are not in the TLB yet, so each comes alone. */
        CHECK(D(1, HV_WC_REMAP_PAGES) == n, "sweep 1 mapped every page once: %llu", (unsigned long long)D(1, HV_WC_REMAP_PAGES));
        CHECK(D(2, HV_WC_FAULTS) == 0 && D(3, HV_WC_FAULTS) == 0, "sweeps 2 and 3 did not fault: %llu, %llu", (unsigned long long)D(2, HV_WC_FAULTS), (unsigned long long)D(3, HV_WC_FAULTS));
        CHECK(c[3][HV_WC_EVICTIONS] == c[0][HV_WC_EVICTIONS] && c[3][HV_WC_RESETS] == c[0][HV_WC_RESETS], "nothing was given up");
    } else if (n > k_pages) {
        if (k_evict) CHECK(c[3][HV_WC_EVICTIONS] > c[0][HV_WC_EVICTIONS] && c[3][HV_WC_RESETS] == c[0][HV_WC_RESETS], "a full window gave up part");
        else CHECK(c[3][HV_WC_RESETS] > c[0][HV_WC_RESETS] && c[3][HV_WC_EVICTIONS] == c[0][HV_WC_EVICTIONS], "a full window gave up all (HV_WINDOW_EVICT=0)");
    }
    /* writes over the same pages, then what a read sees */
    cpu_a.x[0] = va, cpu_a.x[1] = 0x50000, cpu_a.x[2] = n, cpu_a.x[3] = PG;
    CHECK(RUN(&cpu_a, wwrsweep) == 0, "write sweep");
    want = 0;
    int wrong = 0;
    for (uint32_t i = 0; i < n; i++) wrong += *word(100 + i) != 0x50000 + i, want += 0x50000 + i;
    CHECK(!wrong, "%d pages did not take the write", wrong);
    CHECK(sweep_read(va, n) == want, "and read back");
    /* a whole-TLB flush empties the window; the pages come back */
    counts(c[0], &held);
    tlbi_all(&cpu_a);
    counts(c[1], &held);
    CHECK(held == 0 || cpu_b.pc_state == NULL, "nothing held after TLBI VMALLE1IS (%u)", held);
    CHECK(c[1][HV_WC_FULL_DROPS] + c[1][HV_WC_USER_DROPS] > c[0][HV_WC_FULL_DROPS] + c[0][HV_WC_USER_DROPS], "counted as a flush");
    for (int k = 0; k < 3; k++) CHECK(sweep_read(va, n) == want, "after the flush, sweep %d", k);
#undef D
}

/* Pages next to each other whose host memory is not: fault-around must take them one at a time. */
static void test_scattered(uint64_t root) {
    const uint64_t va = 0x38000000;
    const uint32_t n = 64;
    uint64_t want = 0;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t page = 800 + (n - 1 - i);
        map(root, va + i * PG, pa_of(page), AP_EL0_RW | PXN | UXN);
        *word(page) = 0x7000 + i * 3;
        want += 0x7000 + i * 3;
    }
    uint64_t c0[HV_WC_N], c1[HV_WC_N];
    uint32_t held;
    counts(c0, &held);
    for (int k = 0; k < 3; k++) CHECK(sweep_read(va, n) == want, "sweep %d over pages in reverse host order", k);
    counts(c1, &held);
    CHECK(c1[HV_WC_REMAPS] - c0[HV_WC_REMAPS] == c1[HV_WC_REMAP_PAGES] - c0[HV_WC_REMAP_PAGES], "one page a call where host memory does not run on (%llu calls, %llu pages)",
          (unsigned long long)(c1[HV_WC_REMAPS] - c0[HV_WC_REMAPS]), (unsigned long long)(c1[HV_WC_REMAP_PAGES] - c0[HV_WC_REMAP_PAGES]));
}

/* A group where every other page is read-only: one run, each page with its own protection. */
static void test_mixed(uint64_t root) {
    const uint64_t va = 0x3c000000;
    for (uint32_t i = 0; i < 16; i++) {
        map(root, va + i * PG, pa_of(900 + i), (i & 1 ? AP_RO : AP_EL0_RW) | PXN | UXN);
        *word(900 + i) = 0x2000 + i;
    }
    uint64_t want = 0;
    for (uint32_t i = 0; i < 16; i++) want += 0x2000 + i;
    uint64_t c0[HV_WC_N], c1[HV_WC_N];
    uint32_t held;
    tlb_only(va, 16);
    counts(c0, &held);
    for (int k = 0; k < 3; k++) CHECK(sweep_read(va, 16) == want, "sweep %d", k);
    counts(c1, &held);
    if (k_around == 16)
        CHECK(c1[HV_WC_REMAPS] - c0[HV_WC_REMAPS] == 1 && c1[HV_WC_MPROTECTS] - c0[HV_WC_MPROTECTS] == 8, "one call for the group, and its eight read-only pages each made so (%llu, %llu)",
              (unsigned long long)(c1[HV_WC_REMAPS] - c0[HV_WC_REMAPS]), (unsigned long long)(c1[HV_WC_MPROTECTS] - c0[HV_WC_MPROTECTS]));
    for (int round = 0; round < 2; round++)
        for (uint32_t i = 0; i < 16; i++) {
            int r = wr(&cpu_a, va + i * PG, 0x3000 + i);
            if (i & 1) {
                CHECK(r == 2 && cpu_a.far_el1 == va + i * PG, "page %u is read-only: the guest's fault (run %d, far %llx)", i, r, (unsigned long long)cpu_a.far_el1);
                CHECK(*word(900 + i) == 0x2000 + i, "page %u was not written", i);
            } else {
                CHECK(r == 0 && *word(900 + i) == 0x3000 + i, "page %u is read-write (run %d)", i, r);
            }
        }
    /* a write sweep stops at the first read-only page */
    cpu_a.x[0] = va, cpu_a.x[1] = 0x4000, cpu_a.x[2] = 16, cpu_a.x[3] = PG;
    int r = RUN(&cpu_a, wwrsweep);
    CHECK(r == 2 && cpu_a.far_el1 == va + PG, "the sweep faults at page 1 (run %d, far %llx)", r, (unsigned long long)cpu_a.far_el1);
    CHECK(*word(900) == 0x4000 && *word(901) == 0x2001 && *word(902) == 0x3002, "and wrote only page 0");
}

static uint32_t env_u(const char *name, uint32_t dflt) {
    const char *v = getenv(name);
    return v ? (uint32_t)atol(v) : dflt;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setenv("HV_EL0_WINDOW", "34", 0);
    k_pages = env_u("HV_WINDOW_PAGES", 4096);
    if (k_pages < 64) k_pages = 64;
    k_evict = env_u("HV_WINDOW_EVICT", 1) != 0;
    k_around = env_u("HV_WINDOW_AROUND", 16);
    if (k_around < 2) k_around = 1;
    if (k_around > 16) k_around = 16;
    while (k_around & (k_around - 1)) k_around &= k_around - 1;
    alarm(600); /* a hang is a failure */

    if (hv_machine_init(&m, 16 << 20) != 0) return 1;
#ifdef __APPLE__
    dmem = mmap(NULL, DATA_PAGES * PG, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
#else
    int dfd = memfd_create("window-test-ram", 0);
    if (dfd < 0 || ftruncate(dfd, (off_t)(DATA_PAGES * PG)) != 0) return 1;
    dmem = mmap(NULL, DATA_PAGES * PG, PROT_READ | PROT_WRITE, MAP_SHARED, dfd, 0);
    if (dmem != MAP_FAILED) hv_window_linux_backing(dmem, DATA_PAGES * PG, dfd);
#endif
    pt = mmap(NULL, PT_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
    if (dmem == MAP_FAILED || pt == MAP_FAILED) return 1;
    for (uint32_t i = 0; i < DATA_PAGES; i++) *word(i) = 0x1000 + i;
    uint64_t code = (uint64_t)(uintptr_t)window_programs_begin;
    hv_machine_add_ram(&m, code, (uint64_t)(window_programs_finish - window_programs_begin), (void *)(uintptr_t)code, 1);
    hv_machine_add_ram(&m, DATA_PA, DATA_PAGES * PG, dmem, 0);
    hv_machine_add_ram(&m, PT_PA, PT_SIZE, pt, 0);

    /* On its own stack, as in the app: translated code runs with the guest's stack pointer in SP. */
    stack_t ss = {.ss_sp = malloc(1 << 18), .ss_size = 1 << 18};
    sigaltstack(&ss, NULL);
    struct sigaction sa = {.sa_flags = SA_SIGINFO | SA_ONSTACK};
    sa.sa_sigaction = on_fault;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);

    if (hv_window_reserve(&m, 2) != 0 || !hv_window_enabled()) {
        printf("no user-memory windows on this host: nothing tested\n");
        return 1;
    }
    uint64_t root = pt_alloc();
    map_code(root);
    hv_cpu_reset_index(&cpu_a, &m, 0);
    hv_cpu_reset_index(&cpu_b, &m, 1);
    mmu_on(&cpu_a, root | 1ull << 48);
    mmu_on(&cpu_b, root | 1ull << 48);
    hv_self = &cpu_a;

#define T(call) \
    do { \
        int f0 = failures; \
        call; \
        printf("%-16s: %s\n", #call, failures == f0 ? "ok" : "FAIL"); \
    } while (0)
    T(test_basic(root));
    T(test_remap(root));
    T(test_permission(root));
    T(test_smc(root));
    T(test_remote_tlbi(root));
    T(test_switch(root));
    T(test_around(root));
    T(test_sweep(root, 600));
    T(test_scattered(root));
    T(test_mixed(root));

    uint64_t c[HV_WC_N];
    uint32_t held;
    counts(c, &held);
    printf("window: faults %llu (to the TLB path %llu), vm_remap %llu of %llu pages, mprotect %llu (%llu upgrades), mmap %llu of %llu pages, evictions %llu (%llu pages), "
           "resets %llu, drops: switch %llu tlbi %llu flush %llu user %llu, code looks %llu (%llu pages)\n",
           (unsigned long long)c[HV_WC_FAULTS], (unsigned long long)c[HV_WC_MISSES], (unsigned long long)c[HV_WC_REMAPS], (unsigned long long)c[HV_WC_REMAP_PAGES],
           (unsigned long long)c[HV_WC_MPROTECTS], (unsigned long long)c[HV_WC_UPGRADES], (unsigned long long)c[HV_WC_UNMAPS], (unsigned long long)c[HV_WC_UNMAP_PAGES],
           (unsigned long long)c[HV_WC_EVICTIONS], (unsigned long long)c[HV_WC_EVICT_PAGES], (unsigned long long)c[HV_WC_RESETS], (unsigned long long)c[HV_WC_SWITCH_DROPS],
           (unsigned long long)c[HV_WC_TLBI_DROPS], (unsigned long long)c[HV_WC_FULL_DROPS], (unsigned long long)c[HV_WC_USER_DROPS], (unsigned long long)c[HV_WC_CODE_SCANS],
           (unsigned long long)c[HV_WC_CODE_PAGES]);
    printf("%d checks, %d failures (HV_WINDOW_PAGES=%u HV_WINDOW_EVICT=%u HV_WINDOW_AROUND=%u)\n", checks, failures, k_pages, k_evict, k_around);
    return failures != 0;
}
