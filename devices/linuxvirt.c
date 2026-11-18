// SPDX-License-Identifier: GPL-2.0-or-later
#include "linuxvirt.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include "fdt.h"
#include "sysreg.h"
#include "timer.h"
#include "vmapple.h"

/* The same addresses as the virtual Mac (devices/vmapple.h). */
#define LV_GIC_DIST   VMAPPLE_GIC_DIST
#define LV_GIC_REDIST VMAPPLE_GIC_REDIST
#define LV_UART       VMAPPLE_UART
#define LV_RTC        VMAPPLE_RTC
#define LV_PCIE_ECAM  VMAPPLE_PCIE_ECAM
#define LV_ECAM_SIZE  0x10000000ull
#define LV_PCIE_MMIO  VMAPPLE_PCIE_MMIO
#define LV_MMIO_SIZE  0x1fff0000ull
#define LV_MEM        VMAPPLE_MEM
#define LV_SPI_UART   VMAPPLE_IRQ_UART
#define LV_SPI_RTC    VMAPPLE_IRQ_RTC
#define LV_SPI_PCIE   VMAPPLE_IRQ_PCIE /* slot s raises SPI 0x20 + s (devices/pcie.c) */
#define LV_PCI_SLOTS  8

#define PH_GIC 1
#define PH_CLK 2

static uint64_t tick(HvMachine *m, HvCpu *cpu) {
    HvLinuxVirt *b = m->tick_opaque;
    if (cpu->index != 0) return hv_timer_update(b->gic, cpu); /* its own timer: the GIC's lock only */
    hv_dev_lock(m);
    uint64_t deadline = hv_timer_update(b->gic, cpu);
    hv_pl031_tick(b->rtc);
    if (b->blk) hv_virtio_blk_poll(b->blk);
    if (b->keyboard) hv_virtio_input_poll(b->keyboard);
    if (b->mouse) hv_virtio_input_poll(b->mouse);
    hv_dev_unlock(m);
    return deadline;
}

int hv_linuxvirt_create(HvLinuxVirt *b, HvMachine *m, HvCpu *cpus, const HvLinuxVirtConfig *c) {
    memset(b, 0, sizeof *b);
    b->m = m;
    b->cpu = cpus;
    b->ncpus = c->ncpus < 1 ? 1 : c->ncpus > HV_MAX_CPUS ? HV_MAX_CPUS : c->ncpus;
    b->ram_size = c->ram_size;
    b->ram = mmap(NULL, c->ram_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (b->ram == MAP_FAILED) {
        b->ram = NULL;
        return -1;
    }
    hv_machine_add_ram(m, LV_MEM, c->ram_size, b->ram, 0);
    b->gic = hv_gicv3_create(m, LV_GIC_DIST, LV_GIC_REDIST);
    b->uart = hv_pl011_create(m, b->gic, LV_UART, 32 + LV_SPI_UART);
    b->rtc = hv_pl031_create(m, b->gic, LV_RTC, 32 + LV_SPI_RTC);
    b->pcie = hv_pcie_create(m, b->gic, LV_PCIE_ECAM, LV_ECAM_SIZE, LV_PCIE_MMIO, LV_MMIO_SIZE, 32 + LV_SPI_PCIE);
    if (c->disk && !(b->blk = hv_virtio_blk_create(m, b->pcie, c->disk, HV_VBLK_PLAIN))) return -1;
    if (c->mac) b->net = hv_virtio_net_create(m, b->pcie, c->mac);
    b->keyboard = hv_virtio_input_create(m, b->pcie, HV_VINPUT_KEYBOARD);
    b->mouse = hv_virtio_input_create(m, b->pcie, HV_VINPUT_MOUSE);
    if (c->fb_width && c->fb_height) {
        uint64_t bytes = ((uint64_t)c->fb_width * c->fb_height * 4 + 0x1fffff) & ~0x1fffffull;
        if (bytes >= c->ram_size / 2) return -1;
        b->fb_width = c->fb_width, b->fb_height = c->fb_height;
        b->fb_pa = LV_MEM + c->ram_size - bytes;
        b->fb = b->ram + (b->fb_pa - LV_MEM);
    }
    m->tick = tick;
    m->tick_opaque = b;
    return 0;
}

static void put_le(uint8_t *p, uint64_t v, int n) {
    for (int i = 0; i < n; i++) p[i] = (uint8_t)(v >> (8 * i));
}
static uint64_t get_le(const uint8_t *p, int n) {
    uint64_t v = 0;
    for (int i = n - 1; i >= 0; i--) v = v << 8 | p[i];
    return v;
}

static uint8_t *make_dtb(HvLinuxVirt *b, const HvLinuxVirtConfig *c, size_t *len) {
    HvFdt f;
    hv_fdt_init(&f);
    uint64_t usable = b->fb ? b->fb_pa - LV_MEM : b->ram_size;
    hv_fdt_begin(&f, "");
    hv_fdt_str(&f, "compatible", "linux,dummy-virt");
    hv_fdt_str(&f, "model", "Henge Linux machine");
    hv_fdt_u32(&f, "#address-cells", 2);
    hv_fdt_u32(&f, "#size-cells", 2);
    hv_fdt_u32(&f, "interrupt-parent", PH_GIC);

    char uart_path[64];
    snprintf(uart_path, sizeof uart_path, "/pl011@%llx", (unsigned long long)LV_UART);
    hv_fdt_begin(&f, "chosen");
    hv_fdt_str(&f, "bootargs", c->cmdline ? c->cmdline : "console=ttyAMA0");
    hv_fdt_str(&f, "stdout-path", uart_path);
    if (b->initrd_size) {
        hv_fdt_u64(&f, "linux,initrd-start", b->initrd_pa);
        hv_fdt_u64(&f, "linux,initrd-end", b->initrd_pa + b->initrd_size);
    }
    uint8_t seed[64];
    for (size_t i = 0; i < sizeof seed; i += 4) put_le(seed + i, (uint32_t)rand() ^ (uint32_t)hv_counter(), 4);
    hv_fdt_prop(&f, "rng-seed", seed, sizeof seed);
    hv_fdt_u64(&f, "kaslr-seed", (uint64_t)rand() << 32 ^ hv_counter());
    if (b->fb) {
        char name[48];
        snprintf(name, sizeof name, "framebuffer@%llx", (unsigned long long)b->fb_pa);
        hv_fdt_begin(&f, name);
        hv_fdt_str(&f, "compatible", "simple-framebuffer");
        uint32_t reg[4] = {(uint32_t)(b->fb_pa >> 32), (uint32_t)b->fb_pa, 0, b->fb_width * b->fb_height * 4};
        hv_fdt_cells(&f, "reg", reg, 4);
        hv_fdt_u32(&f, "width", b->fb_width);
        hv_fdt_u32(&f, "height", b->fb_height);
        hv_fdt_u32(&f, "stride", b->fb_width * 4);
        hv_fdt_str(&f, "format", "a8r8g8b8");
        hv_fdt_str(&f, "status", "okay");
        hv_fdt_end(&f);
    }
    hv_fdt_end(&f);

    char name[48];
    snprintf(name, sizeof name, "memory@%llx", (unsigned long long)LV_MEM);
    hv_fdt_begin(&f, name);
    hv_fdt_str(&f, "device_type", "memory");
    uint32_t mem[4] = {(uint32_t)(LV_MEM >> 32), (uint32_t)LV_MEM, (uint32_t)(usable >> 32), (uint32_t)usable};
    hv_fdt_cells(&f, "reg", mem, 4);
    hv_fdt_end(&f);

    hv_fdt_begin(&f, "cpus");
    hv_fdt_u32(&f, "#address-cells", 1);
    hv_fdt_u32(&f, "#size-cells", 0);
    for (int i = 0; i < b->ncpus; i++) {
        snprintf(name, sizeof name, "cpu@%x", i);
        hv_fdt_begin(&f, name);
        hv_fdt_str(&f, "device_type", "cpu");
        hv_fdt_str(&f, "compatible", "arm,arm-v8");
        hv_fdt_u32(&f, "reg", (uint32_t)i); /* MPIDR Aff0 (hv_linuxvirt_boot) */
        hv_fdt_str(&f, "enable-method", "psci");
        hv_fdt_end(&f);
    }
    hv_fdt_end(&f);

    hv_fdt_begin(&f, "psci");
    static const char psci_compat[] = "arm,psci-1.0\0arm,psci-0.2";
    hv_fdt_strs(&f, "compatible", psci_compat, sizeof psci_compat);
    hv_fdt_str(&f, "method", "hvc");
    hv_fdt_end(&f);

    hv_fdt_begin(&f, "timer");
    hv_fdt_str(&f, "compatible", "arm,armv8-timer");
    /* Secure and non-secure physical, virtual, hypervisor PPIs (29, 30, 27, 26), level high. */
    uint32_t timer[12] = {1, 13, 4, 1, 14, 4, 1, 11, 4, 1, 10, 4};
    hv_fdt_cells(&f, "interrupts", timer, 12);
    hv_fdt_empty(&f, "always-on");
    hv_fdt_end(&f);

    snprintf(name, sizeof name, "intc@%llx", (unsigned long long)LV_GIC_DIST);
    hv_fdt_begin(&f, name);
    hv_fdt_str(&f, "compatible", "arm,gic-v3");
    hv_fdt_u32(&f, "#interrupt-cells", 3);
    hv_fdt_u32(&f, "#address-cells", 2);
    hv_fdt_u32(&f, "#size-cells", 2);
    hv_fdt_empty(&f, "interrupt-controller");
    hv_fdt_u32(&f, "#redistributor-regions", 1);
    uint64_t rd_size = (uint64_t)b->ncpus * 0x20000;
    uint32_t gic_reg[8] = {0, (uint32_t)LV_GIC_DIST, 0, 0x10000, 0, (uint32_t)LV_GIC_REDIST, 0, (uint32_t)rd_size};
    hv_fdt_cells(&f, "reg", gic_reg, 8);
    uint32_t maint[3] = {1, 9, 4};
    hv_fdt_cells(&f, "interrupts", maint, 3);
    hv_fdt_u32(&f, "phandle", PH_GIC);
    hv_fdt_end(&f);

    hv_fdt_begin(&f, "apb-pclk");
    hv_fdt_str(&f, "compatible", "fixed-clock");
    hv_fdt_u32(&f, "#clock-cells", 0);
    hv_fdt_u32(&f, "clock-frequency", 24000000);
    hv_fdt_str(&f, "clock-output-names", "clk24mhz");
    hv_fdt_u32(&f, "phandle", PH_CLK);
    hv_fdt_end(&f);

    static const char primecell_uart[] = "arm,pl011\0arm,primecell";
    hv_fdt_begin(&f, uart_path + 1);
    hv_fdt_strs(&f, "compatible", primecell_uart, sizeof primecell_uart);
    uint32_t uart_reg[4] = {0, (uint32_t)LV_UART, 0, 0x1000};
    hv_fdt_cells(&f, "reg", uart_reg, 4);
    uint32_t uart_irq[3] = {0, LV_SPI_UART, 4};
    hv_fdt_cells(&f, "interrupts", uart_irq, 3);
    uint32_t clk2[2] = {PH_CLK, PH_CLK};
    hv_fdt_cells(&f, "clocks", clk2, 2);
    static const char uart_clocks[] = "uartclk\0apb_pclk";
    hv_fdt_strs(&f, "clock-names", uart_clocks, sizeof uart_clocks);
    hv_fdt_u32(&f, "arm,primecell-periphid", 0x00041011);
    hv_fdt_end(&f);

    static const char primecell_rtc[] = "arm,pl031\0arm,primecell";
    snprintf(name, sizeof name, "pl031@%llx", (unsigned long long)LV_RTC);
    hv_fdt_begin(&f, name);
    hv_fdt_strs(&f, "compatible", primecell_rtc, sizeof primecell_rtc);
    uint32_t rtc_reg[4] = {0, (uint32_t)LV_RTC, 0, 0x1000};
    hv_fdt_cells(&f, "reg", rtc_reg, 4);
    uint32_t rtc_irq[3] = {0, LV_SPI_RTC, 4};
    hv_fdt_cells(&f, "interrupts", rtc_irq, 3);
    hv_fdt_u32(&f, "clocks", PH_CLK);
    hv_fdt_str(&f, "clock-names", "apb_pclk");
    hv_fdt_u32(&f, "arm,primecell-periphid", 0x00041031);
    hv_fdt_end(&f);

    snprintf(name, sizeof name, "pcie@%llx", (unsigned long long)LV_PCIE_ECAM);
    hv_fdt_begin(&f, name);
    hv_fdt_str(&f, "compatible", "pci-host-ecam-generic");
    hv_fdt_str(&f, "device_type", "pci");
    hv_fdt_u32(&f, "#address-cells", 3);
    hv_fdt_u32(&f, "#size-cells", 2);
    hv_fdt_u32(&f, "#interrupt-cells", 1);
    uint32_t bus_range[2] = {0, (uint32_t)(LV_ECAM_SIZE >> 20) - 1};
    hv_fdt_cells(&f, "bus-range", bus_range, 2);
    uint32_t pci_reg[4] = {0, (uint32_t)LV_PCIE_ECAM, 0, (uint32_t)LV_ECAM_SIZE};
    hv_fdt_cells(&f, "reg", pci_reg, 4);
    /* 32-bit memory space, identity mapped. */
    uint32_t ranges[7] = {0x02000000, 0, (uint32_t)LV_PCIE_MMIO, 0, (uint32_t)LV_PCIE_MMIO, 0, (uint32_t)LV_MMIO_SIZE};
    hv_fdt_cells(&f, "ranges", ranges, 7);
    hv_fdt_empty(&f, "dma-coherent");
    /* INTx: every pin of slot s is SPI 0x20 + s (devices/pcie.c, no swizzle). */
    uint32_t mask[4] = {0xf800, 0, 0, 0};
    hv_fdt_cells(&f, "interrupt-map-mask", mask, 4);
    uint32_t map[LV_PCI_SLOTS * 10];
    for (int s = 0; s < LV_PCI_SLOTS; s++) {
        uint32_t *e = map + s * 10;
        e[0] = (uint32_t)s << 11, e[1] = 0, e[2] = 0, e[3] = 0; /* child: devfn, pin (masked) */
        e[4] = PH_GIC, e[5] = 0, e[6] = 0;                    /* parent and its unit address */
        e[7] = 0, e[8] = LV_SPI_PCIE + (uint32_t)s, e[9] = 4;  /* SPI, level high */
    }
    hv_fdt_cells(&f, "interrupt-map", map, LV_PCI_SLOTS * 10);
    hv_fdt_end(&f);

    hv_fdt_end(&f);
    return hv_fdt_finish(&f, len);
}

static int fail(char *err, size_t errlen, const char *msg) {
    if (err && errlen) snprintf(err, errlen, "%s", msg);
    return -1;
}

int hv_linuxvirt_boot(HvLinuxVirt *b, const HvLinuxVirtConfig *c, const uint8_t *image, size_t image_len,
                      const uint8_t *initrd, size_t initrd_len, char *err, size_t errlen) {
    /* arm64 Image header (Documentation/arch/arm64/booting.rst). */
    if (image_len < 64 || get_le(image + 56, 4) != 0x644d5241)
        return fail(err, errlen, "Not an uncompressed arm64 kernel Image (no ARM\\x64 magic).");
    uint64_t text_offset = get_le(image + 8, 8), image_size = get_le(image + 16, 8);
    if (!image_size) text_offset = 0x80000, image_size = image_len + (16u << 20); /* pre-3.17 header */
    uint64_t usable = b->fb ? b->fb_pa - LV_MEM : b->ram_size;
    b->kernel_pa = LV_MEM + text_offset; /* the base is 2 MB aligned */
    uint64_t kernel_end = text_offset + image_size;
    /* The device tree in the last 2 MB below the framebuffer, the initrd under it. */
    uint64_t dtb_off = (usable - (2u << 20)) & ~0x1fffffull;
    uint64_t initrd_off = initrd_len ? (dtb_off - initrd_len) & ~0xfffull : dtb_off;
    if (image_len > image_size || kernel_end > initrd_off)
        return fail(err, errlen, "The kernel and initrd do not fit in the guest's RAM.");
    memcpy(b->ram + text_offset, image, image_len);
    if (initrd_len) memcpy(b->ram + initrd_off, initrd, initrd_len);
    b->initrd_pa = LV_MEM + initrd_off, b->initrd_size = initrd_len;
    b->dtb_pa = LV_MEM + dtb_off;
    size_t dtb_len = 0;
    uint8_t *dtb = make_dtb(b, c, &dtb_len);
    if (!dtb || dtb_len > (2u << 20)) {
        free(dtb);
        return fail(err, errlen, "Could not build the device tree.");
    }
    memcpy(b->ram + dtb_off, dtb, dtb_len);
    free(dtb);

    b->m->cntvoff = hv_counter();
    for (int i = 0; i < b->ncpus; i++) {
        HvCpu *cpu = &b->cpu[i];
        hv_cpu_reset_index(cpu, b->m, (uint32_t)i);
        cpu->mpidr = 0x80000000u | (uint32_t)i;
        cpu->off = i != 0; /* PSCI CPU_ON starts the others */
    }
    HvCpu *c0 = &b->cpu[0];
    c0->pc = b->kernel_pa;
    c0->x[0] = b->dtb_pa;
    c0->x[1] = c0->x[2] = c0->x[3] = 0;
    return 0;
}

void hv_linuxvirt_destroy(HvLinuxVirt *b) {
    hv_virtio_blk_destroy(b->blk);
    if (b->keyboard) { pthread_mutex_destroy(&b->keyboard->mu); free(b->keyboard); }
    if (b->mouse) { pthread_mutex_destroy(&b->mouse->mu); free(b->mouse); }
    if (b->gic) { pthread_mutex_destroy(&b->gic->mu); free(b->gic); }
    free(b->net);
    free(b->pcie);
    free(b->uart);
    free(b->rtc);
    if (b->ram) munmap(b->ram, b->ram_size);
    memset(b, 0, sizeof *b);
}
