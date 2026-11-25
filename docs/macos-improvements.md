# macOS improvements in this fork

On top of Henge's virtual Mac. Everything here is off or runs only when asked for, except the RAM and refresh time fixes.

- **Lighter macOS stops automatic downloads of macOS updates.** macOS downloads and prepares its next update by itself,
  using more than two CPUs' worth for minutes after startup. The Lighter macOS script now turns that off, asking for the
  Mac's password once; the undo script puts it back. Checks for updates and security files are not changed. On a Mac,
  from a saved machine opening 5 apps (2 rounds): 15.0 s before and **12.9 s** after the whole script, which also
  includes its animation and wallpaper changes. Not measured on an iPhone.
- **Back Up.** Export is now Back Up: the disk is packed with LZFSE, every piece carries a checksum, and the whole file
  is read back before it is kept. Only then is the backup before it removed. Bring in a Mac reads these and old exports.
- **Make Disk Bigger.** With the Mac shut down, its disk grows and Recovery moves to the new end. macOS then takes the
  room with `diskutil apfs resizeContainer disk0s2 0`. Tried from 64 to 128 GB on macOS 27.2 on a Mac, with a clean file
  system check afterwards; not tried on an iPhone. Back up first.
- **A Mac gets the RAM it asks for.** 60 GB of address space held for Mac apps left a 4 GB Mac with 2 GB. It is given
  back when a Mac starts.
- **The display's refresh time** is written where macOS's display driver reads it. Without it WindowServer logged
  "Timer delayed by" the whole uptime. Offered to Henge apart from this, as pull request 161.
- **`HV_HASH_BITS`** (off): a bigger table for finding translated code. On a Mac, making room in a full code cache took
  5.1 and 6.0 ms without it, 3.3 and 3.1 ms with 23. Whole runs were within the noise; not tried on an iPhone.
- **`tools/ios-kit`**: scripts that build iPhone apps inside a virtual Mac from an Xcode you downloaded, without opening
  Xcode.
