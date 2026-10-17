#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Generate one qualified 4K gain/tone entry from explicit local inputs."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import load, require_identity, DLL_SHA256, TUNING_SHA256

def tmc_inputs(c, colour):
    """Extract the base SW TMC141 descriptor and explicit region 0."""
    module = colour.get_module(c, 'tmc14', 1)
    regions = [c.data(p[1], p[2]) for p in module if p[0] == 'region' and p[2] == 368]
    descriptors = []
    for off in range(0xd0 + 56 * len(c.params), sum(c.sec[0]) - 55, 56):
        name = c.f[off:off + 32].split(b'\0')[0]
        selector, data_off, size = struct.unpack_from('<III', c.f, off + 40)
        if name == b'tmc14_sw_v2' and selector == 5:
            descriptors.append(c.data(data_off, size))
    if len(regions) != 6 or len(descriptors) != 1 or len(descriptors[0]) != 84:
        raise ValueError('Unexpected base TMC141 tuning layout')
    return regions[0], descriptors[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'oracle', 'dll', 'tuning', 'ife', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--gain', type=float, required=True)
    parser.add_argument('--drc', type=float, required=True)
    args = parser.parse_args()
    for name in ('inputs', 'oracle', 'dll', 'tuning', 'ife', 'out'):
        setattr(args, name, getattr(args, name).resolve())
    require_identity(args.dll, DLL_SHA256, 'camera DLL')
    require_identity(args.tuning, TUNING_SHA256, 'IMX681 tuning')
    gain = args.gain
    if not math.isfinite(gain) or not 1 <= gain <= 64 or not math.isfinite(args.drc) or not 1 <= args.drc <= 8:
        raise ValueError('Gain must be in 1..64 and DRC in 1..8')
    args.sharpness = 0.75
    args.cct = 5262.96630859375
    args.lux_index = 200
    args.tmc_drc_gain = args.drc
    args.tmc_dark_gain = 1
    args.anr_gain = max(1.0, gain / 2) if gain >= 8 else None
    args.gain_mode = 'interpolate'
    w, h = 3840, 2176
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    def run(label, *command):
        env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
        with (out / (label + '.log')).open('w') as log:
            subprocess.run([str(x) for x in command], cwd=out,
                           env={**env, 'SLRUN_QUIET': '1'}, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=30)
    oracle = [args.oracle, args.dll]
    py = [sys.executable, '-I']
    colour = load('parity-colour')
    source = args.tuning
    c = colour.Chromatix(source)
    tone_regions = load('tone-regions')
    tone_gain = args.drc
    tone_selection = {kind: tone_regions.select(c, colour, kind, tone_gain, args.lux_index,
                                               out, oracle, run) for kind in ('tmc', 'ltm')}
    selections = {}
    for kind, name, size in [('anr', 'anr10', 8640), ('tf', 'tf21', 4688), ('asf', 'asf34', 4036)]:
        module = colour.get_module(c, name, 0)
        regions = [c.data(p[1], p[2]) for p in module if p[0] == 'region' and p[2] == size]
        intervals = colour.triggers(c, module, 7)
        if len(regions) != 7:
            raise ValueError(f'Unexpected {kind} regions')
        select_gain = args.anr_gain if kind == 'anr' and args.anr_gain is not None else gain
        lo, hi, ratio = colour.choose(intervals, select_gain)
        selections[kind] = dict(regions=[lo, hi], ratio=ratio, intervals=intervals, gain=select_gain)
        left, right, selected = (out / (kind + suffix) for suffix in ('-left.bin', '-right.bin', '-region.bin'))
        left.write_bytes(regions[lo]); right.write_bytes(regions[hi])
        if lo == hi:
            selected.write_bytes(regions[lo])
        else:
            run(kind + '-interpolation', *oracle, 'interp', kind, left, right, ratio, selected)
    for name in ('cfg.bin', 'seed-cfg.bin'):
        (out / name).write_bytes((args.inputs / name).read_bytes())
    run('anr', *oracle, 'anr', out / 'anr-region.bin', args.inputs / 'anr-reserve.bin', 3, w, h, 8,
        out / 'anr-regs.bin', out / 'anr-fw.bin', out / 'anr-chrom.bin')
    run('tf', *oracle, 'tf', out / 'tf-region.bin', args.inputs / 'tf-reserve.bin', 3, w, h, 1,
        out / 'tf-regs.bin', out / 'tf-fw.bin', out / 'tf-ref.bin', out / 'tf-chrom.bin')
    run('nps', *py, HERE / 'nps-from-dll.py', 3, out / 'anr-regs.bin', out / 'anr-fw.bin',
        out / 'tf-regs.bin', out / 'tf-fw.bin', out / 'tf-ref.bin', args.inputs / 'iq-full.bin', out / 'iq-nps.bin', out / 'nps')
    iq = bytearray((out / 'iq-nps.bin').read_bytes())
    struct.pack_into('<I', iq, 0x578 + 2 * 0x198, 0)  # DC16 has no reference output.
    (out / 'iq-nps.bin').write_bytes(iq)
    seed = bytearray((args.inputs / 'iq-seed.bin').read_bytes())
    seed[0x498:0x578] = iq[0x498:0x578]
    (out / 'seed-iq.bin').write_bytes(seed)
    run('dsx', *oracle, 'dsx', args.inputs / 'dsx-region.bin', w, h, w // 4, h // 4, 4.0,
        out / 'dsx-regs.bin', out / 'dsx-packed.bin', args.inputs / 'ds4-coefficients.bin')
    run('dsx-pack', *py, HERE / 'dsx-from-dll.py', out / 'dsx-regs.bin', out / 'dsx-packed.bin',
        out / 'iq-nps.bin', out / 'iq-dsx.bin', out / 'dsx.bin', out / 'dsx-luts.bin')

    run('asf', *oracle, 'asf34', out / 'asf-region.bin', out / 'asf-unpacked.bin', out / 'asf-regs.bin', args.sharpness, 1, w, h)
    run('asf-pack', *py, HERE / 'asf-from-dll.py', out / 'asf-unpacked.bin', out / 'asf-regs.bin', out / 'asf.bin', out / 'asf-luts.bin')
    run('colour', *py, HERE / 'parity-colour.py', source, out / 'iq-dsx.bin', out,
        '--cct', args.cct, '--lux-index', args.lux_index, '--input-colour', 'uncorrected', '--asf-program', out / 'asf.bin')
    adrc_args = []
    tmc = dict(mode='disabled')
    if args.tmc_drc_gain is not None:
        _, descriptor = tmc_inputs(c, colour)
        (out / 'tmc-descriptor.bin').write_bytes(descriptor)
        run('tmc', *oracle, 'tmc141', out / 'tmc-region.bin', out / 'tmc-descriptor.bin',
            args.tmc_drc_gain, args.tmc_dark_gain, out / 'adrc.bin')
        adrc_args = [out / 'adrc.bin']
        tmc = dict(mode='anchor-only', selection=tone_selection['tmc'], drc_gain=args.tmc_drc_gain,
                   dark_gain=args.tmc_dark_gain, histogram=False, temporal_adaptation=False,
                   extra_frame_controls={'0x480': 1, '0x484': 1, '0x48c': 50, '0x490': 1},
                   note='Explicit experimental gains and frame controls; AE sources are not calibrated')
    run('ltm', *oracle, 'ltm161', out / 'ltm-region.bin', args.inputs / 'ltm-reserve.bin', w, h,
        out / 'ltm-unpacked.bin', out / 'ltm-regs.bin', out / 'ltm-luts.bin', *adrc_args)
    run('ltm-pack', *py, HERE / 'ltm-from-dll.py', out / 'ltm-regs.bin', out / 'iq.bin', out / 'iq-ltm.bin', out / 'pre.bin', out / 'pre-ltm.bin')
    gamma_chain = load('gamma-chain')
    ife_curves = colour.regions(c, colour.get_module(c, 'gamma15', 1))
    if len(ife_curves) != 1:
        raise ValueError('Unexpected IFE gamma selection layout')
    gamma_module = colour.get_module(c, 'gamma15', 2)
    selected_gamma = colour.blend(colour.regions(c, gamma_module),
                                  colour.choose(colour.triggers(c, gamma_module, 2), args.lux_index))
    gamma_contract = gamma_chain.verify(args.ife, list(ife_curves[0]),
                                       (out / 'iq-ltm.bin').read_bytes(), (out / 'gamma.bin').read_bytes(),
                                       colour.gamma_lut(selected_gamma),
                                       hashlib.sha256(args.dll.read_bytes()).hexdigest())
    (out / 'gamma-contract.json').write_text(json.dumps(gamma_contract, indent=2) + '\n')
    for label, cfg, iqfile in [('striping', 'cfg.bin', 'iq-ltm.bin'), ('seed-striping', 'seed-cfg.bin', 'seed-iq.bin')]:
        run(label, *oracle, 'run', out / cfg, out / iqfile, '0x60800', 0, out / (label + '.bin'), 1, w, h)
        if 'Execute rc=0 ' not in (out / (label + '.log')).read_text():
            raise RuntimeError(f'{label} generation failed')
    record = dict(width=w, height=h, gain=gain, gain_selection=selections,
                  cct=args.cct, lux_index=args.lux_index, sharpness=args.sharpness,
                  tone_selection=tone_selection, tmc=tmc, gamma_contract=gamma_contract)
    (out / 'experiment.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    main()
