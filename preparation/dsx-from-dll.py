#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""DSX from the DLL: `slrun dsx` output (DSX_REG 0xb58 + packed regs 0x200 + 8 LUTs x 0x400) -> IPE DSX register program
   (IPEDSX101Titan680 range writer 0xc12010: 8 REG_CONT ranges at 0x8a68..0x8b74 from packed-reg offsets), LUT table file
   (sels 1..4 at DMI cfg 0x8a08, 0x400 each: luma h, luma v, chroma h, chroma v) and the IQ dsxParameters[pass] @0xf94
   (IpeDsxModuleParameters x2 {mode, paddingWeightsEn, scaleRatioX, scaleRatioY, startLocationX, startLocationY,
   outputWidthX, outputWidthY} from DSX_REG).
usage: dsx-from-dll.py <dsxreg.bin> <packed.bin> <iq_in> <iq_out> <prog_out> <luts_out> [pass=0]"""
import struct, sys
dsxreg = open(sys.argv[1], 'rb').read(); packed = open(sys.argv[2], 'rb').read()
iq_in, iq_out, prog_out, luts_out = sys.argv[3:7]; p = int(sys.argv[7]) if len(sys.argv) > 7 else 0
RANGES = [(0x8a68, 0x00, 5), (0x8a7c, 0x14, 13), (0x8ab0, 0x48, 9), (0x8ad8, 0x6c, 4), (0x8aec, 0x7c, 18), (0x8b34, 0xc4, 2), (0x8b3c, 0xcc, 3), (0x8b48, 0xd8, 11)]
def regcont(off, vals): return struct.pack('<II', (3 << 24) | len(vals), off) + b''.join(struct.pack('<I', v & 0xffffffff) for v in vals)
prog = b''.join(regcont(r, struct.unpack_from('<%dI' % n, packed, o)) for r, o, n in RANGES)
open(prog_out, 'wb').write(prog)
open(luts_out, 'wb').write(packed[0x200:0x200 + 4 * 0x400])
iq = bytearray(open(iq_in, 'rb').read())
w = struct.unpack_from('<20I', dsxreg); pen = struct.unpack_from('<2I', dsxreg, 0x950)
# DSX_REG: enY enC modeY modeC startXY(luma) startXY(chroma) scaleXY(luma) scaleXY(chroma) outWH(luma) outWH(chroma) inWH..
luma = (w[2], pen[0], w[8], w[9], w[4], w[5], w[12], w[13]); chroma = (w[3], pen[1], w[10], w[11], w[6], w[7], w[14], w[15])
struct.pack_into('<8I', iq, 0xf94 + p * 0x40, *luma); struct.pack_into('<8I', iq, 0xf94 + p * 0x40 + 0x20, *chroma)
open(iq_out, 'wb').write(iq)
print(f"dsx pass {p}: luma {luma} chroma {chroma}; program {len(prog)} B -> {prog_out}; luts 4 x 1 KB -> {luts_out}; iq -> {iq_out}")
