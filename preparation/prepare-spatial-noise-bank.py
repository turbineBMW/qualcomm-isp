#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Prepare rear spatial denoising from a qualified P010, TF-disabled owner bank.

Use physical sensor gain for ANR, then raise only the chroma filter's Y/U/V
edge-detection thresholds. Preserve luma thresholds, color, tone and sharpening.
No hardware or installation access; generated proprietary assets stay local.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import DLL_SHA256, anr_programs, anr_reserve, descriptor, load, require_identity, sha
from sensor_layout import LAYOUTS
from chromatix import Chromatix

adaptive = load('../runtime/ipe-adaptive')


def patch_iq(data, fw):
    if len(data) != 6196 or len(fw) != 0xe0:
        raise ValueError('Unexpected IQ/ANR firmware layout')
    iq = bytearray(data)
    if any(struct.unpack_from('<I', iq, 0x578 + i * 0x198)[0] for i in range(4)):
        raise ValueError('Expected temporal filtering disabled')
    iq[0x498:0x578] = fw
    for i in range(4):
        struct.pack_into('<I', iq, 0x498 + i * 0x38, int(i < 3))
    return iq


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'tuning', 'dll', 'oracle', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    out = a.output.absolute()
    out = out.parent.resolve() / out.name
    if os.geteuid() == 0 or out.exists() or out.is_symlink() or out.is_relative_to(HERE.parent):
        raise ValueError('Run unprivileged with a new output outside the repository')
    require_identity(a.dll, DLL_SHA256, 'camera DLL')
    require_identity(a.tuning, LAYOUTS['ov13858']['sha256'], 'rear tuning')
    source = adaptive.Bank(a.assets / 'bank')
    if (source.info.get('precision') != 'p010-internal' or
            source.info.get('colour_stage') != 'ife' or
            source.info.get('temporal_filter') != 'disabled' or
            source.info.get('dll_sha256') != DLL_SHA256 or
            source.info.get('spatial_noise')):
        raise ValueError('Expected unmodified qualified rear motion bank')
    awb = json.loads((a.assets / 'awb-reference.json').read_text())
    if awb.get('source_sha256') != LAYOUTS['ov13858']['sha256']:
        raise ValueError('Expected rear sensor assets')
    out.mkdir(parents=True)
    build = out / 'preparation'
    build.mkdir()
    (out / 'bank').mkdir()
    for name in ('ife.yaml', 'awb-reference.json'):
        shutil.copyfile(a.assets / name, out / name)
    c = Chromatix(a.tuning)
    colour = load('parity-colour')
    mod = colour.get_module(c, 'anr10', 0)
    regions = [c.data(x[1], x[2]) for x in mod if x[0] == 'region' and x[2] == 8640]
    if len(regions) != 7:
        raise ValueError('Unexpected ANR tuning layout')
    intervals = colour.triggers(c, mod, 7)
    reserve = build / 'reserve.bin'
    reserve.write_bytes(anr_reserve(descriptor(c, 'anr10_ipe_v2', 4, 964)))
    env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
    env['SLRUN_QUIET'] = '1'

    def run(log, *args, overrides=None):
        local_env = dict(env)
        if overrides:
            local_env['SLRUN_CHROM_SET'] = overrides
        result = subprocess.run([str(a.oracle.resolve()), str(a.dll.resolve()),
            *map(str, args)], env=local_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=True, timeout=30)
        log.write_text(result.stdout)
        return result.stdout

    def calculate(d, gain, scale):
        d.mkdir()
        lo, hi, t = colour.choose(intervals, gain)
        (d / 'left.bin').write_bytes(regions[lo])
        (d / 'right.bin').write_bytes(regions[hi])
        if lo == hi:
            (d / 'region.bin').write_bytes(regions[lo])
        else:
            run(d / 'interp.log', 'interp', 'anr', d / 'left.bin',
                d / 'right.bin', t, d / 'region.bin')
        args = ['anr', d / 'region.bin', reserve, 3, 3840, 2176, 10,
                d / 'regs.bin', d / 'fw.bin', d / 'chrom.bin']
        log = run(d / 'anr.log', *args)
        if 'anr calc rc=1' not in log or 'anr flow rc=0' not in log:
            raise ValueError('ANR calculation failed')
        if scale != 1:
            chrom = (d / 'chrom.bin').read_bytes()
            if len(chrom) != 3 * 0x954:
                raise ValueError('Unexpected ANR chromatix layout')
            changes = []
            # NcLib ANR v10 pass records; chroma filter detection LUTs only.
            for ipass in range(3):
                for offset, maximum in ((1960, 1023), (2076, 255), (2200, 255)):
                    for i in range(17):
                        pos = ipass * 0x954 + offset + i * 4
                        value = struct.unpack_from('<I', chrom, pos)[0]
                        if value > maximum:
                            raise ValueError('Unexpected chroma threshold range')
                        changes.append(f'{pos}={min(maximum, int(value * scale + .5))}')
            log = run(d / 'scaled.log', *args, overrides=','.join(changes))
            if 'anr calc rc=1' not in log or 'anr flow rc=0' not in log:
                raise ValueError('Scaled ANR calculation failed')
        return anr_programs((d / 'regs.bin').read_bytes()), (d / 'fw.bin').read_bytes()

    gains = []
    for gi, gain in enumerate(source.info['gains']):
        baseline, fw = calculate(build / f'baseline-g{gi:02d}',
            max(1, gain / 2) if gain >= 8 else gain, 1)
        # Reproduce all source ANR programs and IQ patches before changing gain.
        for entry in source.info['entries']:
            if entry['gain_index'] != gi:
                continue
            d = a.assets / 'bank' / entry['id']
            for i in range(3):
                if baseline[i] != (d / f'nps-anr-p{i}.bin').read_bytes():
                    raise ValueError(f'Source ANR reproduction failed: {entry["id"]}')
            for name in ('iq-ltm.bin', 'seed-iq.bin'):
                data = (d / name).read_bytes()
                if patch_iq(data, fw) != data:
                    raise ValueError(f'Source IQ reproduction failed: {entry["id"]}')
        scale = 1 + max(0, min(1, math.log2(gain / 2) / 2))
        gains.append(calculate(build / f'g{gi:02d}', gain, scale))

    def prepare(old):
        entry = json.loads(json.dumps(old))
        d = out / 'bank' / entry['id']
        d.mkdir()
        for name in entry['files']:
            shutil.copyfile(a.assets / 'bank' / entry['id'] / name, d / name)
        regs, fw = gains[entry['gain_index']]
        for i, data in enumerate(regs):
            (d / f'nps-anr-p{i}.bin').write_bytes(data)
        maximum = 0
        for cfg, iq, strip in (('cfg.bin', 'iq-ltm.bin', 'striping.bin'),
                               ('seed-cfg.bin', 'seed-iq.bin', 'seed-striping.bin')):
            (d / iq).write_bytes(patch_iq((d / iq).read_bytes(), fw))
            log = run(d / (strip + '.log'), 'run', d / cfg, d / iq,
                      '0x60800', 0, d / strip, 1, 3840, 2176)
            scratch = re.search(r'scratchBufferSize=(\d+)', log)
            if 'Execute rc=0 ' not in log or not scratch:
                raise ValueError('Striping calculation failed')
            maximum = max(maximum, int(scratch[1]))
        if maximum > 6 << 20:
            raise ValueError('Scratch allocation exceeded')
        entry['scratch_required'] = maximum
        entry['files'] = {name: dict(size=(d / name).stat().st_size,
                                   sha256=sha(d / name)) for name in entry['files']}
        return entry

    with ThreadPoolExecutor(2) as pool:
        entries = list(pool.map(prepare, source.info['entries']))
    info = json.loads(json.dumps(source.info))
    info.update(entries=entries, anr_lighter_from=None, anr_lighter_ev=0, spatial_noise=dict(
        anr_gain='physical', chroma_threshold_scale='1 below 2x, log ramp to 2 at 8x',
        bank_sha256=sha(a.assets / 'bank/bank.json'), tuning_sha256=sha(a.tuning),
        dll_sha256=sha(a.dll), oracle_sha256=sha(a.oracle),
        builder_sha256=sha(Path(__file__)),
        note='Spatial ANR only. TF remains disabled; color, tone, sharpening retained.'))
    (out / 'bank/bank.json').write_text(json.dumps(info, indent=2) + '\n')
    adaptive.Bank(out / 'bank')
    print(f'Verified {len(entries)} spatial noise entries: {out}')


if __name__ == '__main__':
    main()
