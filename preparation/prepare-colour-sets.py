#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Prepare experimental CCT variants while preserving an accepted bank's ASF.

No DLL execution or hardware access. Only CC/CV register commands change;
gamma, tone, denoise, geometry and sharpening remain in the supplied bank.
Generated data must stay outside the source repository.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import load

colour = load('parity-colour')

CCTS = [2300, 2500, 2700, 2850, 3300, 3800, 4100, 4600, 5000, 5263, 5700, 6500, 7500]


def split_colour(program):
    """Require the qualified CC/CV prefix and retain every ASF command byte."""
    offset = 0
    for address, count in ((0x15768, 10), (0x11668, 9)):
        if len(program) < offset + 8:
            raise ValueError('Truncated colour command')
        header, actual = struct.unpack_from('<II', program, offset)
        if (header, actual) != ((3 << 24) | count, address):
            raise ValueError('Unexpected colour command prefix')
        offset += 8 + count * 4
    if len(program) <= offset:
        raise ValueError('Missing sharpening commands')
    return program[:offset], program[offset:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tuning', type=Path, required=True)
    parser.add_argument('--bank', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.is_relative_to(HERE.parent) or args.out.exists() or args.out.is_symlink():
        raise ValueError('Choose a new output directory outside the source repository')
    chrom = colour.Chromatix(args.tuning)
    sensor, layout = colour.identify(chrom.f)
    info = json.loads((args.bank / 'bank.json').read_text())
    if (info['version'], info['width'], info['height']) != (1, 3840, 2176):
        raise ValueError('Unsupported bank')
    if info['gains'] != [2 ** (i / 4) for i in range(25)]:
        raise ValueError('Unsupported gain grid')
    cc = colour.get_module(chrom, 'cc13', 2)
    cv = colour.get_module(chrom, 'cv12', 0)
    def commands(cct):
        matrix, cc_selection = colour.select_colour(chrom, cc, layout['cc_rows'], 12, cct, info['index'])
        values, cv_selection = colour.select_colour(chrom, cv, layout['cv_rows'], 14, cct, info['index'])
        program = colour.cc_matrix([matrix[i:i+3] for i in (0, 3, 6)], matrix[9:]) + colour.ce(values)
        return program, dict(cc=matrix, cv=values, cc_selection=cc_selection, cv_selection=cv_selection)
    anchor, _ = commands(info['cct'])
    tails = {}
    checked = 0
    for entry in info['entries']:
        path = args.bank / entry['id'] / 'post.bin'
        data = path.read_bytes()
        expected = entry['files']['post.bin']
        if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError(f'Bank post checksum mismatch: {path}')
        prefix, tail = split_colour(data)
        # This also prevents accidentally pairing front tuning with a rear bank.
        if prefix != anchor:
            raise ValueError('Bank colour anchor differs from selected sensor tuning')
        gi = entry['gain_index']
        if gi in tails and tails[gi] != tail:
            raise ValueError('Sharpening commands vary with tone; unsupported bank')
        tails[gi] = tail
        checked += 1
    if len(tails) != 25 or checked != 25 * len(info['tones']):
        raise ValueError('Incomplete bank')
    out.mkdir(parents=True, exist_ok=False)
    entries, selections = [], {}
    for cct in CCTS:
        prefix, selections[cct] = commands(cct)
        for gi, gain in enumerate(info['gains']):
            path = out / f'cct{cct}' / f'g{gi:02d}' / 'post.bin'
            path.parent.mkdir(parents=True)
            data = prefix + tails[gi]
            path.write_bytes(data)
            entries.append(dict(cct=cct, gain_index=gi, gain=gain, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
    manifest = dict(version=1, sensor=sensor, ccts=CCTS, gains=info['gains'], file='post.bin',
                    sharpness=info['sharpness'], lux_index=info['index'], entries=entries,
                    source_tuning_sha256=hashlib.sha256(chrom.f).hexdigest(),
                    bank_manifest_sha256=hashlib.sha256((args.bank / 'bank.json').read_bytes()).hexdigest(),
                    source_bank=str(args.bank.resolve()), colour_selections=selections,
                    note='Experimental CCT selection; every ASF suffix preserved from the source bank; no visual parity claim')
    (out / 'colour-sets.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(output=str(out), sensor=sensor, variants=len(entries), verified_source_post_files=checked)))


if __name__ == '__main__':
    main()
