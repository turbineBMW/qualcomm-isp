# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Explicit machine configuration; no hardware is opened at import time."""
import json
import os
from pathlib import Path
import pwd
import subprocess


def load():
    path = Path(os.environ.get('QUALCOMM_ISP_CONFIG', '/etc/qualcomm-isp/config.json'))
    config = json.loads(path.read_text())
    profile = json.loads(Path(config['profile']).read_text())
    if profile['schema'] != 1 or (profile['width'], profile['height'], profile['padded_height']) != (3840, 2160, 2176):
        raise ValueError('Only the qualified 3840x2160/2176 pipeline is implemented')
    for key in ('profile', 'assets', 'cam', 'cam_library_path', 'bridge'):
        if not Path(config[key]).is_absolute():
            raise ValueError(f'{key} must be an absolute path')
    return config, profile


def identity():
    # Set by the root worker from SO_PEERCRED, never from client JSON.
    uid = int(os.environ.get('QUALCOMM_ISP_CAPTURE_UID', str(os.getuid())))
    if uid == 0:
        raise ValueError('Select a non-root capture user with QUALCOMM_ISP_CAPTURE_UID')
    return pwd.getpwuid(uid)


def sensor(profile):
    """Resolve media and sensor numbering on this boot instead of assuming media0."""
    matches = []
    for media in sorted(Path('/dev').glob('media[0-9]*')):
        result = subprocess.run(['media-ctl', '-d', str(media), '-p'], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if '- entity ' in line and ': ' in line:
                name = line.split(': ', 1)[1].split(' (', 1)[0]
                if name.startswith(profile['sensor_entity_prefix']):
                    device = subprocess.check_output(['media-ctl', '-d', str(media), '-e', name], text=True).strip()
                    matches.append((str(media), device))
    if len(matches) != 1:
        raise ValueError(f'Expected one matching sensor, found {len(matches)}')
    return matches[0]


def preflight():
    config, profile = load()
    compatible = Path('/sys/firmware/devicetree/base/compatible').read_bytes().split(b'\0')
    if profile['compatible'].encode() not in compatible:
        raise ValueError('Board does not match the selected profile')
    if 'iommu.passthrough=0' not in Path('/proc/cmdline').read_text().split():
        raise ValueError('The qualified configuration requires iommu.passthrough=0')
    manifest = json.loads(Path(profile['kernel_manifest']).read_text())
    if manifest['kernel_release'] != os.uname().release:
        raise ValueError('Kernel release does not match the camera build manifest')
    for name in ('qcom_camss', 'qcom_camss_icp', 'imx681'):
        for field in ('srcversion', 'vermagic'):
            actual = subprocess.check_output(['modinfo', '-F', field, name], text=True).strip()
            if actual != manifest['modules'][name][field]:
                raise ValueError(f'{name}: {field} mismatch')
        if name != 'qcom_camss_icp':
            actual = Path(f'/sys/module/{name}/srcversion').read_text().strip()
            if actual != manifest['modules'][name]['srcversion']:
                raise ValueError(f'{name}: loaded module mismatch')
    for key in ('cam', 'bridge'):
        if not Path(config[key]).is_file():
            raise ValueError(f'Missing {key}: {config[key]}')
    assets = Path(config['assets'])
    for name in (profile['ife_tuning'], profile['awb_reference'], profile['program_bank'] + '/bank.json'):
        if not (assets / name).is_file():
            raise ValueError(f'Missing local board data: {assets / name}')
    firmware_roots = [Path('/usr/lib/firmware/updates') / os.uname().release,
                      Path('/usr/lib/firmware/updates'),
                      Path('/usr/lib/firmware') / os.uname().release,
                      Path('/usr/lib/firmware')]
    custom = Path('/sys/module/firmware_class/parameters/path').read_text().strip()
    if custom:
        firmware_roots.insert(0, Path(custom))
    if not any(Path(str(root / profile['firmware']) + suffix).is_file()
               for root in firmware_roots for suffix in ('', '.zst', '.xz')):
        raise ValueError(f"Missing firmware: {profile['firmware']}")
    media, device = sensor(profile)
    return dict(board=profile['id'], kernel=os.uname().release, media=media, sensor=device,
                cam=config['cam'], bridge=config['bridge'], assets=str(assets))


if __name__ == '__main__':
    print(json.dumps(preflight(), indent=2))
