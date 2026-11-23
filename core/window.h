#ifndef HENGEVM_WINDOW_H
#define HENGEVM_WINDOW_H

#include <signal.h>
#include <stdint.h>

#include "cpu.h"

/* HV_EL0_WINDOW=bits: guest user memory mapped straight into this process (see window.c). */

struct HvMachine;

int hv_window_enabled(void);                  /* bits, or 0 */
/* Up front, before any code is translated: every CPU's window, or none (the knob goes off). */
int hv_window_reserve(struct HvMachine *m, int ncpus);
int hv_window_attach(HvCpu *cpu);             /* cpu's window (made if missing: tests), emptied; 0 on success or when off */
void hv_window_detach(HvCpu *cpu);
/* What a window has done, under its lock (hv_window_counts adds the CPUs' up). The four kinds of drop come as
   pairs: times something went, and pages. */
enum {
    HV_WC_FAULTS, HV_WC_MISSES,          /* faults in a window; those sent to the access's TLB path */
    HV_WC_REMAPS, HV_WC_REMAP_PAGES,     /* vm_remap calls, and the pages they mapped (more than calls: fault-around) */
    HV_WC_MPROTECTS, HV_WC_UPGRADES,     /* mprotect calls; those that made a read-only page writable on a write */
    HV_WC_UNMAPS, HV_WC_UNMAP_PAGES,     /* mmap calls that took pages out, and the pages */
    HV_WC_EVICTIONS, HV_WC_EVICT_PAGES,  /* a full window gave up some pages */
    HV_WC_RESETS, HV_WC_RESET_PAGES,     /* a full window gave up all of them (HV_WINDOW_EVICT=0) */
    HV_WC_SWITCH_DROPS, HV_WC_SWITCH_PAGES, /* a process switch */
    HV_WC_TLBI_DROPS, HV_WC_TLBI_PAGES,     /* a TLBI by page or range, from any CPU */
    HV_WC_FULL_DROPS, HV_WC_FULL_PAGES,     /* a whole-TLB flush */
    HV_WC_USER_DROPS, HV_WC_USER_PAGES,     /* a flush of the user half */
    HV_WC_CODE_SCANS, HV_WC_CODE_PAGES,  /* pages that became code: looks through the window, pages made read-only */
    HV_WC_N
};
void hv_window_counts(struct HvMachine *m, uint64_t out[HV_WC_N], uint32_t *held); /* held: pages in the windows now */
void hv_window_drop_all(HvCpu *cpu, int why); /* why: HV_WC_SWITCH_DROPS, HV_WC_FULL_DROPS or HV_WC_USER_DROPS */
/* Every page but those in the 32 MB chunks set in keep (bit c: lo + c * 32 MB), as a process switch keeps them. */
void hv_window_drop_except(HvCpu *cpu, uint64_t lo, const uint64_t *keep, uint32_t nchunks);
void hv_window_drop_range(HvCpu *cpu, uint64_t va, uint64_t len); /* any CPU's thread */
void hv_window_protect(HvCpu *cpu, const uint8_t *host_page);     /* page became code: read-only wherever mapped */
/* A host fault: 1 if it was an access through a window (handled: the context now retries it, or goes to the
   access's TLB path), 0 if not. From the SIGBUS/SIGSEGV handler. */
int hv_window_signal(struct HvMachine *m, siginfo_t *info, void *ctx);

#ifndef __APPLE__
/* Linux hosts (tests): host memory [host, host + size) is file fd's, from offset 0; the window maps it from there. */
void hv_window_linux_backing(const uint8_t *host, uint64_t size, int fd);
#endif

#endif
