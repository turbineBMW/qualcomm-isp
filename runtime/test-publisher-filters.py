#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Publisher filter drop-ins: node matching, placeholders, formats, validation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publisher_filters import load

ENV = dict(HOME='/home/u', XDG_CONFIG_HOME='/home/u/.config', XDG_RUNTIME_DIR='/run/user/1000')


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, **spec):
        (self.dir / name).write_text(json.dumps(spec))

    def test_missing_directory_means_no_filters(self):
        filters = load('front', self.dir / 'absent', ENV)
        self.assertFalse(filters)
        self.assertIsNone(filters.formats)

    def test_filter_for_matching_node_with_placeholders_and_formats(self):
        self.write('50-autoframe.json', nodes=['front'],
                   filter='ispautoframe control-file={config_home}/qualcomm-isp/autoframe.conf ! capsfilter caps=video/x-raw,width=1920,height=1080',
                   formats=[[1920, 1080], [1280, 720]])
        filters = load('front', self.dir, ENV)
        self.assertEqual(filters.descriptions, [
            'ispautoframe control-file=/home/u/.config/qualcomm-isp/autoframe.conf ! capsfilter caps=video/x-raw,width=1920,height=1080'])
        self.assertEqual(filters.formats, [(1920, 1080), (1280, 720)])
        self.assertEqual(filters.files, [str(self.dir / '50-autoframe.json')])

    def test_other_node_and_disabled_files_are_skipped(self):
        self.write('50-autoframe.json', nodes=['front'], filter='identity', formats=[[1920, 1080]])
        self.write('60-off.json', filter='identity name=off', enabled=False)
        filters = load('rear', self.dir, ENV)
        self.assertFalse(filters)
        self.assertIsNone(filters.formats)

    def test_without_nodes_applies_everywhere_in_name_order(self):
        self.write('70-b.json', filter='identity name=b')
        self.write('60-a.json', filter='identity name=a')
        self.assertEqual(load('anything', self.dir, ENV).descriptions, ['identity name=a', 'identity name=b'])

    def test_config_home_defaults_to_dot_config(self):
        self.write('50.json', filter='x={config_home}')
        self.assertEqual(load('n', self.dir, dict(HOME='/home/v')).descriptions, ['x=/home/v/.config'])

    def test_invalid_specs_are_errors(self):
        for name, spec in [('a.json', dict(filter='')), ('b.json', dict(filter='identity', formats=[])),
                           ('c.json', dict(filter='identity', formats=[[1920, 1081]])),
                           ('d.json', dict(filter='identity', formats=[[1920, True]])),
                           ('e.json', dict(filter='x={nope}')), ('f.json', [1, 2])]:
            with self.subTest(name=name):
                for old in self.dir.glob('*.json'):
                    old.unlink()
                (self.dir / name).write_text(json.dumps(spec))
                with self.assertRaises(ValueError):
                    load('n', self.dir, ENV)


if __name__ == '__main__':
    unittest.main()
