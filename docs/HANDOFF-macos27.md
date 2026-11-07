# Handoff: Henge 0.1 running macOS 27.2, and where to take it next

Paste this whole file into Claude (Claude Code works best, opened in a clone of this repo).
It was written by another Claude session that built and tested this branch with Tomba247.

---

## Who you're helping and what they want

You're helping a developer with an **M4 MacBook Pro** and an **M4 iPad**. He wants to:

1. Get Henge running macOS on his **M4 iPad**.
2. Make it **faster**, especially opening apps and browsing.

Tomba247 already has macOS 27.2 running well on an iPhone 18 Pro Max using the branches in this
fork. This document explains what was done, what was measured, how to build, and where the
speed is likely going.

## What Henge is (short)

Henge (upstream: `Leviidev/Henge`, GPL-2.0-or-later) runs real macOS on iPhone/iPad without
a jailbreak. iOS gives apps no hypervisor, so Henge is a **virtual machine built on an
arm64→arm64 binary translator**:

- **CPU:** guest arm64 code is translated block by block into a JIT region (`core/translate.c`,
  `core/dispatch.c`). Most instructions are copied unchanged. Rewritten: loads/stores (through a
  software MMU with an inline TLB, `core/mmu.c`, `core/memop.c`), PC-relative instructions and
  branches, system registers and exceptions (`core/sysreg.c`, `core/exception.c`), and pointer
  authentication (stripped).
- **Devices:** emulated, following QEMU's `vmapple` machine (`devices/`): GICv3, timers, AES,
  virtio block/net/input/sound/USB, and so on.
- **Graphics:** macOS's paravirtual GPU command stream is replayed with Metal
  (`devices/pvg_lite.c`, `devices/pvg_metal.m`).
- **Install:** Apple's restore image is restored on-device through vendored idevicerestore/usbmuxd
  over a virtual USB port (`host/install.c`, `host/libusb_shim.c`). The boot ROM (AVPBooter)
  is downloaded from Apple at install time and must **never** be bundled.
- **JIT on iOS:** the built-in StikJIT helper (or StikDebug) with LocalDevVPN; a 1 GB execute
  region is claimed at launch.

Read `docs/architecture.md` first.

## Branches in this fork (Tomba247/Henge)

| Branch | What it is | Release |
|---|---|---|
| `macos27` | Upstream tag `0.1` + upstream commits `f0eec63` and `db1fac3` (PR #8 by shrimpapplepro: macOS 27.2 install + Metal replay fixes) + build number 2 + CI | `0.1-macos27-3`, works on iPhone |
| `macos27-perf` | `macos27` + upstream engine speedups `e43b98c` (software TLB layout), `6b32fa2` (macOS 27 kernel TLBI range flush), `bf5f2a1` (test knobs, prerequisite), `a1e4b82` (slow-path recheck dropped: 8% cold / 31% warm per upstream) + build number 3 | `0.1-macos27-perf-7`, works on iPhone |

All of these were cherry-picks that applied cleanly; no engine code was written by hand here.
Upstream `main` has moved well past 0.1. Between releases, upstream `main` deliberately does
not build (`ios/scripts/package_ipa.sh` checks for `ios/Henge/ReleaseGate.swift`, which only
ships with releases). That's why this fork builds from the `0.1` tag plus cherry-picks. Respect
that choice. Don't stub the file out to get around it.

## What happened, in order (from the iPhone logs)

Device: iPhone 18 Pro Max (`iPhone19,7`), iOS 27.0.1, 11.5 GB RAM, 6 cores, TXM device.

1. **Stock 0.1 + macOS Sequoia 15.6.1:** installs and runs fine.
2. **Stock 0.1 + macOS 27.2 (`26B123`):** the install fails right after the iBSS upload:
   `iBoot Panic: : 70b7ca7d373f8bb:144`, `Build: RELEASE:mBoot-20457.41.3`, then
   `Device did not disconnect. Possibly invalid iBSS`. Cause, per `f0eec63`: 27's iBSS **writes**
   aux-disk sector 32768 over bdif, and 0.1 only served reads. (Issue #26 shows the same panic on
   an M5 iPad after an **in-guest macOS update**: don't update macOS from inside the guest.)
3. **`macos27` build:** 27.2 installs. The first boot shows black for about 70+ s, then the
   desktop appears. Safari and YouTube work. Logged session: about 17 min,
   `853553 draws, 0 failed`, iOS memory headroom steady at 3.2–3.4 GB with the guest at
   **8 GB RAM, 4 vCPUs, 1 GB JIT region**.
4. **`macos27-perf` build:** equally stable (about 13 min, memory headroom 3.2–3.7 GB). The only
   "failed" draws (145) were `kIOGPUCommandBufferCallbackErrorBackgroundExecutionNotPermitted`
   while the app was in the background. That's iOS refusing GPU work from background apps, not
   a regression. Henge 0.1 keeps submitting GPU work when backgrounded (worth fixing: pause the
   presenter on background).
5. These log lines are harmless and also present on upstream `main`:
   `unknown hypervisor call 0x8300ff01 (x1 0) at el1` (3–4 times per boot), and
   `aes2: write +0 = N (not modelled)`.

**User's own read of performance:** "It works great for what it is. Apps open really slowly;
once loaded they're pretty good. Navigating, especially scrolling and browsing in Safari, is
poor." Henge 0.1's log only has graphics stats (`pvg-metal: N execs, N draws, ...`), so the
speedup builds could not be compared numerically from the logs.

## The M4 MacBook Pro changes everything: build options

### Option A: build and install locally (recommended)

No GitHub needed.

1. **Tools:** Xcode 27 (the README asks for 27; it also built cleanly with Xcode 26.6 / iOS SDK
   26.5 in CI), `brew install xcodegen meson ninja pkg-config`, rustup with
   `rustup target add aarch64-apple-ios`.
2. **Husk** (supplies StikJIT, on-device pairing, libslirp, glib), at the path the Makefile
   expects:
   ```sh
   mkdir -p ~/Documents/Coding && git clone https://github.com/Leviidev/Husk ~/Documents/Coding/Husk
   cd ~/Documents/Coding/Husk && git checkout afe83d4
   . scripts/sources.sh && mkdir -p third_party/sources third_party/build
   for u in "$FFI_SRC" "$GLIB_SRC" "$SLIRP_SRC"; do f=third_party/sources/$(basename "$u"); curl -fL -o "$f" "$u"; tar -xf "$f" -C third_party/build; done
   ./scripts/build_ios.sh libffi glib libslirp
   ./scripts/build_rppairing_ios.sh
   ```
3. **Henge:**
   ```sh
   git clone https://github.com/<his-fork>/Henge && cd Henge && git checkout macos27-perf
   ios/scripts/fetch_deps.sh
   mkdir -p ios/build            # package_ipa.sh writes its build log there
   ios/scripts/package_ipa.sh    # -> ~/Desktop/Henge.ipa (unsigned, ad-hoc signed inside)
   ```
4. **Install on the iPad:** either sideload `Henge.ipa` with SideStore/AltStore (as Tomba247
   did), or open `ios/Henge.xcodeproj` (made by `xcodegen generate` in `ios/`), set his own
   team, and run on the iPad from Xcode. A free Apple ID works but expires after 7 days. Keep
   the increased-memory-limit and extended-virtual-addressing entitlements (`package_ipa.sh`
   refuses to package without them).
   **Bump `CFBundleVersion`** in `ios/Henge/Info.plist` and `ios/HengeJITHelper/Info.plist`
   above 3 so it installs as an update over an existing Henge without deleting the user's Macs.

### Option B: GitHub Actions (when away from the Mac)

`.github/workflows/build-ipa.yml` does all of Option A on a GitHub `macos-26` runner (Xcode 26.6,
the newest there), then publishes `Henge.ipa` as a release. It's free on a public fork. Pushing
to `macos27` or `macos27-perf` triggers it; put `[skip ci]` in a commit message to skip a build.
Gotchas already solved: Husk must be at `~/Documents/Coding/Husk`, and `ios/build` must exist.

### Option C: run the engine on the Mac itself (fastest iteration)

The Mac build links Homebrew's copies of the restore libraries (see the comments in `Makefile`;
roughly `brew install libimobiledevice libtatsu libplist libzip`).
`make` builds `build/hengevm` (the same engine as the app, run as a Mac command-line tool) plus
`build/hengevm-ref` (the same machine with the CPU on Hypervisor.framework). `hengevm --install
<ipsw>` installs macOS on the Mac. This is by far the fastest edit-run-measure loop for engine
work. Compare against `hengevm-ref` to separate translation cost from device and graphics cost.
**`make test`:** on GitHub runners `test_jit` and `test_mem` segfault at every commit (the
working 0.1 build included), and `test_devices` wants a folder at
`/Volumes/macOS Storage/husk-work`. On a real M4 they should behave as upstream intends:
run them before and after every engine change.

## The M4 iPad

- Release 0.1 says iPad support "hasn't run on a real iPad yet". Upstream `main` has since
  merged a lot of iPad work that **is not in this fork**: colour matching for P3 panels
  (`7f3398c`), a performance display mode and smoother input (`eb3d8e2`), a smaller pointer
  (`3b8d28f`), pointer-lock hints (`e4df4eb`, `dcae4b3`), JIT before the first Mac on TXM
  devices (`524219d`, issues #22/#23), a compute-pipeline fallback that stopped Metal aborting on
  an M1 iPad (`ddab1f4`, issue #12), 120 Hz and Retina modes (`35d3f6c`, `7fb10f9`), and the
  memory chooser (`d5706ff`).
- Plan: build `macos27-perf` for the iPad as-is first. If iPad-specific problems appear,
  cherry-pick those commits one at a time (`git cherry-pick <sha>`; resolve conflicts, rebuild,
  test). `d5706ff` is known to apply cleanly on top of `macos27`.
- Issue #26 (M5 iPad Pro, Sequoia) reports: physical right-click doesn't open context menus
  (Control-click works), changing the guest resolution causes click offset, and Safari
  WebContent crashes. Expect the same on an M4 iPad.
- Start with **4–6 GB** guest RAM on an iPad with 8 GB; give it more if the iPad has 16 GB.

## Where the time likely goes, and ideas to try

Measure before changing anything. 0.1 already has a profiler: `HV_PROF_FN`, `HV_PROF_PCS`,
`HV_PROF_ALL` (`core/dispatch.c`, `host/main.c`). Upstream later added a sampling profiler
(`host/prof_ios.c`, `7fb10f9`) and stats in the log (`35d3f6c`). Set these on the Mac
(Option C), or on iOS through upstream's `Documents/henge-env.txt` (`35d3f6c`; not in 0.1).

**Repeatable benchmarks** (run inside the guest, same each time):
- Process start, heavy on TLB and page faults: `time (for i in {1..300}; do /usr/bin/true; done)`
- Browser: **Speedometer 3** and **JetStream** in Safari (browserbench.org)
- App launch: time from click to window for System Settings, Safari, TextEdit, cold and warm

**Symptom: apps open slowly.** First-run code means translating lots of new blocks; dyld,
page faults and TLB churn; disk reads through virtio.
- **fastmem:** upstream is measuring it (`9460845`, `c9ce6bc`, `3fcc6f2`: `HV_KVA_HIST`, largest
  reservable range). The idea: map guest virtual addresses directly onto host memory so most
  loads and stores skip the software TLB. That's probably the biggest single CPU win.
- **Code cache size and flush behaviour:** `2050ec5` (1 GB or 1.5 GB), and `HV_JIT_MB` to test a
  small cache. Check how often the JIT region fills and flushes during app launches.
- **Block chaining and superblocks:** `HV_SUPERBLOCKS` (in 0.1, off by default; it continues past
  conditional branches), `HV_NO_XCHAIN`, `HV_INLINE_XJUMP`. Measure whether superblocks help
  app launch and browsing.
- **Idea (unexplored): a persistent translation cache.** macOS's dyld shared cache is identical
  on every boot. Translating it ahead of time and loading the translations into the JIT region
  at boot (relocated, validated by page hash) could remove most first-launch translation cost.
  This is a large project, but the RW/RX dual mapping makes it possible on iOS.

**Symptom: scrolling and browsing are bad.** Several costs stack up:
1. **JavaScriptCore's own JIT inside the guest:** Safari constantly generates and patches
   machine code, which forces Henge to retranslate it and invalidate caches (see `HV_IC_FLUSH`,
   `HV_IC_IVAU` in `core/sysreg.c`). **Experiment:** turn on **Lockdown Mode** in the guest
   (it disables Safari's JavaScript JIT) and compare Speedometer and the feel of scrolling. If
   that's faster or smoother, retranslation churn from guest JITs is the bottleneck, and
   cheaper handling of self-modifying code (invalidating at page or sub-page granularity, a
   faster path for W^X toggles) is the place to work.
2. **Pixels:** in Tomba247's 27.2 log, WindowServer allocated 3840×2160 surfaces (HiDPI 2x
   backing for the 1920×1080 display). That's 4x the pixels to composite and replay. Try
   upstream's Performance screen mode, or offer a non-HiDPI mode, and compare.
3. **Metal replay cost:** `pvg-metal` stats lines report encoding time and GPU wait. In the
   iPhone logs, encoding was 0.1–0.8 s per stats window and GPU wait about 0.01 s, so the GPU
   itself isn't the limit. Look at the CPU side of replay and at frame pacing in
   `presentLoop` (`devices/pvg_metal.m`).
4. **Input-to-frame latency:** scrolling "feel" is often latency, not throughput. Measure the
   time from a touch event (virtio input) to the frame that shows it.
5. **vCPUs vs host cores:** the iPhone has 2 performance and 4 efficiency cores. The M4 iPad has
   more performance cores, so test 4 against 6 vCPUs there. Also try `HV_WFE_SPIN` (spin briefly
   on idle) for latency.

## Ground rules for this work

- Never bundle Apple's boot ROM, restore images or any Apple binaries. Henge fetches them from
  Apple at install time, on purpose.
- Keep everything GPL-2.0-or-later. Credit original authors: use `git cherry-pick` (it keeps
  authorship) rather than copying code.
- Don't push to `Leviidev/Henge`. Work in the fork; offer improvements upstream as pull requests
  only when the user asks.
- One change at a time, with a before/after measurement from the same benchmark and the same
  guest state. Keep a log of results in this file or a new one in `docs/`.
