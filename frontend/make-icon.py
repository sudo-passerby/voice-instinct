"""Write the tray icons (64x64) with the standard library only: Nova's star and two shells, in one
light colour for a dark bar. icon.png connected, icon-muted.png slashed, icon-offline.png hollow and dashed."""
import math
import struct
import zlib

S, SS = 64, 4  # size, supersampling per axis
STAR, GREY = (219, 229, 255), (150, 156, 172)
DOT, RINGS, STROKE = 6.2, (14.5, 23.5), 4.2  # the mark fills three quarters of the square, like the glyphs beside it


def ink(x, y, kind):
    """True where the mark is, at one sample point."""
    dx, dy = x - S / 2, y - S / 2
    r, a = math.hypot(dx, dy), math.atan2(dy, dx)
    if kind == "muted":
        across, along = abs(dx - dy) / math.sqrt(2), abs(dx + dy) / math.sqrt(2)  # top-left to bottom-right
        if along < 25 and across < 2.3:
            return True
        if along < 29 and across < 6.0:
            return False  # a gap, so the slash reads over the shells
    if kind == "offline":
        if abs(r - DOT + 1.5) < 1.6:
            return True  # hollow star
        return any(abs(r - R) < STROKE / 2 and (a / math.tau * 5 + i * 0.37) % 1 < 0.6 for i, R in enumerate(RINGS))
    return r < DOT or any(abs(r - R) < STROKE / 2 for R in RINGS)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


for name, kind, color in (("icon.png", "on", STAR), ("icon-muted.png", "muted", GREY), ("icon-offline.png", "offline", GREY)):
    rows = []
    for y in range(S):
        row = bytearray([0])
        for x in range(S):
            hits = sum(ink(x + (i + 0.5) / SS, y + (j + 0.5) / SS, kind) for i in range(SS) for j in range(SS))
            row += bytes([*color, round(255 * hits / SS**2)])
        rows.append(bytes(row))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    open(name, "wb").write(png)
