#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""LTM 1.6.1 oracle output to firmware IQ and pre-LTM host registers.

slrun-c1 ltm161 writes 18 packed register words then 104 firmware IQ bytes.
The firmware supplies the remaining geometry/config registers per stripe.
LUTs: pre-LTM type 17, DMI 0x15508, selectors 1..7, lengths
96,256,256,512,256,64,64 (weight, LA, curve, scale, mask, LCE+, LCE-).
"""
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ipeprog import regcont

if __name__ == '__main__':
    if len(sys.argv) != 6:
        raise SystemExit('usage: ltm-from-dll.py regs.bin iq-in iq-out pre-in pre-out')
    regs, iq_in, iq_out, pre_in, pre_out = map(Path, sys.argv[1:])
    data = regs.read_bytes()
    iq = bytearray(iq_in.read_bytes())
    if len(data) != 176 or len(iq) != 0x1834:
        raise SystemExit('Unexpected register/IQ size')
    iq[0x1238:0x12a0] = data[72:]
    # IPELTM161Titan680::WriteConfigCmds: these two words only.
    pre = pre_in.read_bytes() + regcont(0x155a0, struct.unpack_from('<2I', data, 0x3c))
    iq_out.write_bytes(iq)
    pre_out.write_bytes(pre)
    print('LTM IQ', struct.unpack('<26I', data[72:]), 'pre bytes', len(pre))
