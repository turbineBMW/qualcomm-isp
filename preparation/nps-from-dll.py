#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Multi-pass NPS builder: ANR + TF registers/fw from `slrun anr|tf` with n passes -> IQ patch + per-pass programs.
usage: nps-from-dll.py <n> <anr_regs> <anr_fw> <tf_regs> <tf_fw> <tf_refine> <iq_in> <iq_out> <prefix> [--no-tf] [--no-refine]
  writes <prefix>-anr-p<k>.bin and <prefix>-tf-p<k>.bin for k < n; IQ: anrParameters @0x498 (EN k<n), tfParameters @0x578
  (EN k<n, useAnroAsImgInput=1), refinementParameters @0xe20 (zeroed with --no-refine)."""
import struct, sys
a = sys.argv[1:]; flags = [x for x in a if x.startswith('--')]; a = [x for x in a if not x.startswith('--')]
n = int(a[0]); anr_regs = open(a[1], 'rb').read(); anr_fw = open(a[2], 'rb').read(); tf_regs = open(a[3], 'rb').read(); tf_fw = open(a[4], 'rb').read(); tf_ref = open(a[5], 'rb').read()
iq_in, iq_out, prefix = a[6], a[7], a[8]; use_tf = '--no-tf' not in flags
def regcont(off, vals): return struct.pack('<II', (3 << 24) | len(vals), off) + b''.join(struct.pack('<I', v & 0xffffffff) for v in vals)
ANR = [(0x4870, 0x000, 1), (0x4880, 0x004, 0x20), (0x4900, 0x084, 1), (0x4940, 0x088, 0xc), (0x4970, 0x0b8, 0x16), (0x49d0, 0x110, 0xe), (0x4a28, 0x148, 0x2d), (0x4adc, 0x1fc, 0x7d), (0x4cf0, 0x3f0, 0x4b)]
for k in range(n):
    base = k * 0x51c
    open(f'{prefix}-anr-p{k}.bin', 'wb').write(b''.join(regcont(r, struct.unpack_from('<%dI' % c, anr_regs, base + o)) for r, o, c in ANR))
    base = k * 0x194
    open(f'{prefix}-tf-p{k}.bin', 'wb').write(regcont(0x5868, struct.unpack_from('<94I', tf_regs, base)) + regcont(0x59fc, struct.unpack_from('<7I', tf_regs, base + 94 * 4)))
iq = bytearray(open(iq_in, 'rb').read())
iq[0x498:0x498 + 0xe0] = anr_fw[:0xe0]
for q in range(4): struct.pack_into('<I', iq, 0x498 + q * 0x38, 1 if q < n else 0)
if use_tf:
    iq[0x578:0x578 + 0x660] = tf_fw[:0x660]
    for q in range(4):
        struct.pack_into('<I', iq, 0x578 + q * 0x198, 1 if q < n else 0)
        if q < n: struct.pack_into('<I', iq, 0x578 + q * 0x198 + 33 * 4, 1)   # useAnroAsImgInput
    iq[0xe20:0xe20 + 0x70] = bytes(0x70) if '--no-refine' in flags else tf_ref[:0x70]
open(iq_out, 'wb').write(iq)
print(f'{n} passes -> {prefix}-anr-p*.bin, {prefix}-tf-p*.bin; iq {iq_out} (tf {"on" if use_tf else "off"}, refine {"off" if "--no-refine" in flags else "from DLL"})')
