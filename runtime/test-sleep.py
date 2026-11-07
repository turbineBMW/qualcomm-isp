#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Both camera workers must quiesce and recover together at the sleep boundary."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('isp_sleep', Path(__file__).with_name('sleep.py'))
sleep = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, isp_config=types.SimpleNamespace(load=lambda: ({}, {'icp_device': 'test'}))):
    spec.loader.exec_module(sleep)


class SleepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in dict(STATE=root/'state.json', MODULE=root/'module', POWER=root/'power', LOCK=root/'lock').items():
            p = patch.object(sleep, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(sleep, 'control')
        self.control = p.start()
        self.addCleanup(p.stop)

    def test_both_cameras_restore_exact_active_units(self):
        enabled = {sleep.UNITS[0], sleep.UNITS[2], sleep.UNITS[3]}
        with patch.object(sleep, 'active', side_effect=lambda unit: unit in enabled):
            sleep.prepare()
        self.control.assert_called_once_with('stop', *[u for u in sleep.UNITS if u in enabled])
        sleep.restore()
        self.assertEqual(self.control.call_args.args, ('start', *[u for u in sleep.UNITS if u in enabled]))
        self.assertFalse(sleep.STATE.exists())

    def test_no_services_does_not_call_systemctl_without_units(self):
        with patch.object(sleep, 'active', return_value=False):
            sleep.prepare()
        sleep.restore()
        self.control.assert_not_called()

    def test_loaded_icp_refuses_sleep_and_restores_both_sockets(self):
        sleep.MODULE.mkdir()
        with patch.object(sleep, 'active', side_effect=lambda unit: unit.endswith('.socket')):
            with self.assertRaisesRegex(RuntimeError, 'ICP still loaded'):
                sleep.prepare()
        self.assertEqual(self.control.call_args.args, ('start', sleep.UNITS[0], sleep.UNITS[2]))
        self.assertFalse(sleep.STATE.exists())

    def test_runtime_reference_refuses_sleep_and_recovers(self):
        sleep.POWER.mkdir()
        (sleep.POWER/'runtime_usage').write_text('1')
        with patch.object(sleep, 'active', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'runtime-PM'):
                sleep.prepare()
        self.assertEqual(self.control.call_args.args, ('start', *sleep.UNITS))


if __name__ == '__main__':
    unittest.main()
