// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef HENGEVM_LINUXVIRT_H
#define HENGEVM_LINUXVIRT_H

#include <stdint.h>

#include "machine.h"
#include "gicv3.h"
#include "pl011.h"
#include "pl031.h"
#include "pcie.h"
#include "virtio_blk.h"
#include "virtio_input.h"
#include "virtio_net.h"
#include "disk.h"

/* A generic arm64 machine for Linux guests, on the same engine and devices as
   the virtual Mac and at the same addresses (devices/vmapple.h), but booted the
   way Linux boots on any arm64 board: the kernel Image is loaded directly, a
   device tree describing the machine goes in x0, and there is no boot ROM.

   Devices Linux drives with its stock drivers: GICv3, the generic timer, PL011
   (console), PL031 (clock), PSCI over HVC (CPU_ON, shutdown, reset), a generic
   ECAM PCIe host with INTx, and modern virtio-pci block, network and input. An
   optional simple-framebuffer at the top of RAM is the display. */
typedef struct HvLinuxVirt {
    HvMachine *m;
    HvCpu *cpu; /* array of ncpus */
    int ncpus;
    HvGic *gic;
    HvPl011 *uart;
    HvPl031 *rtc;
    HvPcie *pcie;
    HvVirtioBlk *blk;
    HvVirtioNet *net;
    HvVirtioInput *keyboard, *mouse;
    uint8_t *ram;
    uint64_t ram_size;
    /* Framebuffer (simple-framebuffer, a8r8g8b8) carved from the top of RAM; 0 x 0 for none. */
    uint32_t fb_width, fb_height;
    uint64_t fb_pa;
    uint8_t *fb;
    /* Where the boot images went (guest physical). */
    uint64_t kernel_pa, dtb_pa, initrd_pa, initrd_size;
} HvLinuxVirt;

typedef struct HvLinuxVirtConfig {
    int ncpus;
    uint64_t ram_size;
    HvDisk *disk;           /* virtio-blk root disk, or NULL */
    const uint8_t *mac;     /* virtio-net MAC, or NULL for no network device */
    uint32_t fb_width, fb_height;
    const char *cmdline;    /* kernel command line */
} HvLinuxVirtConfig;

int hv_linuxvirt_create(HvLinuxVirt *b, HvMachine *m, HvCpu *cpus, const HvLinuxVirtConfig *c);
/* Loads an uncompressed arm64 Image (and an initrd, may be NULL), writes the
   device tree and puts every CPU at reset: CPU 0 at the kernel with x0 = DTB,
   the others off until PSCI CPU_ON. Returns 0, or -1 with err filled in. */
int hv_linuxvirt_boot(HvLinuxVirt *b, const HvLinuxVirtConfig *c, const uint8_t *image, size_t image_len,
                      const uint8_t *initrd, size_t initrd_len, char *err, size_t errlen);
void hv_linuxvirt_destroy(HvLinuxVirt *b);

#endif
