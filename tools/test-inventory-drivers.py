#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Synthetic driver-export fixtures; no owner data, Windows execution or hardware."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).with_name('inventory-drivers.py')
spec = importlib.util.spec_from_file_location('inventory_drivers', TOOL)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

INF = '''[Version]
Class = Camera
Provider = %Maker%
DriverVer = 04/25/2025,1.2.3.4
CatalogFile.NTarm64 = sample.cat
[Strings]
Maker = "Example; Cameras" ; preserve quoted semicolon
[SourceDisksFiles.arm64]
opaque.bin = 1, payload
'''


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'private-owner-name'
        self.root.mkdir()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, '-I', str(TOOL), str(self.root), *map(str, args)],
                              capture_output=True, text=True, timeout=10)

    def test_utf16_metadata_and_case_insensitive_subdirectory_reference(self):
        self.write('pkg/oem1.inf', INF.encode('utf-16'))
        self.write('pkg/PAYLOAD/Opaque.BIN', b'payload without a camera-related filename')
        self.write('audio/audio.inf', '[Version]\nClass=MEDIA\nProvider=Sound Company\n')
        self.write('audio/audio.dll', b'unrelated audio payload')
        report = scanner.inventory(self.root)
        self.assertTrue(report['scan_complete'])
        self.assertEqual(report['summary']['camera_inf_candidates'], 1)
        inf = report['camera_infs'][0]
        self.assertEqual(inf['provider'], 'Example; Cameras')
        self.assertEqual(inf['driverver'], '04/25/2025,1.2.3.4')
        self.assertEqual(inf['catalog_files'], ['sample.cat'])
        self.assertEqual(inf['source_files_found'], ['pkg/PAYLOAD/Opaque.BIN'])
        records = {item['path']: item for item in report['files']}
        self.assertNotIn('audio/audio.dll', records)
        self.assertEqual(records['pkg/PAYLOAD/Opaque.BIN']['sha256'],
                         hashlib.sha256(b'payload without a camera-related filename').hexdigest())

    def test_ansi_tokens_continuations_and_extension_inf(self):
        text = '''[Version]
Class=Extension
Provider=%Maker%
DriverVer=01/02/2026,\\
1.2.0.0
[Strings.0409]
Maker="Caméra Vendor"
Description="Camera sensor extension"
File="com.vendor.tuned.front_imx999.bin"
[SourceDisksFiles]
%File%=1
'''
        self.write('extension/oem2.inf', text.encode('cp1252'))
        self.write('extension/com.vendor.tuned.front_imx999.bin', b'fixture')
        report = scanner.inventory(self.root)
        self.assertEqual(report['camera_infs'][0]['provider'], 'Caméra Vendor')
        self.assertEqual(report['camera_infs'][0]['driverver'], '01/02/2026,1.2.0.0')
        tuned = next(f for f in report['files'] if f['kind'] == 'tuning-candidate')
        self.assertEqual(tuned['sensor_name_hints'], ['imx999'])

    def test_standalone_candidates_and_duplicate_variants(self):
        for directory, data in [('a', b'one'), ('b', b'one'), ('c', b'two')]:
            self.write(directory + '/CAMERA_ICP.mbn', data)
        self.write('QcDeviceMFT9999.dll', b'MZ not an executable, never run')
        report = scanner.inventory(self.root)
        self.assertEqual(report['summary']['candidate_files'], 4)
        group = report['duplicate_filenames'][0]
        self.assertFalse(group['identical'])
        self.assertEqual(len(group['variants']), 2)
        self.assertEqual(report['summary']['conflicting_filenames'], 1)

    def test_identical_duplicates_are_not_conflicts(self):
        self.write('a/CAMERA_ICP.mbn', b'same')
        self.write('b/camera_icp.MBN', b'same')
        report = scanner.inventory(self.root)
        self.assertTrue(report['duplicate_filenames'][0]['identical'])
        self.assertEqual(report['summary']['conflicting_filenames'], 0)

    def test_split_firmware_and_unreadable_inf_produce_useful_partial_report(self):
        self.write('icp.mdt', b'firmware metadata fixture')
        self.write('icp.b00', b'firmware segment fixture')
        blocked = self.write('oem99.inf', INF)
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o600)
        if os.geteuid() == 0:
            self.skipTest('permission check requires a non-root test user')
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['files']), 2)
        self.assertTrue(all(f['kind'] == 'firmware-candidate' for f in report['files']))
        self.assertEqual(report['warnings'][0]['path'], 'oem99.inf')

    def test_symlinks_and_fifo_are_skipped_without_reading(self):
        outside = self.base / 'CAMERA_ICP.mbn'
        outside.write_bytes(b'private outside input')
        (self.root / 'CAMERA_ICP.mbn').symlink_to(outside)
        (self.root / 'loop').symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / 'QcDeviceMFT.dll')
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['files'], [])
        self.assertEqual(len(report['warnings']), 3)

    def test_unsafe_and_missing_source_entries_are_not_followed(self):
        self.write('package/camera.inf', INF + '\n../outside.dll=1\nC:\\outside.dll=1\n')
        report = scanner.inventory(self.root)
        self.assertFalse(report['scan_complete'])
        self.assertEqual(report['camera_infs'][0]['source_entries_not_found'], ['payload/opaque.bin'])
        self.assertEqual(len(report['files']), 1)
        self.assertIn('2 absolute or parent-traversing', report['warnings'][0]['reason'])

    def test_report_contains_metadata_not_root_or_payload_and_is_deterministic(self):
        payload = b'UNIQUE_PRIVATE_BINARY_CONTENT'
        self.write('CAMERA_ICP.mbn', payload)
        self.write('camera.inf', INF + '\n; PRIVATE_COMMENT_MUST_NOT_BE_EXPORTED\n')
        a, b = self.cli(), self.cli()
        self.assertEqual(a.returncode, 0)
        self.assertEqual(a.stdout, b.stdout)
        for private in (str(self.root), 'private-owner-name', payload.decode(), 'PRIVATE_COMMENT_MUST_NOT_BE_EXPORTED'):
            self.assertNotIn(private, a.stdout + a.stderr)

    def test_output_creation_never_overwrites_and_stays_outside_input(self):
        self.write('CAMERA_ICP.mbn', b'fixture')
        output = self.base / 'inventory.json'
        self.assertEqual(self.cli('--output', output).returncode, 0)
        original = output.read_bytes()
        self.assertEqual(self.cli('--output', output).returncode, 2)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self.cli('--output', self.root / 'report.json').returncode, 2)
        dangling = self.base / 'dangling.json'
        dangling.symlink_to(self.base / 'missing')
        self.assertEqual(self.cli('--output', dangling).returncode, 2)
        self.assertFalse((self.base / 'missing').exists())

    def test_invalid_inf_is_reported_instead_of_silent_success(self):
        self.write('camera.inf', b'\xff\xfe\xff')
        report = scanner.inventory(self.root)
        self.assertFalse(report['scan_complete'])
        self.assertEqual(len(report['warnings']), 1)

    def test_oversized_inf_is_reported(self):
        self.write('camera.inf', b' ' * (scanner.MAX_INF_BYTES + 1))
        report = scanner.inventory(self.root)
        self.assertFalse(report['scan_complete'])
        self.assertIn('4 MiB', report['warnings'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
