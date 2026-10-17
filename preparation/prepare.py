#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Prepare local Surface IMX681 camera assets from two explicitly supplied driver files."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import DLL_SHA256, TUNING_SHA256, REFERENCE_GAINS, ife_profile, load, prepare, require_identity, sha

FILES = ('cfg.bin', 'seed-cfg.bin', 'seed-iq.bin', 'seed-striping.bin',
         'iq-ltm.bin', 'striping.bin', 'pre-ltm.bin', 'post.bin', 'ltm-luts.bin',
         'gamma.bin', 'asf-luts.bin', 'dsx.bin', 'dsx-luts.bin',
         'nps-anr-p0.bin', 'nps-anr-p1.bin', 'nps-anr-p2.bin',
         'nps-tf-p0.bin', 'nps-tf-p1.bin')
BUILD_FLAGS = ['-O1', '-g', '-ffixed-x18']


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def validate_request(dll, tuning, output, jobs):
    if not 1 <= jobs <= 8:
        raise ValueError('--jobs must be between 1 and 8')
    if output.exists() or output.is_symlink():
        raise ValueError('Output must be a new directory; existing assets are never overwritten')
    if output.is_relative_to(HERE.parent):
        raise ValueError('Keep generated camera assets outside the source repository')
    require_identity(dll, DLL_SHA256, 'camera DLL')
    require_identity(tuning, TUNING_SHA256, 'IMX681 tuning')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll', type=Path, required=True, help='Owner-supplied QcDeviceMFT8380.dll')
    parser.add_argument('--tuning', type=Path, required=True, help='Owner-supplied com.surface.tuned.ffc_imx681.bin')
    parser.add_argument('--output', type=Path, required=True, help='New asset directory outside this repository')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--check-inputs', action='store_true', help='Check identities/paths without compiling or executing the DLL')
    args = parser.parse_args()
    dll, tuning = args.dll.resolve(strict=True), args.tuning.resolve(strict=True)
    # Preserve the final path component so dangling symlinks are also rejected.
    output = args.output.absolute()
    output = output.parent.resolve() / output.name
    validate_request(dll, tuning, output, args.jobs)
    if args.check_inputs:
        print('Supported DLL and IMX681 tuning hashes; output path is available.')
        return
    if platform.machine() != 'aarch64':
        raise ValueError('Generation requires native aarch64 Linux; input checking works on other hosts')
    if os.geteuid() == 0:
        raise ValueError('Run preparation as your normal user; it needs no hardware or system writes')
    compiler = shutil.which('clang')
    if compiler is None:
        raise ValueError('Install clang, openssl development headers, python-numpy and python-pyyaml')
    import yaml
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='.qualcomm-isp-prepare-', dir=output.parent) as temporary:
        work = Path(temporary)
        assets, bank = work / 'assets', work / 'assets/bank'
        bank.mkdir(parents=True)
        # Freeze and recheck owner inputs before using fixed binary offsets.
        copied_dll, copied_tuning = work / 'QcDeviceMFT8380.dll', work / 'tuning.bin'
        shutil.copyfile(dll, copied_dll)
        shutil.copyfile(tuning, copied_tuning)
        require_identity(copied_dll, DLL_SHA256, 'copied DLL')
        require_identity(copied_tuning, TUNING_SHA256, 'copied tuning')
        derived = prepare(copied_tuning, work / 'inputs')
        (assets / 'ife.yaml').write_text(yaml.safe_dump(ife_profile(copied_tuning), sort_keys=False))
        oracle = work / 'slrun'
        command = [compiler, *BUILD_FLAGS, '-o', str(oracle), str(HERE / 'slrun.c'), '-lm', '-lcrypto']
        subprocess.run(command, check=True, timeout=120)
        env = {k: v for k, v in os.environ.items() if not k.startswith('SLRUN_')}
        env['SLRUN_QUIET'] = '1'
        # This sets the environment for AWB's in-process wrapper too.
        for key in list(os.environ):
            if key.startswith('SLRUN_'):
                del os.environ[key]
        awb = load('awb-reference')
        reference, _, _ = awb.extract(copied_tuning)
        ratios = [[r['rg'], r['bg']] for r in reference['references']]
        reference = awb.query(copied_tuning, ratios, work / 'awb', oracle, copied_dll)
        write_json(assets / 'awb-reference.json', reference)
        gains, tones = [2 ** (i / 4) for i in range(25)], [2 ** (i / 8) for i in range(25)]

        def build(gi, ti):
            name = f'g{gi:02d}-t{ti:02d}'
            entry = work / name
            command = [sys.executable, '-I', str(HERE / 'build-entry.py'),
                       '--inputs', str(work / 'inputs'), '--oracle', str(oracle),
                       '--dll', str(copied_dll), '--tuning', str(copied_tuning),
                       '--ife', str(assets / 'ife.yaml'), '--out', str(entry),
                       '--gain', str(gains[gi]), '--drc', str(tones[ti])]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
            if result.returncode:
                errors = [f.read_text(errors='replace')[-1200:] for f in sorted(entry.glob('*.log'))]
                raise RuntimeError(f'{name} failed: {result.stderr[-2000:]}\n' + '\n'.join(errors[-4:]))
            destination = bank / name
            destination.mkdir()
            for filename in FILES:
                shutil.copyfile(entry / filename, destination / filename)
            for filename in ('experiment.json', 'gamma-contract.json'):
                record = json.loads((entry / filename).read_text())
                # Temporary input paths disappear; record the stable asset name.
                if filename == 'gamma-contract.json':
                    record['profile'] = 'ife.yaml'
                else:
                    record['gamma_contract']['profile'] = 'ife.yaml'
                write_json(destination / filename, record)
            details = {f: {'size': (destination / f).stat().st_size, 'sha256': sha(destination / f)} for f in FILES}
            shutil.rmtree(entry)
            return {'id': name, 'gain_index': gi, 'tone_index': ti, 'gain': gains[gi], 'drc': tones[ti], 'files': details}

        entries = []
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(build, gi, ti) for gi in range(25) for ti in range(25)]
            try:
                for future in as_completed(futures):
                    entries.append(future.result())
                    if len(entries) % 25 == 0:
                        print(f'Prepared {len(entries)}/625 entries', flush=True)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        entries.sort(key=lambda entry: (entry['gain_index'], entry['tone_index']))
        bank_manifest = dict(version=1, width=3840, height=2176, gains=gains, tones=tones,
                             gain_step_ev=.25, tone_step_ev=.125, entries=entries,
                             cct=5262.96630859375, index=200, sharpness=.75,
                             anr_lighter_from=12, anr_lighter_ev=1.0,
                             wb_reference=REFERENCE_GAINS, wb_log_envelope=.051,
                             runtime_dll=False, source='Explicit offline tuning grid; not captured-frame metadata',
                             note='Lab bounded display-luma tone controller; not Windows AE/DRC runtime reconstruction',
                             oracle_sha256=sha(oracle), dll_sha256=DLL_SHA256,
                             builder_sha256=sha(HERE / 'build-entry.py'))
        write_json(bank / 'bank.json', bank_manifest)
        spec = importlib.util.spec_from_file_location('adaptive', HERE.parent / 'runtime/ipe-adaptive.py')
        adaptive = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adaptive)
        adaptive.Bank(bank)  # Runtime's own manifest/geometry/hash checks; no camera access.
        sources = {str(f.relative_to(HERE.parent)): sha(f) for f in HERE.iterdir() if f.suffix in ('.py', '.c')}
        for filename in ('pd10.py', 'ipe-adaptive.py'):
            sources['runtime/' + filename] = sha(HERE.parent / 'runtime' / filename)
        write_json(assets / 'preparation.json', {
            'schema': 1, 'profile': 'surface-pro-11-denali-oled',
            'inputs': {'QcDeviceMFT8380.dll': DLL_SHA256, 'com.surface.tuned.ffc_imx681.bin': TUNING_SHA256},
            'sources': sources, 'derived_input_hashes': derived,
            'helper_sha256': sha(oracle), 'compiler_flags': BUILD_FLAGS + ['-lm', '-lcrypto'],
            'compiler': subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
            'python': platform.python_version(), 'numpy': adaptive.np.__version__, 'pyyaml': yaml.__version__,
            'entries': len(entries), 'preparation_seconds': time.monotonic() - started,
            'validation': 'runtime Bank manifest, geometry and checksum checks passed; no hardware test',
        })
        manifest = {str(f.relative_to(assets)): sha(f) for f in sorted(assets.rglob('*')) if f.is_file()}
        write_json(assets / 'local-data-manifest.json', manifest)
        # Refuse replacement even if another process created the destination meanwhile.
        output.mkdir(exist_ok=False)
        try:
            for source in sorted(assets.iterdir(), key=lambda path: path.name == 'local-data-manifest.json'):
                source.rename(output / source.name)
        except BaseException:
            # A directory without the completion marker is never a finished result.
            (output / 'local-data-manifest.json').unlink(missing_ok=True)
            raise
    print(f'Prepared {len(entries)} entries in {time.monotonic() - started:.1f}s: {output}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
