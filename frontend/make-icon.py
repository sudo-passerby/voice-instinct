"""Write the tray icons (64x64) with the standard library only: Nova's star, with its four spikes, inside one
shell, in one light colour for a dark bar. icon.png connected; icon-busy.png an errand is running or a question
waits (a gold body on the shell, as on the orb); icon-muted.png slashed; icon-offline.png hollow and dashed."""
import math
import struct
import zlib

S, SS = 64, 4  # size, supersampling per axis
STAR, GREY, GOLD = (219, 229, 255), (150, 156, 172), (255, 184, 56)
# The mark fills three quarters of the square, like the glyphs beside it. The spikes taper from a wide base to a
# point, so at 22 px it is a star in a ring and not a plus in a circle.
DOT, SPIKE, SPIKE_W, RING, STROKE = 7.0, 18.0, (4.8, 0.3), 25.0, 4.2
BODY, BODY_AT, BODY_GAP = 7.0, math.radians(45), 11.0


def ink(x, y, kind):
    """What is drawn at one sample point: None, "mark" or "body"."""
    dx, dy = x - S / 2, S / 2 - y  # y up
    r, a = math.hypot(dx, dy), math.atan2(dy, dx)
    ring = abs(r - RING) < STROKE / 2
    if kind == "muted":
        across, along = abs(dx + dy) / math.sqrt(2), abs(dx - dy) / math.sqrt(2)  # top-left to bottom-right
        if along < 27 and across < 2.3:
            return "mark"
        if along < 31 and across < 6.0:
            return None  # a gap, so the slash reads over the star
    if kind == "offline":  # hollow star, dashed shell
        return "mark" if abs(r - DOT + 1.0) < 1.7 or (ring and (a / math.tau * 6 + 0.2) % 1 < 0.6) else None
    if kind == "busy":
        b = math.hypot(dx - RING * math.cos(BODY_AT), dy - RING * math.sin(BODY_AT))
        if b < BODY:
            return "body"
        if b < BODY_GAP:
            return None
    along, across = max(abs(dx), abs(dy)), min(abs(dx), abs(dy))
    spike = along < SPIKE and across < SPIKE_W[0] + (SPIKE_W[1] - SPIKE_W[0]) * along / SPIKE
    return "mark" if r < DOT or spike or ring else None


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


for name, kind, color in (("icon.png", "on", STAR), ("icon-busy.png", "busy", STAR), ("icon-muted.png", "muted", GREY),
                          ("icon-offline.png", "offline", GREY)):
    rows = []
    for y in range(S):
        row = bytearray([0])
        for x in range(S):
            hits = [ink(x + (i + 0.5) / SS, y + (j + 0.5) / SS, kind) for i in range(SS) for j in range(SS)]
            body = hits.count("body")
            row += bytes([*(GOLD if body > hits.count("mark") else color), round(255 * (len(hits) - hits.count(None)) / SS**2)])
        rows.append(bytes(row))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    open(name, "wb").write(png)
