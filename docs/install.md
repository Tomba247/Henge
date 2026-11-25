# Install

**You need** an iPhone or iPad with an A14 or M1 chip or newer and iOS or iPadOS 17 or later, a way to sideload, and JIT.
These builds are tried on an iPhone 18 Pro Max with iOS 27.0.1.

1. Download **Henge.ipa** from Downloads (**Henge.tipa** for TrollStore).
2. Install it with SideStore, AltStore, Feather, FlareStore or another sideloader. To get updates in one of them, add the
   source `https://raw.githubusercontent.com/Tomba247/Henge/source/source.json`.
   Installing over an earlier build of this fork keeps your machines, programs and settings. Do not delete the old app
   first: that deletes them.
3. If your certificate has it, sign with the **increased memory limit** entitlement.
4. Open Henge, go to Settings, and **set up JIT** (StikDebug, TrollStore, or the helper built into Henge on iOS 26 or
   later). Henge's first screen says **JIT on** when it works. This is needed each time Henge is opened.
5. Choose what to run:
   - **macOS**: the Install tab. Keep about 43 GB free, Henge open and the device charging.
   - **Linux**: see "Running a Linux machine" below.
   - **Claude Code and the toolchain**: turn on Settings › **Mac apps without macOS**, then follow the steps in "A coding
     terminal" below.

## Recommended settings

In Files › On My iPhone › Henge, make a text file called **henge-env.txt** (or use Settings › Engine options), one line
each:

```
HV_HASH_BITS=23
HV_FIXED_ADDR=1
HV_TR_SPIN=200
HV_SUPERBLOCKS=1
```

These are what this fork's owner uses on an iPhone 18 Pro Max. They are read when a machine starts.

| Line | What it does |
|---|---|
| `HV_HASH_BITS=23` | A bigger table for finding translated code, 48 MB more memory. On a Mac, making room in a full code cache took about half the time |
| `HV_FIXED_ADDR=1` | Programs load at their own addresses, so translated code is used again. It gives up macOS's address randomization, a security protection, inside that virtual Mac |
| `HV_TR_SPIN=200` | A CPU waits briefly in place for the translator before it sleeps |
| `HV_SUPERBLOCKS=1` | Longer translated blocks. No measured gain in our test of opening apps; leave it out if a machine misbehaves |

Already on without a file: `HV_HZ=60`, `HV_FLUSH=2`, `HV_SYNC_WAIT=1`, `HV_WFI_SPIN=0`. Leave `HV_EL0_WINDOW` off (5 to
10 times slower on an iPhone when tried). For Linux, `HV_LINUX_VULKAN=1` adds Vulkan and `HV_LINUX_NO_3D=1` turns 3D off.
The coding terminal needs none of these.
