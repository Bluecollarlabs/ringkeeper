#!/usr/bin/env python3
"""Regenerate the dashboard PWA icons (stdlib only): python3 -I tools/pwa_icons.py ui-mockup/icons"""
import math, struct, sys, zlib, os
out = sys.argv[1]
BG = (11, 15, 23); A = (139, 124, 255); B = (61, 214, 181)
def png(path, size, ring_r, ring_w, rounded):
    ss = 3; rows = []
    c = size / 2
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    px = x + (sx + .5) / ss; py = y + (sy + .5) / ss
                    dx, dy = px - c, py - c
                    # background: full square (maskable) or rounded square
                    inside = True
                    if rounded:
                        r = size * .22; qx = max(abs(dx) - (c - r), 0); qy = max(abs(dy) - (c - r), 0)
                        inside = qx * qx + qy * qy <= r * r
                    if not inside:
                        continue
                    d = math.hypot(dx, dy)
                    col = BG
                    if abs(d - ring_r) <= ring_w / 2:
                        t = (math.atan2(dy, dx) + math.pi) / (2 * math.pi)
                        t = .5 - .5 * math.cos(2 * math.pi * t)
                        col = tuple(int(A[i] + (B[i] - A[i]) * t) for i in range(3))
                    elif math.hypot(dx - ring_r * .0, dy + ring_r) <= ring_w * .42:
                        col = (255, 255, 255)
                    for i in range(3): acc[i] += col[i]
                    acc[3] += 255
            n = ss * ss
            a = acc[3] // n
            row += bytes([acc[0] // max(1, acc[3] // 255), acc[1] // max(1, acc[3] // 255), acc[2] // max(1, acc[3] // 255), a]) if acc[3] else bytes(4)
        rows.append(bytes(row))
    raw = zlib.compress(b''.join(rows), 9)
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', raw) + chunk(b'IEND', b'')
    open(path, 'wb').write(data)
os.makedirs(out, exist_ok=True)
for s in (192, 512):
    png(f'{out}/icon-{s}.png', s, s * .30, s * .11, True)
png(f'{out}/icon-maskable-512.png', 512, 512 * .24, 512 * .09, False)
png(f'{out}/apple-touch-icon.png', 180, 180 * .30, 180 * .11, False)
