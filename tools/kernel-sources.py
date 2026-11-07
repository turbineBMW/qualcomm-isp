#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Write packaging/kernel/ISP-SOURCES.json from the recipe in packaging/kernel.

Records the base recipe the ISP kernel is derived from, the files that define
the build (PKGBUILD, config, opt-in module options) and every ISP patch with
its hash, subject and author. Run it after rebasing the recipe; review the
diff of the JSON with the recipe change.
"""
import argparse
import email
import email.header
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
KERNEL = ROOT / 'packaging' / 'kernel'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header(message, name):
    value = message.get(name, '')
    return ''.join(part.decode(enc or 'utf-8') if isinstance(part, bytes) else part
                   for part, enc in email.header.decode_header(value)).replace('\n', ' ')


def isp_patches(first, last):
    entries = []
    for path in sorted(KERNEL.glob('*.patch')):
        if not path.name[:4].isdigit() or not first <= int(path.name[:4]) <= last:
            continue
        message = email.message_from_bytes(path.read_bytes())
        subject = re.sub(r'^\[PATCH[^\]]*\]\s*', '', re.sub(r'\s+', ' ', header(message, 'Subject'))).strip()
        author = re.sub(r'\s*<.*>$', '', header(message, 'From')).strip()
        entries.append(dict(file=path.name, sha256=sha256(path), subject=subject, author=author))
    return entries


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status', required=True)
    p.add_argument('--base-url', required=True, help='Repository of the base recipe')
    p.add_argument('--base-commit', required=True)
    p.add_argument('--base-path', required=True)
    p.add_argument('--base-version', required=True)
    p.add_argument('--base-describes', required=True, help='What the base recipe is, relative to Omarchy')
    p.add_argument('--isp-commit', required=True, help='Kernel commit the ISP patch bodies were exported from')
    p.add_argument('--isp-range', default='3300-3368')
    p.add_argument('--change', action='append', default=[], help='One change to the base recipe (repeatable)')
    a = p.parse_args()

    pkgbuild = (KERNEL / 'PKGBUILD').read_text()
    field = lambda name: re.search(rf'^{name}=(\S+)', pkgbuild, re.M).group(1).strip('\'"')
    pkgver, pkgrel, kernel = field('pkgver'), field('pkgrel'), field('_kernel')
    first, last = (int(x) for x in a.isp_range.split('-'))
    patches = isp_patches(first, last)
    if len(patches) != last - first + 1:
        raise SystemExit(f'expected {last - first + 1} ISP patches, found {len(patches)}')
    for entry in patches:
        if entry['file'] not in pkgbuild:
            raise SystemExit(f"{entry['file']} is not in the PKGBUILD's source array")
    data = dict(
        schema=2,
        status=a.status,
        base_recipe=dict(url=f'{a.base_url.rstrip("/")}/tree/{a.base_commit}/{a.base_path}',
                         commit=a.base_commit, path=a.base_path, version=a.base_version,
                         describes=a.base_describes),
        package=kernel,
        package_version=f'{pkgver}-{pkgrel}',
        kernel_release=f'{pkgver}.0-{pkgrel}-{kernel.removeprefix("linux-")}' if pkgver.count('.') == 1
        else f'{pkgver}-{pkgrel}-{kernel.removeprefix("linux-")}',
        isp_commit=a.isp_commit,
        recipe_changes=a.change,
        PKGBUILD_sha256=sha256(KERNEL / 'PKGBUILD'),
        config_sha256=sha256(KERNEL / 'config.aarch64'),
        opt_in_config_sha256=sha256(KERNEL / 'omarchy-isp-lab.conf'),
        groups=[dict(name='isp-port', range=f'{first}..{last}', patches=patches)],
    )
    (KERNEL / 'ISP-SOURCES.json').write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    print(f"{len(patches)} ISP patches, {data['package_version']}, {data['kernel_release']}")


if __name__ == '__main__':
    main()
