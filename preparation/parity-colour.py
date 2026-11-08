#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Extract the base Windows IPE colour tuning for an explicit CCT/lux index.

Lab only. The proprietary Chromatix stays local. 'prepared' input has the
current IFE matrix/tone curve applied: disable IPE CC/GLUT rather than applying
them twice. 'uncorrected' emits the Windows CC/GLUT and requires a separately
prepared input. Neither mode implements LTM or claims Windows photo parity.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE))
from chromatix import Chromatix
from sensor_layout import identify
from ipeprog import regcont, cc_matrix


def choose(intervals, value):
    """Hold inside a region; linearly interpolate across the gap to the next.

    Matches the local IQSettingUtils::GetIndexPtTrigger implementation.
    """
    if not math.isfinite(value):
        raise ValueError('Trigger must be finite')
    for i, (start, end) in enumerate(intervals):
        if value <= end or i == len(intervals) - 1:
            return i, i, 0.0
        next_start = intervals[i + 1][0]
        if value < next_start:
            return i, i + 1, (value - end) / (next_start - end)
    raise ValueError('Empty trigger table')


def get_module(c, name, occurrence):
    return [m for m in c.modules() if any(p[0] == 'mod_' + name + '_trigger_data' for p in m)][occurrence]


def regions(c, module):
    return [struct.unpack('<%df' % (p[2] // 4), c.data(p[1], p[2]))
            for p in module if p[0] == 'region' and p[2]]


def triggers(c, module, count):
    p = next(p for p in module if p[0] == 'trigger' and p[2] == count * 24)
    return [struct.unpack_from('<ff', c.data(p[1], p[2]), i * 24) for i in range(count)]


def blend(values, selection):
    lo, hi, ratio = selection
    return [a * (1 - ratio) + b * ratio for a, b in zip(values[lo], values[hi])]


def select_colour(c, module, lux_rows, fields, cct, lux_index):
    """Follow each lux branch's own CCT references in the qualified base tree.

    Upper controls are singleton branches in both supported sensor files.
    CCM and CV may have different CCT intervals, including between lux rows.
    Serialized child/region references are zero-based parameter IDs.
    """
    if not all(math.isfinite(v) for v in (cct, lux_index)) or cct <= 0:
        raise ValueError('Invalid colour trigger input')
    controls = next(p for p in module if p[0] == 'control_var_type')
    if c.data(controls[1], controls[2]) != struct.pack('<5I', 2, 5, 100, 0, 6):
        raise ValueError('Unsupported colour trigger axes')
    byid = {p[3]: p for p in module}
    root = next(p for p in module if p[0].startswith('mod_'))
    shape = (1, 1, 1, lux_rows, 4)
    decisions, weights = [], {}

    def read(part, depth):
        if part[2] != shape[depth] * 24:
            raise ValueError('Unexpected colour trigger count')
        nodes = []
        previous = -math.inf
        for start, end, count, child_id, has_region, region_id in struct.iter_unpack('<ffIIII', c.data(part[1], part[2])):
            if not all(math.isfinite(v) for v in (start, end)) or start > end or start <= previous:
                raise ValueError('Invalid colour trigger intervals')
            previous = end
            child, region = byid[child_id + 1], byid[region_id + 1]
            node = dict(interval=(start, end))
            if depth == 4:
                if count or has_region != 1 or child[2] or region[0] != 'region' or region[2] != fields * 4:
                    raise ValueError('Invalid colour leaf reference')
                values = struct.unpack('<%df' % fields, c.data(region[1], region[2]))
                if not all(math.isfinite(v) for v in values):
                    raise ValueError('Nonfinite colour coefficient')
                node.update(values=values, region_id=region[3])
            else:
                if count != shape[depth + 1] or has_region or region[2] or child[0] != 'trigger':
                    raise ValueError('Invalid colour child reference')
                node['children'] = read(child, depth + 1)
            nodes.append(node)
        return nodes

    def visit(nodes, depth, path, weight):
        selection = choose([n['interval'] for n in nodes], lux_index if depth == 3 else cct) if depth >= 3 else (0, 0, 0.0)
        lo, hi, ratio = selection
        if depth >= 3:
            decisions.append(dict(path=path, axis='lux_index' if depth == 3 else 'cct',
                                  intervals=[n['interval'] for n in nodes], selection=selection))
        values = []
        for index, factor in ([(lo, 1.0)] if lo == hi else [(lo, 1 - ratio), (hi, ratio)]):
            node = nodes[index]
            if depth == 4:
                values.append(node['values'])
                key = str(node['region_id'])
                weights[key] = weights.get(key, 0.0) + weight * factor
            else:
                values.append(visit(node['children'], depth + 1, path + [index], weight * factor))
        return list(values[0]) if lo == hi else blend(values, (0, 1, ratio))

    result = visit(read(root, 0), 0, [], 1.0)
    return result, dict(decisions=decisions, leaf_weights=weights)


def round_hw(x):
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def ce(values):
    r, g, b, yoff, ap, am, bp, bm, cp, cm, dp, dm, cb, cr = values
    q = lambda x: round_hw(x * 256) & 0xfff
    return regcont(0x11668, [(round_hw(yoff) & 0x7ff) << 21,
                           q(r) | (q(g) << 16), q(b), q(ap) | (q(am) << 16),
                           q(bp) | (q(bm) << 16), q(cp) | (q(cm) << 16),
                           q(dp) | (q(dm) << 16), (round_hw(cb) & 0x1fff) << 19,
                           (round_hw(cr) & 0x1fff) << 19])


def gamma_lut(values):
    if len(values) != 771:
        raise ValueError('Expected three 257-knot gamma curves')
    entries = []
    for ch in range(3):
        v = values[ch * 257:(ch + 1) * 257]
        for i in range(256):
            x = min(1023, max(0, round_hw(v[i])))
            y = min(1024 if i == 255 else 1023, max(0, round_hw(v[i + 1])))
            delta = min(511, max(-512, y - x))
            entries.append(x | ((delta & 1023) << 10))
    return struct.pack('<768I', *entries)


def cst(c):
    # Descriptor format: 32-byte name + version/selector + offset/size/id.
    start = 0xd0 + 56 * len(c.params)
    end = sum(c.sec[0])
    hits = []
    for off in range(start, end - 55, 56):
        name = c.f[off:off + 32].split(b'\0')[0]
        if name == b'cst12_ipe':
            data_off, size = struct.unpack_from('<II', c.f, off + 44)
            hits.append(c.data(data_off, size))
    if len(hits) != 1 or len(hits[0]) != 108:
        raise ValueError('Unexpected CST descriptor')
    blob = hits[0]
    matrix = struct.unpack_from('<9f', blob, 48)
    # Windows reserve is 14-bit. The measured PPS instance uses 10-bit
    # clamps/offsets; fold its input-centre subtraction into output offsets.
    out_offset = struct.unpack_from('<3i', blob, 84)
    in_offset = struct.unpack_from('<3i', blob, 96)
    regs = []
    for row in range(3):
        m = matrix[row * 3:row * 3 + 3]
        q = [round_hw(x * 1024) & 0x1fff for x in m]
        o = round_hw((out_offset[row] - sum(a * b for a, b in zip(m, in_offset))) / 16)
        regs += [q[0] | (q[1] << 16), q[2], (o & 0x7ff) << 21, 0, 0, 1023 << 22]
    return regcont(0x11068, regs), list(matrix)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('chromatix', type=Path)
    p.add_argument('iq', type=Path)
    p.add_argument('outdir', type=Path)
    p.add_argument('--cct', type=float, required=True)
    p.add_argument('--lux-index', type=float, required=True)
    p.add_argument('--input-colour', choices=('prepared', 'uncorrected'), required=True)
    p.add_argument('--asf-program', type=Path)
    a = p.parse_args()
    c = Chromatix(a.chromatix)
    layout = identify(c.f)[1]
    ccmod, gammod, cvmod = [get_module(c, n, k) for n, k in [('cc13', 2), ('gamma15', 2), ('cv12', 0)]]
    gsel = choose(triggers(c, gammod, layout['gamma_rows']), a.lux_index)
    matrix, ccsel = select_colour(c, ccmod, layout['cc_rows'], 12, a.cct, a.lux_index)
    gamma = blend(regions(c, gammod), gsel)
    cv, cvsel = select_colour(c, cvmod, layout['cv_rows'], 14, a.cct, a.lux_index)
    pre, cstmatrix = cst(c)
    iq = bytearray(a.iq.read_bytes())
    if len(iq) != 0x1834:
        raise ValueError('Unexpected IpeIQSettings length')
    full_colour = a.input_colour == 'uncorrected'
    for off, en in [(0xc44, 1), (0xca4, full_colour), (0xca8, full_colour), (0xcb0, 1)]:
        struct.pack_into('<I', iq, off, int(en))
    post = cc_matrix([matrix[i:i + 3] for i in range(0, 9, 3)], matrix[9:]) if full_colour else b''
    post += ce(cv)
    if a.asf_program:
        post += a.asf_program.read_bytes()
    elif struct.unpack_from('<I', iq, 0xcf0)[0]:
        raise ValueError('ASF enabled in IQ: supply --asf-program and its LUTs at execution')
    a.outdir.mkdir(parents=True, exist_ok=True)
    for name, data in [('iq.bin', iq), ('pre.bin', pre), ('post.bin', post), ('gamma.bin', gamma_lut(gamma))]:
        (a.outdir / name).write_bytes(data)
    report = dict(source_sha256=hashlib.sha256(c.f).hexdigest(), input_colour=a.input_colour,
                  cct=a.cct, lux_index=a.lux_index, cc_selection=ccsel, gamma_selection=gsel,
                  cv_selection=cvsel, cc=matrix, cv=cv, cst_matrix=cstmatrix,
                  ltm='not enabled; this file uses ltm16, not ltm13',
                  note='CCT/lux are explicit experimental inputs, not measured frame metadata')
    (a.outdir / 'selection.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
