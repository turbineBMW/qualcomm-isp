#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Boundary checks use synthetic inputs; no Windows data or hardware required."""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inputs import DLL_SHA256, require_identity, load, anr_reserve
from prepare import validate_request
comparison = load('compare-assets')
import temporal_blend


class PreparationChecks(unittest.TestCase):
    def anr_descriptor(self):
        data = bytearray([0xa5] * 964)
        for index in range(4):
            base = 44 + 228 * index
            struct.pack_into('<3I', data, base, index, 1, 0)
            struct.pack_into('<2I', data, base + 220, 0, 1)
        return data

    def test_anr_reserve_retains_pass_selectors_and_last_dithering_flag(self):
        data = self.anr_descriptor()
        reserve = anr_reserve(data)
        self.assertEqual(len(reserve), 912)
        for index in range(4):
            self.assertEqual(struct.unpack_from('<3I', reserve, 228 * index), (index, 1, 0))
            self.assertEqual(struct.unpack_from('<2I', reserve, 228 * index + 220), (0, 1))
        self.assertEqual(reserve[-4:], struct.pack('<I', 1))

    def test_anr_reserve_rejects_shifted_records_and_invalid_flags(self):
        data = self.anr_descriptor()
        shifted = data[:44] + data[48:] + bytes(4)
        with self.assertRaisesRegex(ValueError, 'pass selector'):
            anr_reserve(shifted)
        for offset in (44 + 4, 44 + 228 + 224):
            invalid = bytearray(data)
            struct.pack_into('<I', invalid, offset, 2)
            with self.assertRaisesRegex(ValueError, 'flags'):
                anr_reserve(invalid)
        with self.assertRaisesRegex(ValueError, 'size'):
            anr_reserve(data[:-4])

    def test_wrong_driver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'camera.dll'
            source.write_bytes(b'Synthetic unsupported file')
            with self.assertRaisesRegex(ValueError, 'Unsupported camera DLL SHA-256'):
                require_identity(source, DLL_SHA256, 'camera DLL')

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'owner-file'
            marker.write_text('preserve me')
            with self.assertRaisesRegex(ValueError, 'never overwritten'):
                validate_request(root / 'dll', root / 'tuning', root, 2)
            self.assertEqual(marker.read_text(), 'preserve me')

    def test_dangling_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            output.symlink_to(Path(temporary) / 'missing')
            with self.assertRaisesRegex(ValueError, 'never overwritten'):
                validate_request(Path('dll'), Path('tuning'), output, 2)

    def test_generated_data_cannot_land_in_source(self):
        with self.assertRaisesRegex(ValueError, 'outside the source repository'):
            validate_request(Path('dll'), Path('tuning'), HERE / 'not-created', 2)

    def test_job_count_is_bounded(self):
        for jobs in (0, -1, 9):
            with self.assertRaisesRegex(ValueError, '--jobs'):
                validate_request(Path('dll'), Path('tuning'), Path('/unused'), jobs)

    def striping(self):
        data = bytearray(comparison.STRIPING_SIZE)
        for offset in comparison.POINTER_OFFSETS:
            struct.pack_into('<Q', data, offset, 0x123456780000)
        return data

    def test_only_the_documented_pointer_bytes_are_ignored(self):
        a, b = self.striping(), self.striping()
        for offset in comparison.POINTER_OFFSETS:
            struct.pack_into('<Q', b, offset, 0xabcdef000000)
        self.assertEqual(comparison.comparable_striping(a), comparison.comparable_striping(b))
        b[comparison.POINTER_OFFSETS[0] + 8] = 1
        self.assertNotEqual(comparison.comparable_striping(a), comparison.comparable_striping(b))

    def test_spatial_rows_ramp_in_from_eight_times_gain(self):
        rows = [temporal_blend.spatial_row(g, 1.0) for g in range(25)]
        self.assertEqual(rows[:13], list(range(13)))
        self.assertEqual(rows[13:17], [12, 12, 12, 12])
        self.assertEqual(rows[17:], list(range(13, 21)))
        self.assertEqual([temporal_blend.spatial_row(g, 0) for g in range(25)], list(range(25)))
        self.assertTrue(all(b >= a for a, b in zip(rows, rows[1:])))

    def test_temporal_filter_moves_to_the_full_pass_only(self):
        t = temporal_blend
        iq = bytearray(6196)
        for q, on in enumerate((1, 1, 0)):
            struct.pack_into('<I', iq, t.TF + q * t.PASS, on)
        for index in (19, 20):
            struct.pack_into('<I', iq, t.TF + index * 4, 1)
        program = struct.pack('<4I', 0x0300005e, 0x5868, 0x405c4706, 0x8040) + bytes(404)
        new_iq, new_program = t.full_pass_only(bytes(iq), program, 'entry')
        words = struct.unpack_from('<21I', new_iq, t.TF)
        self.assertEqual((words[0], words[3], words[19], words[20]), (1, 1, 0, 0))
        self.assertEqual(struct.unpack_from('<I', new_iq, t.TF + t.PASS)[0], 0)
        self.assertEqual(struct.unpack_from('<I', new_program, 8)[0], 0x40444702)
        self.assertEqual(new_program[12:], program[12:])
        self.assertEqual(new_iq[:t.TF], bytes(iq[:t.TF]))
        with self.assertRaises(ValueError):   # already converted
            t.full_pass_only(new_iq, new_program, 'entry')
        with self.assertRaises(ValueError):   # reference marked invalid
            t.full_pass_only(bytes(iq), struct.pack('<4I', 0x0300005e, 0x5868, 0x405c4707, 0x8040) + bytes(404), 'entry')

    def test_unknown_striping_layout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unqualified striping size'):
            comparison.comparable_striping(self.striping()[:-1])
        data = self.striping()
        struct.pack_into('<Q', data, comparison.POINTER_OFFSETS[0], 3)
        with self.assertRaisesRegex(ValueError, 'Unexpected process-pointer'):
            comparison.comparable_striping(data)


if __name__ == '__main__':
    unittest.main()
