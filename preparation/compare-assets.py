#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Compare prepared data with an accepted local bank; never opens a camera."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from prepare import FILES

# The accepted striping serializer retains four process pointers. These are
# the only ignored bytes, at the measured size for this profile/firmware.
POINTER_OFFSETS = (0x144d0, 0x14968, 0x14e10, 0x152b8)
STRIPING_SIZE = 155752


def comparable_striping(data):
    if len(data) != STRIPING_SIZE:
        raise ValueError('Unqualified striping size; cannot apply the pointer comparison')
    result = bytearray(data)
    for offset in POINTER_OFFSETS:
        pointer, = struct.unpack_from('<Q', result, offset)
        if not 0x100000000 <= pointer < (1 << 48) or pointer % 8:
            raise ValueError('Unexpected process-pointer field in striping data')
        result[offset:offset + 8] = bytes(8)
    return bytes(result)


def compare(reference, candidate):
    spec = importlib.util.spec_from_file_location('adaptive', HERE.parent / 'runtime/ipe-adaptive.py')
    adaptive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adaptive)
    original, generated = adaptive.Bank(reference / 'bank'), adaptive.Bank(candidate / 'bank')
    if original.entries.keys() != generated.entries.keys():
        raise ValueError('Bank grids differ')
    for key in ('version', 'width', 'height', 'gains', 'tones', 'cct', 'index', 'sharpness',
                'anr_lighter_from', 'anr_lighter_ev', 'wb_reference', 'wb_log_envelope', 'runtime_dll', 'dll_sha256'):
        if original.info[key] != generated.info[key]:
            raise ValueError(f'Bank metadata differs: {key}')
    exact, pointer_only, canonical = 0, 0, hashlib.sha256()
    for coordinates in sorted(original.entries):
        name = original.entries[coordinates]['id']
        for filename in FILES:
            a = (reference / 'bank' / name / filename).read_bytes()
            b = (candidate / 'bank' / name / filename).read_bytes()
            if filename in ('striping.bin', 'seed-striping.bin'):
                normalized_a, normalized_b = comparable_striping(a), comparable_striping(b)
                if normalized_a != normalized_b:
                    raise ValueError(f'Striping differs outside process pointers: {name}/{filename}')
                pointer_only += a != b
                payload = normalized_b
            else:
                if a != b:
                    raise ValueError(f'Hardware input differs: {name}/{filename}')
                payload = b
            exact += a == b
            canonical.update((name + '/' + filename + '\0').encode())
            canonical.update(hashlib.sha256(payload).digest())
    a = json.loads((reference / 'awb-reference.json').read_text())
    b = json.loads((candidate / 'awb-reference.json').read_text())
    a.pop('oracle_sha256', None)
    b.pop('oracle_sha256', None)
    if a != b:
        raise ValueError('AWB reference data differs beyond the rebuilt helper hash')
    a = yaml.safe_load((reference / 'ife.yaml').read_text())
    b = yaml.safe_load((candidate / 'ife.yaml').read_text())
    inactive_ccm_differs = a.get('ccm') != b.get('ccm')
    if a.get('ccm_strength') == b.get('ccm_strength') == 0:
        a.pop('ccm', None)
        b.pop('ccm', None)
    if a != b:
        raise ValueError('IFE settings differ beyond a disabled CCM matrix')
    return {'entries': len(original.entries), 'hardware_files': len(original.entries) * len(FILES),
            'byte_identical_files': exact, 'striping_files_with_only_process_pointer_changes': pointer_only,
            'ignored_striping_offsets': [hex(offset) for offset in POINTER_OFFSETS],
            'comparison_sha256': canonical.hexdigest(), 'awb_matches_except_helper_hash': True,
            'ife_matches_except_disabled_ccm': True, 'disabled_ccm_matrix_differs': inactive_ccm_differs,
            'hardware_test': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.reference.resolve(), args.candidate.resolve()), indent=2))


if __name__ == '__main__':
    main()
