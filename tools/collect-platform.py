#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Read-only X1E bring-up inventory; prints JSON without opening a camera."""
import json
from pathlib import Path
import platform


def text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return None


def strings(path):
    try:
        return [s.decode('utf-8', errors='replace') for s in path.read_bytes().split(b'\0') if s]
    except OSError:
        return []


dt = Path('/sys/firmware/devicetree/base')
nodes = []
if dt.exists():
    for p in sorted(dt.rglob('compatible')):
        compatible = strings(p)
        if not any(any(word in c for word in ('camss', 'imx', 'ovti,', 'vd55', 'qcom,cci', 'csi', 'icp', 'ipe', 'bps'))
                   for c in compatible):
            continue
        nodes.append({'path': '/' + str(p.parent.relative_to(dt)),
                      'compatible': compatible,
                      'status': strings(p.parent / 'status') or ['okay (implicit)'],
                      'firmware': strings(p.parent / 'firmware-name')})

devices = {}
# Media devices sit on the media bus; there is no /sys/class/media.
for group, sysfs in (('media', '/sys/bus/media/devices'), ('video4linux', '/sys/class/video4linux')):
    devices[group] = []
    for p in sorted(Path(sysfs).glob('*')):
        # A media device has no device link; its parent is the device.
        driver = p / 'device/driver'
        if not driver.exists():
            driver = p.resolve().parent / 'driver'
        devices[group].append({'device': '/dev/' + p.name,
                               'name': text(p / 'name') or text(p / 'model'),
                               'driver': driver.resolve().name if driver.exists() else None})

modules = {}
for name in ('qcom_camss', 'qcom_camss_icp', 'phy_qcom_mipi_csi2', 'imx681', 'vd55g0'):
    p = Path('/sys/module') / name
    modules[name] = {'loaded': p.exists(), 'srcversion': text(p / 'srcversion')}

cmdline = text(Path('/proc/cmdline')) or ''
print(json.dumps({
    'kernel': platform.release(), 'architecture': platform.machine(),
    'model': strings(dt / 'model'), 'compatible': strings(dt / 'compatible'),
    'iommu_arguments': [s for s in cmdline.split() if s.startswith(('iommu.', 'arm-smmu.'))],
    'camera_dt_nodes': nodes, 'devices': devices, 'modules': modules,
    'note': 'Inventory only; node presence and enabled status do not establish camera support.',
}, indent=2))
