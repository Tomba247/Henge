# The Linux machine

HengeVM's engine runs arm64 code with its own JIT, MMU and devices, so it is not
tied to macOS. `devices/linuxvirt.c` puts a generic arm64 machine for Linux
guests on the same engine and devices as the virtual Mac: a real Linux kernel,
so systemd, snaps, containers and kernel modules behave as on any arm64 computer.

## The machine

Same addresses as the virtual Mac (`devices/vmapple.h`), booted the way Linux
boots on any arm64 board: the kernel `Image` is loaded directly at the start of
RAM, a device tree describing the machine is generated (`devices/fdt.c`) and
handed over in x0, and there is no boot ROM.

| Device | Address | Linux driver |
| --- | --- | --- |
| GICv3 distributor / redistributors | 0x10000000 / 0x10010000 | `arm,gic-v3` |
| Generic timer (virtual timer, PPI 27) | — | `arm,armv8-timer` |
| PL011 UART (SPI 1) | 0x20010000 | `arm,pl011` (`ttyAMA0`) |
| PL031 RTC (SPI 2) | 0x20050000 | `arm,pl031` |
| PSCI 1.0 over HVC | — | `arm,psci-1.0` (CPU_ON, power off, reset) |
| PCIe ECAM / 32-bit window | 0x40000000 / 0x50000000 | `pci-host-ecam-generic`, INTx: slot s is SPI 0x20 + s |
| virtio-pci block, network, input | PCIe | `virtio_blk`, `virtio_net`, `virtio_input` |
| simple-framebuffer (optional) | top of RAM | `simpledrm` / `simplefb` |
| RAM | 0x70000000 | — |

The CPU starts at EL1 (HengeVM plays EL2), as on a VM host without nested
virtualization: Linux reports "CPU: All CPU(s) started at EL1" and has no KVM.

## Trying it without a phone

`tools/hengelinux` boots a kernel from the command line with the serial console
on the terminal (Ctrl-A x quits). `make linux-test` builds it for an arm64 Linux
host; `host/linux/` stands in for the few Mach and Apple headers the engine
includes. On an x86 machine it runs under `qemu-aarch64` (a JIT inside a JIT, so
very slow; `HV_CLOCK_DIV=20` slows the guest's clock so a 250 Hz kernel is not
all timer ticks):

```sh
make linux-test
qemu-aarch64 -cpu max build/linux/hengelinux --kernel Image --initrd initrd.gz \
    --append "console=ttyAMA0 rdinit=/init"
HV_CLOCK_DIV=20 qemu-aarch64 -cpu max build/linux/hengelinux --kernel Image \
    --disk ubuntu.img --cpus 2 --ram 2048 --append "console=ttyAMA0 root=/dev/vda rw"
```

`--kernel` takes an uncompressed arm64 `Image` (Ubuntu's `vmlinuz` is gzip:
`zcat vmlinuz > Image`). Without an initramfs, use `root=/dev/vda`, not a label.

## Tested so far (qemu-aarch64 on x86, 2026-11-18)

Kernel: Ubuntu 24.04's stock `linux-image-unsigned-6.8.0-149-generic` (arm64).

- Boots to a BusyBox shell from an initramfs; finds the PCIe host bridge and the
  virtio devices; powers off through PSCI.
- Two CPUs (PSCI CPU_ON, the second redistributor), and an ext4 root file system
  on virtio-blk.
- The simple-framebuffer: Linux's `simpledrm` driver takes it and the text
  console draws on it (`HV_FB_DUMP=file.ppm` saves it when the tool stops).
- One engine fix was needed: the translator read macOS's commpage to inline
  counter reads, which a Linux host does not have (`core/translate.c`).

Not tried yet: a phone, input, network, sound.

## What the app needs

- **Machine:** build `linuxvirt` instead of the virtual Mac in `hv_vm_create`
  when a kernel is given. The input, disk, network and sound plumbing is the
  same devices.
- **Display:** the simple-framebuffer is 32-bit BGRA in guest RAM, the format
  `hv_vm_boot_display` already hands `BootDisplay.swift`.
- **Input:** the Mac's xHCI controller with its keyboard and Magic Trackpad 2 —
  Linux drives them with `xhci_hcd`, `usbhid` and `hid-magicmouse` (libinput then
  gives two-finger scrolling and gestures) — or virtio-input.
- **Network:** virtio-net on the existing user-mode network (`host/net_slirp.c`).
- **The system:** Ubuntu's arm64 cloud root file system
  (`noble-server-cloudimg-arm64-root.tar.xz`) as an ext4 disk image, with the
  kernel's modules added. A CI job can build the disk image (with a desktop
  installed) so the app only downloads it.
- **Graphics acceleration (later):** a virtio-gpu device; with virglrenderer on
  ANGLE/Metal, OpenGL in the guest would run on the phone's GPU.
