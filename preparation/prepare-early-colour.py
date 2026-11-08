#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Move the qualified rear color matrix to IFE and retain the P010 interior.

Requires libcamera's opt-in RAW CCM highlight model. Generated owner data must
remain local. The source asset bank and installed configuration are untouched.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'runtime'))
from inputs import load, require_identity
from sensor_layout import LAYOUTS

colour = load('parity-colour')
sets = load('prepare-colour-sets')
adaptive = load('../runtime/ipe-adaptive')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'tuning', 'ife', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    out = args.out.absolute()
    out = out.parent.resolve() / out.name
    if os.geteuid() == 0 or out.exists() or out.is_symlink() or out.is_relative_to(HERE.parent):
        raise ValueError('Run unprivileged with a new output outside the source repository')
    require_identity(args.tuning, LAYOUTS['ov13858']['sha256'], 'rear tuning')
    bank = adaptive.Bank(args.assets / 'bank')
    if bank.info.get('precision') != 'p010-internal' or bank.info.get('colour_stage', 'ipe') != 'ipe':
        raise ValueError('Expected the qualified P010 rear bank with late color correction')
    awb = json.loads((args.assets / 'awb-reference.json').read_text())
    if awb.get('source_sha256') != LAYOUTS['ov13858']['sha256']:
        raise ValueError('Source assets belong to another sensor')
    chrom = colour.Chromatix(args.tuning)
    matrix, selection = colour.select_colour(chrom, colour.get_module(chrom, 'cc13', 2),
        LAYOUTS['ov13858']['cc_rows'], 12, bank.info['cct'], bank.info['index'])
    if any(matrix[9:]) or not all(math.isfinite(v) and -8 <= v < 8 for v in matrix[:9]):
        raise ValueError('Unsupported matrix or offsets')
    if any(abs(sum(matrix[i:i+3]) - 1) > 0.02 for i in (0, 3, 6)):
        raise ValueError('Expected a neutral-preserving matrix')
    anchor = colour.cc_matrix([matrix[i:i+3] for i in (0, 3, 6)], matrix[9:])
    identity = colour.cc_matrix([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [0, 0, 0])
    profile = yaml.safe_load(args.ife.read_text())
    if profile.get('ccm_strength') != 0 or profile.get('raw_ccm_model', False):
        raise ValueError('Source IFE must have its color matrix disabled')
    if profile.get('ae', {}).get('raw_highlights') is not True:
        raise ValueError('Early correction requires RAW highlight protection')
    profile.update(ccm=matrix[:9], ccm_offsets=matrix[9:], ccm_strength=1.0, raw_ccm_model=True)
    # Check every source before writing any generated program.
    for entry in bank.info['entries']:
        data = (args.assets / 'bank' / entry['id'] / 'post.bin').read_bytes()
        sets.split_colour(data)
        if data[:len(anchor)] != anchor:
            raise ValueError('Source bank color matrix differs from the selected rear tuning')
    out.mkdir(parents=True)
    (out / 'bank').mkdir()
    shutil.copyfile(args.assets / 'awb-reference.json', out / 'awb-reference.json')
    (out / 'ife.yaml').write_text(yaml.safe_dump(profile, sort_keys=False))
    info = json.loads(json.dumps(bank.info))
    for entry in info['entries']:
        target = out / 'bank' / entry['id']
        target.mkdir()
        for name in entry['files']:
            shutil.copyfile(args.assets / 'bank' / entry['id'] / name, target / name)
        post = target / 'post.bin'
        post.write_bytes(identity + post.read_bytes()[len(anchor):])
        entry['files']['post.bin'] = dict(size=post.stat().st_size, sha256=digest(post))
    info.update(colour_stage='ife', colour_source=dict(
        bank_sha256=digest(args.assets / 'bank/bank.json'), tuning_sha256=digest(args.tuning),
        input_ife_sha256=digest(args.ife), ife_sha256=digest(out / 'ife.yaml'),
        builder_sha256=digest(Path(__file__)), selection=selection,
        note='Fixed source CCM moved before IFE gamma; late CCM unity; CV/tone/ANR/TF/ASF retained; RAW CCM model required.'))
    (out / 'bank/bank.json').write_text(json.dumps(info, indent=2) + '\n')
    adaptive.Bank(out / 'bank')
    print(json.dumps(dict(output=str(out), entries=len(info['entries']), ife_sha256=digest(out / 'ife.yaml'))))


if __name__ == '__main__':
    main()
