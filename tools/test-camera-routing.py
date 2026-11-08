#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Desktop integration check: restart publishers/WirePlumber and check fallback.

Close camera and audio applications first. Does not capture frames or enable
services. Restores the initial publisher running state even on failure.
"""
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
PROFILES = [json.loads((ROOT / 'profiles' / name).read_text()) for name in (
    'surface-pro-11-denali-oled.json', 'surface-pro-11-denali-oled-rear.json')]
UNITS = ['qualcomm-isp-publisher.service', 'qualcomm-isp-rear-publisher.service']


def systemctl(*args):
    subprocess.run(['systemctl', '--user', *args], check=True, timeout=20)


def names():
    objects = json.loads(subprocess.check_output(['pw-dump'], timeout=5))
    return [o.get('info', {}).get('props', {}).get('node.name') for o in objects
            if o.get('info', {}).get('props', {}).get('media.class') == 'Video/Source']


def check(label, active):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        actual = names()
        if all(actual.count(p['node_name']) == int(on) and
               actual.count(p['raw_node_name']) == int(not on)
               for p, on in zip(PROFILES, active)):
            print('PASS', label, actual, flush=True)
            return
        time.sleep(.2)
    raise AssertionError((label, actual))


def main():
    initial = [subprocess.run(['systemctl', '--user', 'is-active', '--quiet', u]).returncode == 0
               for u in UNITS]
    try:
        systemctl('start', *UNITS)
        check('both ISP routes', [True, True])
        systemctl('restart', 'wireplumber.service')
        check('ISP before monitor enumeration', [True, True])
        systemctl('stop', UNITS[1])
        check('rear fallback only', [True, False])
        systemctl('stop', UNITS[0])
        check('both fallbacks', [False, False])
        systemctl('restart', 'wireplumber.service')
        check('no ISP at monitor enumeration', [False, False])
        systemctl('start', UNITS[1])
        check('rear replaces existing fallback', [False, True])
        systemctl('start', UNITS[0])
        check('front replaces existing fallback', [True, True])
        for i in range(3):
            systemctl('restart', *UNITS)
            check(f'publisher restart {i + 1}', [True, True])
    finally:
        for unit, running in zip(UNITS, initial):
            systemctl('start' if running else 'stop', unit)


if __name__ == '__main__':
    main()
