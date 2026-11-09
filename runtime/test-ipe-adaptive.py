#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Program selection helpers of ipe-adaptive.py; no hardware."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('adaptive', Path(__file__).with_name('ipe-adaptive.py'))
adaptive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adaptive)


class RelievedGain(unittest.TestCase):
    def test_off_and_low_gain_are_unchanged(self):
        for gain in (1, 2.5, 8, 16, 64):
            self.assertEqual(adaptive.relieved_gain(gain, 0), gain)
        for gain in (1, 4, 8):
            self.assertAlmostEqual(adaptive.relieved_gain(gain, 1.0), gain)

    def test_full_relief_from_sixteen(self):
        for gain in (16, 24, 32, 64):
            self.assertAlmostEqual(adaptive.relieved_gain(gain, 1.0), gain / 2)

    def test_selection_never_steps_backwards(self):
        for relief in (0.5, 1.0):
            gains = [2 ** (i / 16) for i in range(97)]
            selected = [adaptive.relieved_gain(g, relief) for g in gains]
            self.assertTrue(all(b >= a - 1e-9 for a, b in zip(selected, selected[1:])), relief)
            self.assertTrue(all(1 <= s <= g + 1e-9 for s, g in zip(selected, gains)))

    def test_reference_gains_replace_both_yaml_layouts(self):
        inline = 'awb:\n  # note\n  raw_reference_gains: [1.88, 1.90]\n  blue_bias: 0.94\n'
        block = 'awb:\n  raw_reference_gains:\n  - 1.884930453\n  - 1.903106151\n  blue_bias: 0.94\n'
        for text in (inline, block):
            out = adaptive.replace_reference_gains(text, 1.922, 1.556)
            self.assertIn('  raw_reference_gains: [1.922, 1.556]\n  blue_bias: 0.94\n', out)
            self.assertEqual(out.count('raw_reference_gains'), 1)
            self.assertNotIn('1.90', out.replace('1.922', ''))
        with self.assertRaises(ValueError):
            adaptive.replace_reference_gains('awb:\n  blue_bias: 0.94\n', 1.9, 1.5)

    def test_tone_limit(self):
        tone = adaptive.Tone(target=145, max_gain=2.0)
        ctx = dict(ae=True, gain=16.0, exposure_us=33000, wb=(1.9, 1.9))
        for _ in range(40):
            tone.observe(dict(ctx), 60, 50)
        self.assertEqual(tone.gain, 2.0)


if __name__ == '__main__':
    unittest.main()
