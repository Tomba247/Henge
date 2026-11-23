// HV_REVIVE (core/dispatch.c, find_stale): a write to a page of code drops every block of the page; a block
// whose own instructions did not change is put back instead of translated again. What can go wrong is checked:
// the changed code must be what runs, also when the unchanged block got to it through an exit chained within the
// page (direct and conditional), from another page, and while a second CPU runs the same page.
//
// Run with HV_REVIVE=1 and without: the results must be the same, and only the counts differ.
// Programs run at EL1 with the MMU off (guest physical = guest virtual).
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "cpu.h"
#include "dispatch.h"
#include "machine.h"
#include "mmu.h"

#define PG 16384ull
#define BASE 0x40000000ull
#define NPAGES 8u

#define ADD_X0(imm) (0x91000000u | (uint32_t)(imm) << 10) /* add x0, x0, #imm */
#define RET 0xd65f03c0u
#define NOP 0xd503201fu
#define B(from, to) (0x14000000u | (uint32_t)((((int64_t)(to) - (int64_t)(from)) / 4) & 0x3ffffff))
#define BL(from, to) (0x94000000u | (uint32_t)((((int64_t)(to) - (int64_t)(from)) / 4) & 0x3ffffff))
#define BLR_X10 0xd63f0140u
#define STR_W1_X2 0xb9000041u /* str w1, [x2] */
#define IC_IVAU_X2 0xd50b7522u
#define DSB_ISH 0xd5033b9fu
#define ISB 0xd5033fdfu
#define CBNZ_X3(from, to) (0xb5000003u | (uint32_t)((((int64_t)(to) - (int64_t)(from)) / 4) & 0x7ffff) << 5)

/* page 0: the callers and the writer; page 1: the code that is rewritten; page 2: more code, never written */
#define MAIN_A (BASE + 0x000) /* blr x10 */
#define MAIN_B (BASE + 0x100) /* bl f: an exit chained from another page */
#define WRITER (BASE + 0x200) /* str w1, [x2]; ic ivau, x2; dsb ish; isb: what a guest must do for another CPU to run the new code */
#define MAIN_C (BASE + 0x300) /* bl f2 */
#define STOP (BASE + 0x800)  /* every caller ends here: one stop address for both CPUs */
#define P (BASE + PG)
#define F (P + 0x000)  /* x0 += 1; b g */
#define G (P + 0x400)  /* x0 += 0x10 (or what was written); ret */
#define H (P + 0x800)  /* x0 += 0x100; ret */
#define F2 (P + 0xc00) /* cbnz x3, h; x0 += 2; b g */
#define Q (BASE + 2 * PG)
#define K (Q + 0x000) /* x0 += 0x200; ret */

static HvMachine m;
static HvCpu cpu_a, cpu_b;
static uint8_t *mem;
static int failures, checks;
static int revive;

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

static void put(uint64_t at, uint32_t insn) { memcpy(mem + (at - BASE), &insn, 4); }

static int run(HvCpu *cpu, uint64_t pc) {
    cpu->el = 1;
    cpu->pc = pc;
    int r = hv_run(cpu);
    if (r != HV_EXIT_HALT || cpu->pc != STOP) {
        printf("  run from %llx: dispatcher returned %d at pc %llx\n", (unsigned long long)pc, r, (unsigned long long)cpu->pc);
        return -1;
    }
    return 0;
}

/* x0 after calling fn through the indirect call (0), the direct call to f (1), or the one to f2 (2). */
static uint64_t call(HvCpu *cpu, int how, uint64_t fn, uint64_t x3) {
    cpu->x[0] = 0, cpu->x[3] = x3, cpu->x[10] = fn;
    uint64_t at = how == 0 ? MAIN_A : how == 1 ? MAIN_B : MAIN_C;
    if (run(cpu, at) != 0) return ~0ull;
    return cpu->x[0];
}

/* The guest writes an instruction (a store from translated code, as a JIT in the guest does). */
static void guest_write(HvCpu *cpu, uint64_t at, uint32_t insn) {
    cpu->x[1] = insn, cpu->x[2] = at;
    if (run(cpu, WRITER) != 0) failures++;
}

static void test_single(void) {
    put(WRITER + 4, NOP); /* one CPU in this test: without the IC IVAU, as test_exc's p_smc_nomaint */
    hv_dma_written(&m, WRITER, 16);
    guest_write(&cpu_a, Q + 0x3000, NOP); /* the writer itself translated before anything is counted */
    uint64_t t0 = m.stats.translations, r0 = m.stats.revived, s0 = m.stats.flush_smc;
    for (int i = 0; i < 3; i++) { /* three times: the exits get chained */
        CHECK(call(&cpu_a, 0, F, 0) == 0x11, "f then g");
        CHECK(call(&cpu_a, 1, F, 0) == 0x11, "f then g, called directly");
        CHECK(call(&cpu_a, 0, H, 0) == 0x100, "h");
        CHECK(call(&cpu_a, 2, F2, 0) == 0x12, "f2 then g");
        CHECK(call(&cpu_a, 2, F2, 1) == 0x100, "f2 then h");
        CHECK(call(&cpu_a, 0, K, 0) == 0x200, "k");
    }
    uint64_t t1 = m.stats.translations;
    CHECK(m.stats.revived == r0, "nothing revived before any write");

    /* g changes: f, f2 and h did not, and each must get to the new g. */
    guest_write(&cpu_a, G, ADD_X0(0x20));
    CHECK(m.stats.flush_smc == s0 + 1, "the write dropped the page's blocks (%llu drops)", (unsigned long long)(m.stats.flush_smc - s0));
    CHECK(call(&cpu_a, 1, F, 0) == 0x21, "f (chained to the old g) runs the new g");
    CHECK(call(&cpu_a, 2, F2, 0) == 0x22, "f2 (its conditional's other way chained to the old g) runs the new g");
    CHECK(call(&cpu_a, 2, F2, 1) == 0x100, "f2 then h");
    CHECK(call(&cpu_a, 0, F, 0) == 0x21, "f through the indirect call");
    CHECK(call(&cpu_a, 0, H, 0) == 0x100, "h");
    CHECK(call(&cpu_a, 0, K, 0) == 0x200, "k");
    uint64_t t2 = m.stats.translations, r2 = m.stats.revived;
    if (revive) {
        CHECK(r2 - r0 >= 3, "f, f2 and h were revived (%llu)", (unsigned long long)(r2 - r0));
        CHECK(t2 - t1 == 1, "only the new g was translated (%llu)", (unsigned long long)(t2 - t1));
        CHECK(m.stats.revive_changed >= 1, "the old g was seen, and seen to have changed");
    } else {
        CHECK(r2 == r0, "nothing revived with the knob off");
    }

    /* The same bytes written again: the page is dropped, everything comes back. */
    guest_write(&cpu_a, G, ADD_X0(0x20));
    CHECK(call(&cpu_a, 1, F, 0) == 0x21, "after the same bytes again");
    uint64_t t3 = m.stats.translations;
    if (revive) CHECK(t3 == t2, "nothing translated after a write that changed nothing (%llu)", (unsigned long long)(t3 - t2));

    /* Back to the first g: its first translation is still there, two generations old. */
    guest_write(&cpu_a, G, ADD_X0(0x10));
    CHECK(call(&cpu_a, 1, F, 0) == 0x11, "the first g again");
    CHECK(call(&cpu_a, 2, F2, 0) == 0x12, "f2 and the first g again");
    if (revive) CHECK(m.stats.translations == t3, "the first g was revived, not translated (%llu)", (unsigned long long)(m.stats.translations - t3));

    /* Many rounds, and a write by the host (a device's DMA) now and then. */
    int bad = 0;
    for (int i = 0; i < 4000; i++) {
        uint32_t v = 0x10 + (uint32_t)(i % 7);
        if (i % 5 == 4) {
            put(G, ADD_X0(v));
            hv_dma_written(&m, G, 4);
        } else {
            guest_write(&cpu_a, G, ADD_X0(v));
        }
        if (call(&cpu_a, i & 1, F, 0) != 1 + v) bad++;
        if (call(&cpu_a, 2, F2, i & 2 ? 1 : 0) != (i & 2 ? 0x100 : 2 + v)) bad++;
        if (i % 3 == 0 && call(&cpu_a, 0, H, 0) != 0x100) bad++;
    }
    CHECK(!bad, "%d wrong results in 4000 rounds", bad);
    put(WRITER + 4, IC_IVAU_X2);
    hv_dma_written(&m, WRITER, 16);
    printf("  single: %llu translations, %llu revived, %llu drops, %llu stale as published\n", (unsigned long long)(m.stats.translations - t0),
           (unsigned long long)(m.stats.revived - r0), (unsigned long long)(m.stats.flush_smc - s0), (unsigned long long)m.stats.revive_born_stale);
}

/* A second CPU runs the page's code all the while. What it sees during a change may be either g; what the CPU
   that made the change sees after it must be the new one. */
static volatile int g_stop;
static int b_bad, b_runs;

static void *other(void *arg) {
    (void)arg;
    hv_self = &cpu_b;
    while (!__atomic_load_n(&g_stop, __ATOMIC_SEQ_CST)) {
        uint64_t v = call(&cpu_b, b_runs & 1, F, 0);
        if (v < 0x11 || v > 0x17) b_bad++;
        if (call(&cpu_b, 0, H, 0) != 0x100) b_bad++;
        v = call(&cpu_b, 2, F2, 0);
        if (v < 0x12 || v > 0x18) b_bad++;
        b_runs++;
    }
    return NULL;
}

/* What the engine holds for f and g, when a result is wrong: which link is the stale one. */
static void dump_state(void) {
#ifdef HV_DBG_RING
    uint32_t end = hv_dbg_at;
    for (uint32_t k = end > 160 ? end - 160 : 0; k < end; k++) {
        HvDbgEvent *e = &hv_dbg_ring[k & 4095];
        printf("    %llu cpu %d %s: %llx %llx %llx\n", (unsigned long long)e->t, e->cpu, e->what ? e->what : "?", (unsigned long long)e->a, (unsigned long long)e->b, (unsigned long long)e->c);
    }
#endif
    HvRam *r = hv_ram_lookup(&m, P);
    uint64_t pg = (P - r->pa) >> HV_TLB_PAGE_BITS;
    uint32_t now;
    memcpy(&now, mem + (G - BASE), 4);
    printf("    page: gen %u code_pages %d mask %llx xlating %d; g is now %x\n", r->page_gen[pg], r->code_pages[pg], (unsigned long long)r->code_mask[pg], (int)r->xlating[pg], now);
    for (int w = 0; w < 2; w++) {
        uint64_t va = w ? G : F;
        uint32_t h = hv_block_hash(va, va, HV_BLOCK_EL1 | HV_BLOCK_SPX);
        for (uint32_t flags = 0; flags < 16; flags++) {
            h = hv_block_hash(va, va, flags);
            for (HvBlock *b = m.hash[h]; b; b = b->next) {
                if (b->va != va || b->flags != flags) continue;
                printf("    %s block %p flags %x gen %u%s sum %08x%08x insns %u code %p\n", w ? "g" : "f", (void *)b, b->flags, b->gen, b->gen == r->page_gen[pg] ? " (current)" : "", b->sum_hi, b->sum_lo,
                       b->ninsns, (void *)b->code);
                if (w) continue;
                for (uint32_t idx = 0; idx < m.nslots; idx++) {
                    uint32_t *site = hv_slot_dec(&m, m.slot_site[idx]);
                    if (!site || site < b->code || site >= b->code + 256) continue;
                    uint64_t to = *HV_SLOT(&m, idx);
                    HvBlock *t = to ? hv_block_at(&m, (const uint32_t *)(uintptr_t)to) : NULL;
                    printf("      slot %u: to %llx", idx, (unsigned long long)to);
                    if (t) printf(" = block %p va %llx gen %u sum %08x%08x", (void *)t, (unsigned long long)t->va, t->gen, t->sum_hi, t->sum_lo);
                    printf("; listed %llx tseg %u xa %llx; site insn %08x\n", (unsigned long long)m.slot_xpa[idx], m.slot_tseg[idx], (unsigned long long)m.slot_xa[idx], *site);
                }
            }
        }
    }
}

static void test_two_cpus(void) {
    uint64_t t0 = m.stats.translations, r0 = m.stats.revived;
    pthread_t t;
    pthread_create(&t, NULL, other, NULL);
    int bad = 0, rounds = 20000;
    for (int i = 0; i < rounds; i++) {
        uint32_t v = 0x10 + (uint32_t)(i % 7);
        HV_DBG("test: A writes g, round, value", i, v, 0);
        guest_write(&cpu_a, G, ADD_X0(v));
        HV_DBG("test: A wrote; calls f", i, v, 0);
        uint64_t got = call(&cpu_a, i & 1, F, 0);
        HV_DBG("test: A got", got, 1 + v, 0);
        if (got != 1 + v) {
            if (bad++ < 1) {
                printf("  round %d: f gave %llx, want %x (how %d)\n", i, (unsigned long long)got, 1 + v, i & 1);
                dump_state();
            }
        }
        if (i % 4 == 0 && (got = call(&cpu_a, 2, F2, 0)) != 2 + v) {
            if (bad++ < 5) printf("  round %d: f2 gave %llx, want %x\n", i, (unsigned long long)got, 2 + v);
        }
    }
    __atomic_store_n(&g_stop, 1, __ATOMIC_SEQ_CST);
    pthread_join(t, NULL);
    __atomic_store_n(&cpu_b.pc_state->tlb_live, 0, __ATOMIC_SEQ_CST); /* parked: its thread is gone */
    CHECK(!bad, "%d wrong results on the writing CPU in %d rounds", bad, rounds);
    CHECK(!b_bad, "%d results on the other CPU that no g could give, in %d runs", b_bad, b_runs);
    CHECK(b_runs > 0, "the other CPU ran");
    printf("  two CPUs: %d rounds, %d runs on the other, %llu translations, %llu revived, %llu stale as published\n", rounds, b_runs,
           (unsigned long long)(m.stats.translations - t0), (unsigned long long)(m.stats.revived - r0), (unsigned long long)m.stats.revive_born_stale);
}

/* The code cache taken back segment by segment under it all: revived blocks and their exits go as any do. */
static void test_evictions(void) {
    uint64_t e0 = m.stats.evictions;
    int bad = 0;
    /* Fill the cache with other code: page 3 on, each word a block of its own (x0 += 1; b next ... ret at the end). */
    for (int round = 0; round < 3000 && m.stats.evictions - e0 < 60; round++) {
        for (uint32_t pg = 3; pg < NPAGES; pg++) {
            uint64_t base = BASE + pg * PG;
            for (uint32_t i = 0; i < PG / 8; i++) {
                put(base + 8 * i, ADD_X0(((uint32_t)round * 977u + pg * 131u + i) & 0xfff)); /* new instructions every round: nothing to revive */
                put(base + 8 * i + 4, i + 1 < PG / 8 ? B(base + 8 * i + 4, base + 8 * i + 8) : RET);
            }
            put(base + PG - 4, RET);
            hv_dma_written(&m, base, PG);
            cpu_a.x[0] = 0, cpu_a.x[10] = base;
            if (run(&cpu_a, MAIN_A) != 0) bad++;
        }
        uint32_t v = 0x10 + (uint32_t)(round % 7);
        guest_write(&cpu_a, G, ADD_X0(v));
        if (call(&cpu_a, 1, F, 0) != 1 + v) bad++;
        if (call(&cpu_a, 2, F2, 0) != 2 + v) bad++;
        if (call(&cpu_a, 0, H, 0) != 0x100) bad++;
    }
    CHECK(!bad, "%d wrong results", bad);
    CHECK(m.stats.evictions - e0 >= 60, "the cache filled and segments were taken back (%llu)", (unsigned long long)(m.stats.evictions - e0));
    printf("  evictions: %llu\n", (unsigned long long)(m.stats.evictions - e0));
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    alarm(300); /* a hang is a failure */
    revive = getenv("HV_REVIVE") && atoi(getenv("HV_REVIVE"));
    if (hv_machine_init(&m, 16 << 20) != 0) return 1;
    mem = mmap(NULL, NPAGES * PG, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (mem == MAP_FAILED) return 1;
    hv_machine_add_ram(&m, BASE, NPAGES * PG, mem, 0);
    for (uint64_t at = BASE; at < BASE + 3 * PG; at += 4) put(at, NOP);
    put(MAIN_A, BLR_X10), put(MAIN_A + 4, B(MAIN_A + 4, STOP));
    put(MAIN_B, BL(MAIN_B, F)), put(MAIN_B + 4, B(MAIN_B + 4, STOP));
    put(WRITER, STR_W1_X2), put(WRITER + 4, IC_IVAU_X2), put(WRITER + 8, DSB_ISH), put(WRITER + 12, ISB), put(WRITER + 16, B(WRITER + 16, STOP));
    put(MAIN_C, BL(MAIN_C, F2)), put(MAIN_C + 4, B(MAIN_C + 4, STOP));
    m.stop_pc = STOP;
    put(F, ADD_X0(1)), put(F + 4, B(F + 4, G));
    put(G, ADD_X0(0x10)), put(G + 4, RET);
    put(H, ADD_X0(0x100)), put(H + 4, RET);
    put(F2, CBNZ_X3(F2, H)), put(F2 + 4, ADD_X0(2)), put(F2 + 8, B(F2 + 8, G));
    put(K, ADD_X0(0x200)), put(K + 4, RET);
    hv_cpu_reset_index(&cpu_a, &m, 0);
    hv_cpu_reset_index(&cpu_b, &m, 1);
    hv_self = &cpu_a;

#define T(call) \
    do { \
        int f0 = failures; \
        call; \
        printf("%-16s: %s\n", #call, failures == f0 ? "ok" : "FAIL"); \
    } while (0)
    T(test_single());
    T(test_two_cpus());
    T(test_evictions());
    printf("%d checks, %d failures (HV_REVIVE=%d)\n", checks, failures, revive);
    return failures != 0;
}
