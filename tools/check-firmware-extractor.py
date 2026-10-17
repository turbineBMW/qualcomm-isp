#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Check a supplied Omarchy extractor with synthetic camera data in a temp tree."""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('extractor', type=Path, help='Reviewed qcom-firmware-extract script')
    args = parser.parse_args()
    extractor = args.extractor.resolve(strict=True)
    name = 'qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn'
    payload = b'Synthetic camera fixture; not executable firmware.\n'
    with tempfile.TemporaryDirectory(prefix='qualcomm-isp-extractor-') as temporary:
        root = Path(temporary)
        dt, firmware, store, stage = [root / x for x in ('dt', 'firmware', 'store', 'stage')]
        node = dt / 'soc@0' / 'icp@ac01000'
        node.mkdir(parents=True)
        (node / 'firmware-name').write_bytes(name.encode() + b'\0')
        (dt / 'compatible').write_bytes(b'microsoft,denali-oled\0')
        for directory in (firmware, store / 'camera-driver', root / 'provided', root / 'system/run'):
            directory.mkdir(parents=True)
        image = store / 'camera-driver/CAMERA_ICP.mbn'
        image.write_bytes(payload)
        (store / 'camera-driver/ife.yaml').write_text('synthetic: tuning\n')
        (root / 'config.gz').write_bytes(gzip.compress(b'CONFIG_FW_LOADER=y\n'))
        env = os.environ.copy()
        env.update({
            'QCOM_FW_DT_ROOT': str(dt),
            'QCOM_FW_FIRMWARE_ROOT': str(firmware),
            'QCOM_FW_PROVIDED_DIR': str(root / 'provided'),
            'QCOM_FW_KERNEL_CONFIG': str(root / 'config.gz'),
            'QCOM_FW_ROOT': str(root / 'system'),
            # Upstream's fixture hook only; all destinations are under root.
            'QCOM_FW_TEST_EUID': '0',
        })

        def run(*arguments):
            return subprocess.run(['bash', str(extractor), *map(str, arguments)],
                                  env=env, check=True, capture_output=True,
                                  text=True, timeout=30)

        assert run('--list-missing').stdout.strip() == name
        run('--stage', stage, '-d', store)
        assert (stage / name).read_bytes() == payload
        assert not list(stage.rglob('*.yaml'))
        print('PASS: device-tree camera name stages at the exact firmware path')

        run('--install', '--no-rebuild', '-d', store)
        installed = firmware / 'updates' / name
        assert installed.read_bytes() == payload
        manifest = (root / 'system/var/lib/omarchy/qcom-firmware/manifest').read_text()
        assert name in manifest and hashlib.sha256(payload).hexdigest() in manifest
        assert run('--list-missing').stdout == ''
        print('PASS: camera installation and manifest match the requested bytes')

        # Existing firmware must not be replaced by a different driver-store file.
        image.write_bytes(b'Other synthetic version\n')
        run('--install', '--no-rebuild', '-d', store)
        assert installed.read_bytes() == payload
        print('PASS: an already installed camera image is preserved')

        # Multiple different camera images lack a companion for disambiguation.
        installed.unlink()
        (store / 'second-driver').mkdir()
        (store / 'second-driver/CAMERA_ICP.mbn').write_bytes(payload)
        result = run('--stage', root / 'ambiguous-stage', '-d', store)
        assert not (root / 'ambiguous-stage' / name).exists()
        assert 'ambiguous' in result.stderr
        print('PASS: differing camera variants are refused instead of guessed')


if __name__ == '__main__':
    main()
