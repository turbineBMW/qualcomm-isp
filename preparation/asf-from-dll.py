#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Build the ASF post-LTM register program and the nine DMI tables from the DLL's own unpacked struct and
packed registers (slrun asf34 output). Packing per IPEASF341::RunCalculation (dll-dis/asf341-calc.dis):
 sel1: L1 actNorm | softTh<<8 | gainNeg<<16 | gainPos<<24 (measured with an impulse probe) ; sel2: L1 gainWeight ; sel3: L1 softThWeight ;
 sel4-6: layer 2 ; sel9: skin[17] = act(0x12f3) | gain(0x12e2)<<8 ; sel10/11: 256 x (a | b<<8) from 0xce2/0xde2 and 0xee2/0xfe2.
usage: asf-from-dll.py <unpacked.bin> <regs.bin> <post_out> <tables_out>"""
import struct, sys
u = open(sys.argv[1], 'rb').read(); r = struct.unpack('<68I', open(sys.argv[2], 'rb').read()[:0x110])
def regcont(off, regs): return struct.pack('<II', (3 << 24) | len(regs), off) + b''.join(struct.pack('<I', x & 0xffffffff) for x in regs)
post = (regcont(0x11c68, r[0:2]) + regcont(0x11c74, r[2:4]) + regcont(0x11c7c, r[4:14]) + regcont(0x11cb0, r[14:20]) + regcont(0x11cd4, r[20:26]) + regcont(0x11cec, r[26:44])
        + regcont(0x11d34, r[44:46]) + regcont(0x11d3c, r[46:49]) + regcont(0x11d48, r[49:61]) + regcont(0x11d80, r[61:62]) + regcont(0x11d84, r[62:65]) + regcont(0x11d90, r[65:68]))
def t4(a, b, c, d): return b''.join(struct.pack('<I', u[d+i] | (u[c+i] << 8) | (u[b+i] << 16) | (u[a+i] << 24)) for i in range(256))   # bfi dst keeps the low byte: act | soft<<8 | gneg<<16 | gpos<<24
def t1(a): return b''.join(struct.pack('<I', u[a+i]) for i in range(256))
def t2(a, b, n=256): return b''.join(struct.pack('<I', u[a+i] | (u[b+i] << 8)) for i in range(n))   # (verified lane order: skin act|gain<<8, contrastNeg|chromaPos<<8, contrastPos|chromaNeg<<8)
tables = t4(0x7f, 0x27f, 0x67f, 0xa7f) + t1(0x47f) + t1(0x77f) + t4(0x17f, 0x37f, 0x87f, 0xb7f) + t1(0x57f) + t1(0x97f) + t2(0x12f3, 0x12e2, 17) + t2(0xce2, 0xee2) + t2(0xde2, 0xfe2)   # sel10 = contrastNeg | chromaPos<<8 ; sel11 = contrastPos | chromaNeg<<8 (pack loop 0xb2bf38/0xb2bf98)
open(sys.argv[3], 'wb').write(post); open(sys.argv[4], 'wb').write(tables)
print(f"post {len(post)} B (12 ranges, 68 regs), tables {len(tables)} B; regs0-3 {[hex(x) for x in r[:4]]}")
