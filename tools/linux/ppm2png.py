#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""ppm2png.py IN.ppm OUT.png: a binary PPM (P6, what hengelinux's HV_FB_DUMP writes) as a PNG, with the
standard library only. Also prints how much of the screen is not black, so a log shows whether anything drew."""
import struct, sys, zlib

def main(src, dst):
    data = open(src, "rb").read()
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace(): pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace(): end += 1
        fields.append(data[pos:end]); pos = end
    assert fields[0] == b"P6", "not a binary PPM"
    w, h = int(fields[1]), int(fields[2])
    pix = data[pos + 1:pos + 1 + w * h * 3]
    rows = b"".join(b"\x00" + pix[y * w * 3:(y + 1) * w * 3] for y in range(h))
    def chunk(kind, body): return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
    open(dst, "wb").write(png)
    lit = sum(1 for i in range(0, len(pix), 3 * 7) if pix[i] | pix[i + 1] | pix[i + 2])
    print(f"screen {w}x{h}: {100.0 * lit / max(1, len(pix) // (3 * 7)):.0f}% of it not black -> {dst}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
