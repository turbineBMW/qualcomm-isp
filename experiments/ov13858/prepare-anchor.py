#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Prepare one experimental OV13858 IPE anchor outside the source checkout."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
DLL = 'c241b7fbb2ec54e439752a1ea7ad25da10ca740012a54bd0e7a87ea94a141c35'
TUNING = '4858ccb297eeecbc8e9b6d673f7ab4b0ead559adf16e3fe717eea9e40ccef635'


def require(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f'Unsupported input SHA-256: {path.name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll', type=Path, required=True)
    parser.add_argument('--tuning', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0 or platform.machine() != 'aarch64':
        parser.error('Preparation requires a normal user on native aarch64 Linux')
    out = args.output.absolute()
    out = out.parent.resolve() / out.name
    if out.exists() or out.is_symlink() or out.is_relative_to(SOURCE):
        parser.error('Use a new output directory outside the source repository')
    dll, tuning = args.dll.resolve(strict=True), args.tuning.resolve(strict=True)
    require(dll, DLL)
    require(tuning, TUNING)
    import yaml
    out.mkdir(parents=True)
    prep = out / 'preparation'
    shutil.copytree(SOURCE / 'preparation', prep, ignore=shutil.ignore_patterns('__pycache__'))
    (out / 'runtime').symlink_to(SOURCE / 'runtime', target_is_directory=True)
    local_dll, local_tuning = out / 'QcDeviceMFT8380.dll', out / 'tuning.bin'
    shutil.copyfile(dll, local_dll)
    shutil.copyfile(tuning, local_tuning)
    require(local_dll, DLL)
    require(local_tuning, TUNING)
    sys.path.insert(0, str(prep))
    spec = importlib.util.spec_from_file_location('rear_inputs', prep / 'inputs.py')
    inputs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inputs)
    derived = inputs.prepare(local_tuning, out / 'inputs', sensor='ov13858')
    profile = inputs.ife_profile(local_tuning, sensor='ov13858')
    profile['ae']['raw_highlights'] = False
    profile['awb'] = dict(blue_bias=1, warmth=0, neutral_ratio=1)
    (out / 'ife.yaml').write_text(yaml.safe_dump(profile, sort_keys=False))
    awb, _, _ = inputs.load('awb-reference').extract(local_tuning)
    (out / 'awb-reference.json').write_text(json.dumps(awb, indent=2) + '\n')
    r, g, b = awb['initial_gains']
    manual = {'frames': [{0: dict(AeEnable=False, AwbEnable=False, ExposureTime=30000,
                                  AnalogueGain=2.0, ColourGains=[r / g, b / g])}]}
    (out / 'manual.yaml').write_text(yaml.safe_dump(manual, sort_keys=False))
    subprocess.run(['clang', '-O1', '-g', '-ffixed-x18', '-o', str(out / 'slrun'),
                    str(prep / 'slrun.c'), '-lm', '-lcrypto'], check=True, timeout=120)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
    env['SLRUN_QUIET'] = '1'
    subprocess.run([sys.executable, '-I', str(prep / 'build-entry.py'), '--inputs', str(out / 'inputs'),
                    '--oracle', str(out / 'slrun'), '--dll', str(local_dll), '--tuning', str(local_tuning),
                    '--ife', str(out / 'ife.yaml'), '--out', str(out / 'anchor'), '--gain', '2', '--drc', '1.5'],
                   env=env, check=True, timeout=120)
    (out / 'preparation.json').write_text(json.dumps(dict(experimental=True, sensor='ov13858',
        acpi_id='MSHW0491', dll_sha256=DLL, tuning_sha256=TUNING, gain=2, drc=1.5,
        exposure_us=30000, fixed_white_balance=True, inputs=derived), indent=2) + '\n')
    print(f'Prepared one fixed-control rear anchor in {out}')


if __name__ == '__main__':
    main()
