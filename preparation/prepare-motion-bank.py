#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Disable rear temporal filtering, retaining spatial denoising and color.

Regenerates striping from the qualified P010 early-color owner bank. No camera
or installation access; generated proprietary assets must remain local.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import DLL_SHA256, load, require_identity
from sensor_layout import LAYOUTS

adaptive = load('../runtime/ipe-adaptive')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'dll', 'oracle', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    out = a.output.absolute()
    out = out.parent.resolve() / out.name
    if os.geteuid() == 0 or out.exists() or out.is_symlink() or out.is_relative_to(HERE.parent):
        raise ValueError('Run unprivileged with a new output outside the repository')
    require_identity(a.dll, DLL_SHA256, 'camera DLL')
    source = adaptive.Bank(a.assets / 'bank')
    if (source.info.get('precision') != 'p010-internal' or
            source.info.get('colour_stage') != 'ife' or
            source.info.get('dll_sha256') != DLL_SHA256):
        raise ValueError('Expected qualified P010 early-color bank')
    awb = json.loads((a.assets / 'awb-reference.json').read_text())
    if awb.get('source_sha256') != LAYOUTS['ov13858']['sha256']:
        raise ValueError('Expected rear sensor assets')
    out.mkdir(parents=True)
    (out / 'bank').mkdir()
    for name in ('ife.yaml', 'awb-reference.json'):
        shutil.copyfile(a.assets / name, out / name)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
    env['SLRUN_QUIET'] = '1'

    def prepare(old):
        entry = json.loads(json.dumps(old))
        d = out / 'bank' / entry['id']
        d.mkdir()
        for name in entry['files']:
            shutil.copyfile(a.assets / 'bank' / entry['id'] / name, d / name)
        iq = bytearray((d / 'iq-ltm.bin').read_bytes())
        if len(iq) != 6196:
            raise ValueError('Unexpected IQ layout')
        flags = [struct.unpack_from('<I', iq, 0x578 + i * 0x198)[0] for i in range(4)]
        if flags != [1, 1, 0, 0]:
            raise ValueError(f'Unexpected source TF enables: {flags}')
        for i in range(4):
            struct.pack_into('<I', iq, 0x578 + i * 0x198, 0)
        (d / 'iq-ltm.bin').write_bytes(iq)
        result = subprocess.run([str(a.oracle.resolve()), str(a.dll.resolve()),
            'run', str(d / 'cfg.bin'), str(d / 'iq-ltm.bin'), '0x60800', '0',
            str(d / 'striping.bin'), '1', '3840', '2176'], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            check=True, timeout=30)
        (d / 'striping.log').write_text(result.stdout)
        scratch = re.search(r'scratchBufferSize=(\d+)', result.stdout)
        if 'Execute rc=0 ' not in result.stdout or not scratch or int(scratch[1]) > 6 << 20:
            raise ValueError('Striping calculation or scratch limit failed')
        for name in ('iq-ltm.bin', 'striping.bin'):
            entry['files'][name] = dict(size=(d / name).stat().st_size, sha256=sha(d / name))
        return entry

    with ThreadPoolExecutor(2) as pool:
        entries = list(pool.map(prepare, source.info['entries']))
    info = json.loads(json.dumps(source.info))
    info.update(entries=entries, temporal_filter='disabled', motion_source=dict(
        bank_sha256=sha(a.assets / 'bank/bank.json'), dll_sha256=sha(a.dll),
        oracle_sha256=sha(a.oracle), builder_sha256=sha(Path(__file__)),
        note='TF enables zero; striping regenerated. Seed, spatial ANR, color, tone and sharpening unchanged.'))
    (out / 'bank/bank.json').write_text(json.dumps(info, indent=2) + '\n')
    adaptive.Bank(out / 'bank')
    print(f'Verified {len(entries)} motion bank entries: {out}')


if __name__ == '__main__':
    main()
