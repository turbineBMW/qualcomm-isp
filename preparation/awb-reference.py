#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Decode local Windows AWB references and query its ratio-to-CCT oracle.

These are tuning-reference estimates, not calibrated scene measurements.
No neutral-pixel classifier, per-device OTP or lens-shading correction is applied.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from chromatix import Chromatix

LABELS = ('High', 'D75', 'D65', 'D50', 'CW', 'Fluorescent', 'TL84', 'Incandescent', 'Horizon', 'Low')
# CSFStatDistV1 initializer, DLL RVA 0x6c3a70; the fluorescent branch is separate.
CCTS = (12000, 7500, 6500, 5000, 4100, 4000, 3800, 2850, 2300, 1500)


def module_blob(c, name, size):
    hits = []
    for off in range(0xd0 + 56 * len(c.params), sum(c.sec[0]) - 55, 56):
        label = c.f[off:off + 32].split(b'\0')[0].decode()
        selector, data_off, length = struct.unpack_from('<III', c.f, off + 40)
        if label == name and selector == 6:
            hits.append(c.data(data_off, length))
    if len(hits) != 1 or len(hits[0]) != size:
        raise ValueError(f'Unexpected {name} descriptor')
    expected = (1, 1, 0) if name == 'initDataV1' else (1, 0, 0)
    if struct.unpack_from('<3I', hits[0], 8) != expected:
        raise ValueError(f'Unsupported {name} revision')
    return hits[0]


def distance_tree(c):
    blob = module_blob(c, 'SFDistWVV1', 40)
    count, ref = struct.unpack_from('<II', blob, 32)

    def walk(n, ref, depth):
        # Serialized references are zero based; descriptor IDs are one based.
        p = c.byid[ref + 1]
        stride = 12 if depth == 2 else 16
        limit = (20, 30, 5)[depth]
        if p[0] != 'triggerData' or not 0 < n <= limit or p[2] != n * stride:
            raise ValueError('Unexpected AWB distance-weight tree')
        data = c.data(p[1], p[2])
        out = bytearray()
        for i in range(n):
            off = i * stride
            lo, hi = struct.unpack_from('<ff', data, off)
            if not all(map(math.isfinite, (lo, hi))) or lo > hi:
                raise ValueError('Invalid distance-weight interval')
            out += data[off:off + 12]
            if depth < 2:
                child_count, child_ref = struct.unpack_from('<II', data, off + 8)
                out += walk(child_count, child_ref, depth + 1)
            else:
                weight, = struct.unpack_from('<f', data, off + 8)
                if not math.isfinite(weight) or weight < 0:
                    raise ValueError('Invalid distance weight')
        return out

    return struct.pack('<I', count) + walk(count, ref, 0)


def extract(source):
    c = Chromatix(source)
    refs = module_blob(c, 'refPtV1', 144)
    mwb = module_blob(c, 'MWBGainV1', 144)
    init = module_blob(c, 'initDataV1', 48)
    cal = module_blob(c, 'sensorCalV1', 172)
    entries = []
    for i, label in enumerate(LABELS):
        rg, bg = struct.unpack_from('<ff', refs, 24 + i * 8)
        r, g, b = struct.unpack_from('<fff', mwb, 24 + i * 12)
        if not all(math.isfinite(x) and x > 0 for x in (rg, bg, r, g, b)):
            raise ValueError('Invalid reference or manual gain')
        if not math.isclose(r * rg, g, abs_tol=3e-6) or not math.isclose(b * bg, g, abs_tol=3e-6):
            raise ValueError('AWB references and manual gains disagree')
        entries.append(dict(index=i, illuminant=label, nominal_cct=CCTS[i], rg=rg, bg=bg,
                            manual_gains=[r, g, b]))
    if any(struct.unpack_from('<10f', refs, 104)):
        raise ValueError('Additional AWB references need mapping')
    initial = struct.unpack_from('<4f', init, 24)
    ncal, = struct.unpack_from('<I', cal, 24)
    if ncal > 6:
        raise ValueError('Unexpected sensor calibration count')
    report = dict(source_sha256=hashlib.sha256(c.f).hexdigest(), references=entries,
                  initial_gains=list(initial[:3]), initial_cct=initial[3],
                  sensor_calibration=dict(enabled=bool(struct.unpack_from('<I', cal, 20)[0]),
                      count=ncal, ccts=list(struct.unpack_from('<6f', cal, 28)),
                      golden_ratios=[list(struct.unpack_from('<ff', cal, 52 + i * 8)) for i in range(15)],
                      per_device_otp_applied=False),
                  note='Reference-space mapping only; scene neutrality, unit calibration and shading are not established')
    return report, refs[24:104], distance_tree(c)


def query(source, ratios, out, oracle, dll):
    if not ratios or any(len(p) != 2 or not all(math.isfinite(v) and 0 < v <= 16 for v in p) for p in ratios):
        raise ValueError('Ratios must be finite, positive R/G and B/G pairs within 16')
    report, refs, dist = extract(source)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in [('references.bin', refs), ('distance-tree.bin', dist),
                       ('ratios.bin', b''.join(struct.pack('<ff', *p) for p in ratios))]:
        (out / name).write_bytes(data)
    with (out / 'oracle.log').open('w') as log:
        subprocess.run([str(oracle), str(dll), 'awb-cct', str(out / 'references.bin'),
                        str(out / 'distance-tree.bin'), str(out / 'ratios.bin'), str(out / 'output.bin')],
                       env={**os.environ, 'SLRUN_QUIET': '1'}, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=30)
    data = (out / 'output.bin').read_bytes()
    if len(data) != 12 * len(ratios):
        raise ValueError('Unexpected AWB oracle output size')
    report['dll_sha256'] = hashlib.sha256(dll.read_bytes()).hexdigest()
    report['oracle_sha256'] = hashlib.sha256(oracle.read_bytes()).hexdigest()
    report['estimates'] = []
    for i, pair in enumerate(ratios):
        distance, cct, decision = struct.unpack_from('<ffI', data, i * 12)
        report['estimates'].append(dict(rg=pair[0], bg=pair[1], cct=cct,
                                        signed_reference_distance=distance, decision=LABELS[decision]))
    (out / 'reference.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def frame_ratios(log, frame=59):
    matches = list(re.finditer(rf'\bseq: {frame:06d}\b', log))
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one frame {frame} metadata block')
    block = re.split(r'\bseq: \d+\b', log[matches[0].end():], maxsplit=1)[0]
    match = re.search(r'\bColourGains = \[\s*([^,]+),\s*([^\]]+)\]', block)
    if not match:
        raise ValueError(f'Missing ColourGains for frame {frame}')
    gains = [float(x) for x in match.groups()]
    if not all(math.isfinite(v) and v > 0 for v in gains):
        raise ValueError('Invalid colour gains')
    return [1 / v for v in gains]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--oracle', type=Path, required=True)
    p.add_argument('--dll', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--ratios', type=float, nargs=2, metavar=('RG', 'BG'))
    g.add_argument('--capture-log', type=Path)
    g.add_argument('--check-references', action='store_true')
    a = p.parse_args()
    if a.capture_log:
        ratios = [frame_ratios(a.capture_log.read_text())]
    elif a.check_references:
        report, _, _ = extract(a.source)
        ratios = [[r['rg'], r['bg']] for r in report['references']]
    else:
        ratios = [a.ratios]
    report = query(a.source, ratios, a.out, a.oracle, a.dll)
    print(json.dumps(report['estimates'], indent=2))


if __name__ == '__main__':
    main()
