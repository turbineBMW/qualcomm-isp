#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Branch-specific interpolation regressions, without Windows data or hardware."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inputs import load

colour = load('parity-colour')


class Fixture:
    def __init__(self):
        self.blob = bytearray()
        self.parts = []
        self.part('control_var_type', struct.pack('<5I', 2, 5, 100, 0, 6))
        self.empty = self.part('region', b'')
        self.empty_trigger = self.part('trigger', b'')
        rows = []
        for row, second_start in enumerate((2600, 2800)):
            entries = []
            for i, interval in enumerate(((1, 2300), (second_start, 3400), (3600, 4300), (4900, 9000))):
                region = self.part('region', struct.pack('<f', row * 100 + i * 10))
                entries.append(struct.pack('<ffIIII', *interval, 0, self.empty_trigger[3] - 1, 1, region[3] - 1))
            rows.append(self.part('trigger', b''.join(entries)))
        lux = self.part('trigger', b''.join(self.node(interval, 4, child)
                         for interval, child in zip(((1, 350), (400, 900)), rows)))
        child = self.part('trigger', self.node((0, 0), 2, lux))
        child = self.part('trigger', self.node((1, 16), 1, child))
        self.part('mod_cc13_trigger_data', self.node((1, 100), 1, child))

    def node(self, interval, count, child):
        return struct.pack('<ffIIII', *interval, count, child[3] - 1, 0, self.empty[3] - 1)

    def part(self, name, data):
        result = (name, len(self.blob), len(data), len(self.parts) + 1)
        self.blob.extend(data)
        self.parts.append(result)
        return result

    def data(self, offset, size):
        return self.blob[offset:offset + size]


class ColourSelectionTests(unittest.TestCase):
    def select(self, cct, lux):
        fixture = Fixture()
        # Directory order is intentionally unrelated to the tree's traversal.
        return colour.select_colour(fixture, list(reversed(fixture.parts)), 2, 1, cct, lux)

    def test_each_lux_row_uses_its_own_temperature_interval(self):
        self.assertEqual(self.select(2600, 200)[0], [10])
        self.assertAlmostEqual(self.select(2600, 500)[0][0], 106)

    def test_nested_lux_blend_preserves_different_temperature_weights(self):
        values, selection = self.select(2600, 375)
        self.assertAlmostEqual(values[0], 58)
        self.assertAlmostEqual(sum(selection['leaf_weights'].values()), 1)
        self.assertEqual(len(selection['leaf_weights']), 3)

    def test_temperature_plateaus_and_endpoint_clamps(self):
        for cct, expected in ((100, 0), (2300, 0), (2600, 10), (3400, 10), (3500, 15), (12000, 30)):
            self.assertAlmostEqual(self.select(cct, 200)[0][0], expected)

    def test_unsupported_layout_is_rejected(self):
        fixture = Fixture()
        with self.assertRaisesRegex(ValueError, 'trigger count|child reference'):
            colour.select_colour(fixture, fixture.parts, 3, 1, 3300, 200)

    def test_nonfinite_input_is_rejected(self):
        for cct, lux in ((float('nan'), 200), (3300, float('inf')), (0, 200)):
            with self.assertRaisesRegex(ValueError, 'trigger input'):
                self.select(cct, lux)


if __name__ == '__main__':
    unittest.main()
