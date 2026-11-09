#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Make the IPE temporal filter blend, and keep faces natural: the final form of a program bank.

Temporal filter. The three-pass programs enable TF on the full and DC4
passes. In that form the filter never blends: a one-level step against a
steady reference comes out unblended and the output noise has no
frame-to-frame correlation (measured 2026-11-09 on the front bank; the
references are rewritten every frame and the quarter-size input and reference
agree, so the plumbing is not the cause). With TF on the full pass alone,
deciding for itself instead of taking indications from the DC4 pass, the same
probe blends and real frames carry a quarter to a third of their noise from
one frame to the next. Per entry:
  iq-ltm.bin     TfPassParameters (0x578 + pass * 0x198): pass 0 words 3/19/20
                 = 1/0/0 (no indications, no rank filter / low-pass on the
                 frame-similarity decision), pass 1 word 0 = 0 (TF off)
  nps-tf-p0.bin  TF_CONFIG_0: USEINDICATIONS, APPLYFSRANKFILTER, APPLYFSLPF off
  striping.bin   recalculated by the striping library for the new settings

Spatial relief. From 16x gain the full-strength spatial programs flatten skin
and hair into patches at native scale; one stop lower keeps them. The
temporal thresholds, however, must match the real noise or the filter stops
blending. So a row above 8x takes everything spatial (ANR, ASF, colour, seed
pass) from the row `relief` stops lower (ramping in from 8x to 16x) and keeps
its own temporal filter. The gain rows differ in iq-ltm.bin only inside the
TF parameters, so the row's own iq-ltm.bin is kept.

usage: temporal_blend.py <slrun> <camera DLL> <source bank> <new bank> [--spatial-relief-ev 1.0]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

TF = 0x578
PASS = 0x198
TF_END = TF + 4 * PASS
CLEAR = (1 << 2) | (1 << 19) | (1 << 20)
OWN = ('iq-ltm.bin', 'nps-tf-p0.bin', 'nps-tf-p1.bin')
STEPS_PER_EV = 4
KNEE = 3 * STEPS_PER_EV   # 8x


def spatial_row(gain_index, relief_ev):
    """Row that supplies the spatial programs: unchanged to 8x, `relief_ev` lower from 16x."""
    over = min(max(gain_index - KNEE, 0), STEPS_PER_EV)
    return gain_index - round(relief_ev * over)


def full_pass_only(iq, program, name):
    """Patch TfPassParameters and the full-pass TF program; returns the new bytes."""
    iq = bytearray(iq)
    words = lambda q: struct.unpack_from('<21I', iq, TF + q * PASS)
    if [words(q)[0] for q in range(3)] != [1, 1, 0] or words(0)[3] or words(1)[3]:
        raise ValueError(f'{name}: not a full + DC4 temporal filter configuration')
    for index, value in ((3, 1), (19, 0), (20, 0)):
        struct.pack_into('<I', iq, TF + index * 4, value)
    struct.pack_into('<I', iq, TF + PASS, 0)
    program = bytearray(program)
    header, offset, config0 = struct.unpack_from('<3I', program)
    if (header, offset) != (0x0300005e, 0x5868) or config0 & CLEAR != CLEAR or config0 & 1:
        raise ValueError(f'{name}: unexpected temporal filter program')
    struct.pack_into('<I', program, 8, config0 & ~CLEAR)
    return bytes(iq), bytes(program)


def restripe(entry, oracle, dll, width, height):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
    env['SLRUN_QUIET'] = '1'
    result = subprocess.run([str(oracle), str(dll), 'run', str(entry / 'cfg.bin'), str(entry / 'iq-ltm.bin'), '0x60800', '0',
                             str(entry / 'striping.bin'), '1', str(width), str(height)],
                            env=env, capture_output=True, text=True, timeout=60)
    if result.returncode or 'Execute rc=0 ' not in result.stdout or 'numPasses=3' not in result.stdout:
        raise RuntimeError(f'{entry.name}: striping failed: {result.stdout[-400:]} {result.stderr[-400:]}')


def convert_bank(source, target, oracle, dll, relief_ev=1.0):
    """Write the blending bank `target` from `source`; returns the new manifest (also written)."""
    source, target = Path(source), Path(target)
    info = json.loads((source / 'bank.json').read_text())
    if info.get('temporal_filter'):
        raise ValueError('Bank was already converted')
    if not 0 <= relief_ev <= 1:
        raise ValueError('Spatial relief must be 0..1 stops')
    target.mkdir(parents=True)
    for entry in info['entries']:
        gi, ti = entry['gain_index'], entry['tone_index']
        donor = source / f'g{spatial_row(gi, relief_ev):02d}-t{ti:02d}'
        own = source / entry['id']
        directory = target / entry['id']
        directory.mkdir()
        for name in entry['files']:
            shutil.copyfile((own if name in OWN else donor) / name, directory / name)
        for name in ('experiment.json', 'gamma-contract.json'):
            if (own / name).exists():
                shutil.copyfile(own / name, directory / name)
        own_iq, donor_iq = (own / 'iq-ltm.bin').read_bytes(), (donor / 'iq-ltm.bin').read_bytes()
        if own_iq[:TF] != donor_iq[:TF] or own_iq[TF_END:] != donor_iq[TF_END:]:
            raise ValueError(f"{entry['id']}: gain rows differ outside the temporal filter parameters")
        iq, program = full_pass_only(own_iq, (own / 'nps-tf-p0.bin').read_bytes(), entry['id'])
        (directory / 'iq-ltm.bin').write_bytes(iq)
        (directory / 'nps-tf-p0.bin').write_bytes(program)
        restripe(directory, oracle, dll, info['width'], info['height'])
        for name in entry['files']:
            data = (directory / name).read_bytes()
            entry['files'][name] = dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    info['temporal_filter'] = 'full pass only, own decisions'
    info['spatial_relief_ev'] = relief_ev
    info['spatial_relief_from'] = KNEE
    info['converter_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (target / 'bank.json').write_text(json.dumps(info, indent=2) + '\n')
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('oracle', type=Path)
    parser.add_argument('dll', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    parser.add_argument('--spatial-relief-ev', type=float, default=1.0)
    a = parser.parse_args()
    info = convert_bank(a.source.resolve(), a.target.resolve(), a.oracle.resolve(), a.dll.resolve(), a.spatial_relief_ev)
    print(f"{len(info['entries'])} entries converted into {a.target}")


if __name__ == '__main__':
    main()
