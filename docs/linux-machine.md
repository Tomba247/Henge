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

Not tried yet: a phone, input, network, sound, the desktop.

## An Ubuntu machine

`tools/linux/make-ubuntu-disk.sh` makes a folder the app brings in, from Ubuntu
as it is: the arm64 cloud root file system and the stock generic kernel (the
release's own series, 6.8 for 24.04), the kernel uncompressed (`Image`) and its
modules indexed with `depmod` (they are not installed as packages, so nothing
else would). A `henge` user with no password signs in on the consoles and has
sudo; the network is DHCP on virtio-net; `--desktop` adds Xfce with LightDM
signing in by itself (apt runs inside the image, so that needs an arm64 host).

```sh
sudo tools/linux/make-ubuntu-disk.sh ~/Ubuntu --size 24G --desktop --pack
```

The folder holds `Image`, `disk.img.hvsparse` (the disk with its holes left out)
and `machine.json` with `"kind": "linux"` and the kernel's command line.

`.github/workflows/linux-machine.yml` does the same on a GitHub-hosted arm64
runner, where the engine runs natively: it builds `tools/hengelinux`, makes the
machine, boots it with `henge.autotest` on the command line (a unit in the image
then reports what came up on the serial console and powers off), and uploads the
folder as an artifact.

## In the app

Bring the folder in like an exported Mac: drop it on Henge in Finder, or pick it
in the Files app. A `"kind": "linux"` machine.json makes it a Linux machine: its
`Image` (and `initrd.img`, if there is one) instead of the boot ROM and
auxiliary storage, a 1280 x 800 screen, and `henge_vm_boot_linux` to start it.
Instant resume is off for it (the snapshot format holds the Mac's devices).

What runs on the phone, and how:

- **Machine:** `hv_vm_create` builds `linuxvirt` when `HvVmConfig.linux_kernel`
  is set; its devices are in the virtual Mac's board, so the input, disk,
  network and sound code is the Mac's. Guest RAM comes from
  `hv_vmapple_alloc_ram`, outside the app's footprint where iOS allows it.
- **Display:** the simple-framebuffer, handed to `BootDisplay.swift` through
  `hv_vm_boot_display` (32-bit BGRA, as the Mac's boot framebuffer). The guest
  draws with the CPU: no GPU acceleration yet.
- **Input:** the Mac's xHCI controller with its USB keyboard and Magic Trackpad 2
  (Linux: `xhci_pci`, `usbhid`, `hid-magicmouse`; libinput gives two-finger
  scrolling and gestures), and virtio-input for the relative mouse.
- **Network and sound:** virtio-net on the user-mode network, virtio-snd.

## Next

- Try it on a phone.
- A virtio-gpu device: with virglrenderer on ANGLE/Metal, OpenGL in the guest
  would run on the phone's GPU, as LinuxRunner does it.
- Saving a Linux machine (instant resume).
