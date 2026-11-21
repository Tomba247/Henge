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
| virtio-gpu (the display; 3D with virgl) | PCIe | `virtio_gpu` (DRM/KMS; Mesa's virgl driver in the guest) |
| simple-framebuffer (instead, with `HV_LINUX_FB`) | top of RAM | `simpledrm` / `simplefb` |
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

### On a Mac host, and under a sanitizer

`make mac-linux-test` builds the same tool for an Apple-silicon Mac
(`build/hengelinux`; no 3D there yet: the GPU libraries are built for iOS
only). GitHub's Apple-silicon runner (`xcode-27`, the image the app is built
on: an M1 virtual Mac with 3 CPUs, 7 GB, 16 KB pages, macOS 27) boots a
released Ubuntu GNOME machine with it to the desktop and through the display
part of the self-test in about a minute. `.github/workflows/mac-probe.yml`
does that and says what else that runner is (2026-11-21):

- The engine's nine tests pass there natively: `MAP_JIT` memory works for an
  ad-hoc signed program. Its write/execute switch is not enforced in the
  virtual Mac (a write while the region is executable goes through), so a
  mistake with it shows on real hardware only.
- Metal is an "Apple Paravirtual device" (not Metal 3, no Apple GPU family).
  MoltenVK 1.4.2 answers on it with the phone's gaps: no `nullDescriptor`,
  `logicOp` or `geometryShader`, no `VK_KHR_external_semaphore_fd`;
  `VK_EXT_external_memory_host` is there.
- No Hypervisor.framework: `hengevm-ref` builds and cannot run.
- `free()` clears a freed block, as on a phone: of blocks of 32 to 4096 bytes
  every byte past the first 16 reads 0 afterwards. A read after `free()`
  finds zeros there; glibc leaves what was written.

`LX_SAN` (Linux host) and `MAC_SAN` (Mac host) build the device layer and
the tool with a sanitizer and leave `core/` as it is, since translated code
enters it with the guest's registers live:

```sh
make linux-test LXCC=gcc LX_VIRGL=1 LX_SAN=-fsanitize=address LXB=build/linux-virgl-asan
make mac-linux-test B=build/san MAC_SAN=-fsanitize=address
ASAN_OPTIONS=detect_leaks=0:use_sigaltstack=0 build/san/hengelinux ...
```

`use_sigaltstack=0`: the engine's CPU threads install their own alternate
signal stack (64 KB from `malloc`), and the sanitizer, which unmaps what it
takes for its own when a thread ends, otherwise stops a clean run at its end
with "failed to deallocate 0x10000 bytes". On a Linux host the engine's
signals stay its own with `handle_segv=0:handle_sigbus=0:handle_sigill=0:handle_sigfpe=0:allow_user_segv_handler=1`.

`linux-machine.yml` boots every machine once this way (3D, the iPhone's way
of drawing) and fails on a report. What it is for: builds 85 to 97 of the app
read a posted job after freeing it (`devices/vgpu_virgl.c`); iOS's `free()`
had cleared it, the code then wrote into the freed block, and the app died
seconds into a desktop somewhere in the system's own code. No boot on a
runner showed anything, and an earlier sanitizer run had only virglrenderer
instrumented, not the engine. With that file as it was, the device layer
under AddressSanitizer reports it at the first drawing command
(`mac-probe.yml`'s proof step): heap-use-after-free, a read of size 4 in
`render_thread`, 16 bytes inside a 32-byte block freed two lines earlier.

With all of the engine built with AddressSanitizer and UBSan the nine tests
run too, and what they report is not memory errors: left shifts of negative
branch offsets (`core/translate.c`, `core/memop.c`) and `HV_EC_BRK << 26` in
an `int` (`core/exception.c`), which the compilers in use shift as meant;
and `test_mem`'s checks that the fast path was taken and one of
`test_devices`' that an operation took under 50 ms, which an instrumented
build does not meet.

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

## The kernel: 16 KB pages

HengeVM's software TLB keeps 16 KB pages, macOS's and the iPhone's size; a guest
mapping smaller pages takes the slow path, a table walk, on every access
(`core/mmu.c`, `tlb_fill`). Ubuntu's stock arm64 kernel uses 4 KB pages, so its
user space crawled: on the arm64 CI runner one boot service (AppArmor compiling
snap profiles) took 18 minutes. `tools/linux/build-kernel.sh` builds Linux (6.12
long-term) from arm64 defconfig plus `tools/linux/henge-kernel.config`: 16 KB
pages, a 47-bit VA (three table levels, as macOS), the machine's devices built in,
and what Ubuntu needs. The same BusyBox work on the engine (qemu-aarch64 host,
guest seconds):

| | Ubuntu 6.8, 4 KB pages | Henge 6.12, 16 KB pages |
| --- | --- | --- |
| shell loop (3000 iterations) | 1.97 s | 0.02 s |
| gzip of 16 MB | 25.6 s | 0.14 s |
| md5 + sha256 of 4 MB | 9.3 s | 0.05 s |

With it, Ubuntu 24.04 boots to its signed-in console with systemd and snapd in
under a minute on the arm64 runner (4 CPUs, 4 GB).

User space is Ubuntu's own. Programs that assume 4 KB pages (a few allocators and
JITs built that way) may not run on a 16 KB kernel, as on Asahi Linux.

## An Ubuntu machine

`tools/linux/make-ubuntu-disk.sh` makes a folder the app brings in, from Ubuntu
as it is: the arm64 cloud root file system and the stock generic kernel (the
release's own series, 6.8 for 24.04), the kernel uncompressed (`Image`) and its
modules indexed with `depmod` (they are not installed as packages, so nothing
else would). A `henge` user with no password signs in on the consoles and has
sudo; the network is DHCP on virtio-net. `--desktop` adds Xfce with LightDM
signing in by itself, restyled after Ubuntu (Yaru, the Ubuntu font, a dock);
`--gnome` adds Ubuntu's own desktop, GNOME Shell with Ubuntu's session, dock and
theme, GDM signing in by itself, without the first-run setup, the welcome tour,
the screen lock or animations (Settings > Accessibility turns those back on).
Both get Firefox as Mozilla's deb (Ubuntu's firefox package is the snap). apt
runs inside the image, so these need an arm64 host.

```sh
sudo tools/linux/make-ubuntu-disk.sh ~/Ubuntu --size 24G --desktop --pack
```

The folder holds `Image`, `disk.img.hvsparse` (the disk with its holes left out)
and `machine.json` with `"kind": "linux"` and the kernel's command line.

`.github/workflows/linux-machine.yml` does the same on a GitHub-hosted arm64
runner, where the engine runs natively: it builds `tools/hengelinux`, makes the
Xfce and the GNOME machine (a job each), boots each with `henge.autotest` on the
command line (`/usr/local/bin/henge-autotest` in the image then reports what came
up on the serial console, asks for a screenshot and powers off), again with virgl
3D both ways, and uploads each folder as an artifact if its desktop came up. The
app's build attaches them to its release as `Henge-Ubuntu-Xfce.zip` and
`Henge-Ubuntu-GNOME.zip`.

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
- **Display:** virtio-gpu (see "The GPU" below), its screen handed to
  `BootDisplay.swift` through `hv_vm_boot_display` (32-bit BGRA, a copy with
  the pointer drawn in). `HV_LINUX_FB` in the environment brings back the
  simple-framebuffer instead.
- **Input:** for now the virtio keyboard and mouse (`host/vm.c`, "Input for the
  Linux machine"): keys from the app's on-screen and hardware keyboards go to the
  virtio keyboard; on the iPhone trackpad one finger moves the pointer, two
  scroll, a tap clicks and a two-finger tap right-clicks.
- **USB (off):** the Mac's xHCI controller with its USB keyboard and Magic
  Trackpad 2 would give Linux the trackpad itself (`hid-magicmouse`, libinput's
  gestures). Linux finds its ports once the Supported Protocol capability gives a
  port count rather than a mask (`hv_xhci_spec_ports`), but then a transfer event
  pointing at an earlier TD than the one its driver waits for leads it to a
  stale command and a panic (`hengelinux --usb` shows it). macOS's driver
  tolerates the same events.
- **Network and sound:** virtio-net on the user-mode network, virtio-snd.

## The GPU

`devices/virtio_gpu.c` is a virtio-gpu device, which Linux's `virtio_gpu` DRM
driver drives: the guest has a real KMS display (modes up to 4K, page flips, a
hardware pointer) and Xorg uses its `modesetting` driver.

- **2D:** the guest's resources are kept in host memory, filled from its pages
  by TRANSFER_TO_HOST_2D; RESOURCE_FLUSH on the scanned-out one copies the
  damaged rectangle to the screen.
- **3D (virgl):** with a renderer (`HvVgpu3d`, filled by `devices/vgpu_virgl.c`)
  the device offers `VIRTIO_GPU_F_VIRGL` and the two virgl capsets, and passes
  contexts, 3D resources, transfers, command buffers and fences to
  virglrenderer, which runs the guest's Gallium commands on the host's OpenGL
  or OpenGL ES. virglrenderer and its GL contexts live on a thread of their own;
  scanned-out 3D resources are read back for the screen.
  - On a Linux host (`make linux-test LX_VIRGL=1`, `hengelinux --virgl`) it uses
    the host's Mesa through surfaceless EGL. On GitHub's arm64 runner Xorg in the
    guest reports *glamor X acceleration enabled on virgl*.
  - On iOS there is no Mesa: the renderer is virglrenderer on ANGLE, which draws
    with Metal (as UTM does). `ios/scripts/build-gpu-deps.sh` builds ANGLE,
    libepoxy and virglrenderer from UTM's forks as frameworks (the *iOS GPU
    libraries* workflow, cached for the app's build); the app embeds them and
    the engine loads them when a Linux machine starts (`HV_VIRGL_DLOPEN`),
    making the EGL display on ANGLE's Metal backend and handing virglrenderer
    its contexts (`HV_VIRGL_ANGLE`). Without them the GPU is 2D.
    `HV_VIRGL_OWN_EGL=1 hengelinux --virgl` takes that path on Linux, with
    Mesa's EGL, and the CI boots it too.

Next for the GPU: the screen as a Metal texture (UTM's virglrenderer can hand
out a resource's `MTLTexture`) rather than read back, a CAMetalLayer for it, and
the display following the app's window size.

## Vulkan (off unless asked for: `HV_LINUX_VULKAN=1`)

OpenGL (virgl) is what the desktop and its browsers draw with. Vulkan is being
added for programs that ask for it, and to try the desktop's OpenGL on top of
it (Zink) and measure on a phone whether that is faster. Until it is, it stays
behind switches: `hengelinux --venus`, `make-ubuntu-disk.sh --vulkan`, and the
workflow's `vulkan` input (its machine is named `henge-vulkan-*`, which the
app's build does not pick up).

The way to Vulkan on the host's GPU is Venus: Mesa's Venus driver in the guest
sends Vulkan calls through virtio-gpu to virglrenderer, which runs them on the
host's Vulkan (on a phone that would be MoltenVK, on Metal).

- **Tried:** the host's half without a machine, on the arm64 runner and on
  x86: Mesa's Venus driver (`mesa-vulkan-drivers`, which also has lavapipe)
  against virglrenderer's test server (`virgl_test_server --venus`, package
  `virgl-server`) on lavapipe gives "Virtio-GPU Venus (llvmpipe)". Ubuntu
  24.04's virglrenderer 1.0.0 serves Mesa 25.2's Venus (Vulkan 1.2).
- **Tried:** with `mesa-vulkan-drivers` and `vulkan-tools` in an image, the
  guest's own lavapipe draws `vkcube` on GNOME (the workflow's run 37).
- **Written (`hengelinux --venus`, Linux hosts):** what Venus asks of
  virtio-gpu. With the engine itself under qemu-aarch64 on x86, a guest's Mesa
  connects, maps its ring from the window, and the render server runs its
  commands as far as a Vulkan device; it then stalls on the ring's idle
  handshake, which relies on a store being seen by a following load in another
  process, an order qemu does not keep. On the arm64 runner it does not stall
  (run 44677024084): Ubuntu GNOME comes up, OpenGL stays virgl, and the guest's
  `vulkaninfo` lists "Virtio-GPU Venus (llvmpipe)" beside its own lavapipe.
  - Context types (`VIRTIO_GPU_F_CONTEXT_INIT`) and the Venus capset.
  - Blob resources (`VIRTIO_GPU_F_RESOURCE_BLOB`): created, mapped, unmapped.
  - The host-visible window: one block of reserved host memory that is guest
    RAM at `0x1000000000`, shown to Linux as a 64-bit BAR with a virtio shared
    memory region over it. The PCI bridge's 64-bit range is exactly the BAR's
    size, so Linux can only put it there. A blob is mapped over part of the
    block (`virgl_renderer_resource_map_fixed`, or its file with `mmap`), so
    the engine's memory code (`core/`) is unchanged.
  - Fences on a context's own timelines (`VIRTIO_GPU_FLAG_INFO_RING_IDX`).
  - virglrenderer runs Vulkan in its render server; OpenGL stays in the engine.
- **Open: a window.** `vkcube` on the Venus device finds no `VK_KHR_swapchain`.
  Mesa 25.2's Venus driver (Ubuntu 24.04) offers it only when the host's Vulkan
  can import a semaphore from a sync file (`VK_KHR_external_semaphore_fd`),
  which lavapipe has only with `/dev/udmabuf` and MoltenVK never has. Mesa
  26.2's offers it always (Kali has 26.1). So a phone needs a guest with the
  newer Mesa, or virglrenderer answering for the host. After the swapchain, the
  frame has to reach the desktop's OpenGL: a blob of the Venus context taken
  into the virgl context (a dma-buf on Linux hosts, shared memory on Apple's).
- **Works on the runner (2026-11-20, run 44758218302, Ubuntu GNOME):** on the
  app's virglrenderer (UTM's fork with `ios/patches`, built for Linux with its
  render server as threads) and lavapipe, `henge-vkmap` passes (mapped memory,
  a submit, a fence) and `vkcube` on the Venus device draws its 120 frames in
  a window on GNOME (89 fps; the screenshot has the cube), the frame reaching
  the desktop's OpenGL as a dma-buf. What it took on the runner:
  - `/dev/udmabuf` opened to the runner's user: lavapipe then has the
    sync-file and dma-buf extensions, and Ubuntu's Venus offers
    `VK_KHR_swapchain`.
  - `VKR_GUEST_PAGE_SIZE=16384` (`virglrenderer-guest-page-size.patch`): the
    guest's kernel rounds a blob up to its 16 KB page, the runner's pages are
    4 KB, and virglrenderer refuses a blob larger than the memory behind it
    (`vkr: mem dma_buf_size 4096 < blob_size 16384`). A phone's pages are
    16 KB. Ubuntu's own virglrenderer 1.0.0 cannot be helped that way and is
    not booted with `--venus`.
  - `virglrenderer-thread-server-fence-table.patch`: with the render server as
    threads, UTM's code freed the renderer's fence table when the last Venus
    context went, and the next OpenGL fence crashed the engine.
- **The app:** the GPU libraries build for iOS with Venus and MoltenVK and the
  patches above; `HV_LINUX_VULKAN=1` in henge-env.txt turns Vulkan on, and
  images made with `--vulkan` have a "Vulkan test" launcher whose result goes
  to the app's log. Untried on a phone. What differs there from the runner:
  MoltenVK instead of lavapipe, memory as shared memory imported with
  `VK_EXT_external_memory_host` (or files, `virglrenderer-shm-fallback.patch`)
  instead of dma-bufs, no sync files, and a frame handed to ANGLE as a Metal
  texture.
  - No sync files: Mesa's Venus before 26.2 (Ubuntu 24.04 has 25.2, Kali
    26.1) offers a swapchain only when the host imports semaphores from sync
    files, which MoltenVK cannot. All the guest imports is an already signaled
    payload, so `virglrenderer-semaphore-import-emulation.patch` answers for
    such a host: it offers the extension as importable only and signals the
    semaphore from an empty submission. `VKR_NO_SEMAPHORE_FD=1` makes any host
    one without it; the workflow's Vulkan boot runs that way.
- **On a phone (iPhone19,7, iOS 27.0.1, build 101.94, 2026-11-20):** Venus
  starts on MoltenVK 1.4.2; the guest's `vulkaninfo` lists "Virtio-GPU Venus
  (Apple A20 Pro GPU)", Vulkan 1.3; `henge-vkmap` passes (mapped memory, a
  submit, a fence). `shm_open` is refused ("Operation not permitted"), so the
  memory is unlinked files in the temporary directory
  (`virglrenderer-shm-fallback.patch`). `vkcube` gets a swapchain (the
  semaphore emulation) and then fails on its first image: the guest's window
  system allocates it from the first device-local memory type, MoltenVK's
  private storage, into which host memory cannot be imported ("host memory
  import is unsupported for memory type 0"). `virglrenderer-host-import-memory-type.patch`
  gives such memory, which the guest never maps, a host-visible type that can
  be imported. Unexplained: the app died without a word when Chrome was
  started with Vulkan on (a bus error or trap, which the app did not log; it
  does from the next build).
- **It works on a phone (build 101.98, the same iPhone, 2026-11-20):** in a
  Vulkan test image, one program at a time: `vulkaninfo` (Venus on "Apple A20
  Pro GPU"), `henge-vkmap`, and `vkcube --c 300` twice, each exit 0, nothing
  refused or failed in the app's log. While a cube ran the desktop drew about
  835 frames in 10 s against about 240 without it. Chrome started with Vulkan
  on and the app stayed up. Not tried there yet: two programs at once, Chrome
  drawing with Vulkan, a desktop on Zink.
- **What had been ending the app (builds 101.85 to 101.97) was not Vulkan.**
  The render thread of the OpenGL side (devices/vgpu_virgl.c) freed a posted
  job and then read its `async` field; iOS's free() clears memory, so the read
  gave 0 and `done = 1` was written into freed memory, by then another
  thread's small object. The app died seconds into a desktop in the system's
  own code (CoreFoundation, libxpc, UIKit), with Vulkan on or off, with either
  image; an iOS crash report showed a dictionary key of 0x0000000100000000,
  the pair `async = 0, done = 1`. glibc's free() leaves the field, so a runner
  never wrote. Settings > Linux > "Don't wait for the GPU", off, avoided it.
  The crash in MoltenVK's `MVKQueue` destructor with two programs at once
  (101.97) and the one in libxpc blamed on 101.95's trap-guard report were
  this too, as far as can be told; `virglrenderer-one-device-at-a-time.patch`
  (instances and devices made and destroyed one at a time across contexts,
  which share the process) stays as a precaution.
- **A device without `VK_KHR_external_semaphore_fd` has no `GetSemaphoreFdKHR`:**
  the runner's boot as such a host jumped to address 0 at a program's first
  presented frame. `virglrenderer-semaphore-wait-emulation.patch`: a
  submission that waits for the semaphore, itself waited for. The runner's
  self-test then passes with the emulation forced, with two cubes at once and
  six `henge-vkmap` in a row, also with the library built with
  AddressSanitizer (the workflow's `asan` switch; its runtime aborts at the
  end of the run on a deallocation of its own, after the tests).
- **Hands off:** `HV_LINUX_APPEND=henge.autotest=gpu` in henge-env.txt adds to
  the kernel's command line, so an image with the self-test runs its display
  and Vulkan part by itself at start and powers off; the result is on the
  console, in the app's log.
- **Trying it on a phone:** a release made by `build-henge.yml` with
  `vulkan_run` set carries a Vulkan test machine. In Files › On My iPhone ›
  Henge › henge-env.txt, the line `HV_LINUX_VULKAN=1`; start the machine; tap
  **Vulkan test** in the dock. Its result (lines starting `vktest:`) and the
  engine's own (`virgl:`, `anon_file:`) are in the app's log.
- **OpenGL on Vulkan (Zink) does not start on a phone** (build 101.98, Mesa
  25.2.8 in the guest): "Zink requires the nullDescriptor feature of KHR/EXT
  robustness2". MoltenVK 1.4.2 does not have it (its issue 2650, open), nor
  `logicOp` and `geometryShader`, which Zink's OpenGL 2.1 also lists. So a
  desktop drawn with Vulkan is not to be had from Mesa as packaged; UTM's
  release notes (5.0.7) say the same of MoltenVK for other reasons. The
  desktop stays on virgl (glxgears there: about 3000 frames a second);
  Vulkan is for programs that use it themselves.
- **The phone's half, from reading UTM's virglrenderer fork (1.3.0, the one the
  app already builds):** nothing here is tried yet.
  - It has Venus for Apple devices: `-Dvenus=true -Drender-server-mode=thread
    -Drender-server-worker=thread` runs the render server as threads inside the
    app (no helper process, which iOS would not allow), on MoltenVK.
  - Memory the guest maps is POSIX shared memory imported into Vulkan
    (`VK_EXT_external_memory_host`), so every blob is a file that
    `virgl_renderer_resource_map_fixed` can map into the window: the same
    design as above.
  - A Vulkan frame reaches the OpenGL compositor as a linear image in shared
    memory, wrapped as a Metal texture (no dma-buf, no IOSurface).
  - Risks: `shm_open` inside the iOS sandbox, MoltenVK's limits, the app's
    memory limit (each mapped allocation is shared memory), and a Vulkan fault
    taking the whole app down since nothing is in a separate process.

## What the desktop images hold

- **Network:** on Ubuntu GNOME NetworkManager runs the card (netplan's renderer
  is NetworkManager), so GNOME has its network icon and Settings panel; Kali
  uses NetworkManager too. Ubuntu Xfce and the server use systemd-networkd.
- **Browsers:** Firefox (Mozilla's deb on Ubuntu, Firefox ESR on Kali) and a
  second one, Google Chrome on Ubuntu (Google's arm64 package) or Chromium on
  Kali, with a policy file that skips its first-run questions and keeps
  hardware acceleration on. The self-test opens it and prints how its GPU
  process was started.
- **YouTube app (`yt`):** yt-dlp and mpv. YouTube needs yt-dlp to run
  JavaScript, which it does with Deno (`/usr/local/bin/deno`).

## Next

- Try it on a phone.
- Saving a Linux machine (instant resume).
