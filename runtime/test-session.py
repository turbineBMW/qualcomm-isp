#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""session.py stop escalation: a stuck ipe-live and its camera tree die by the deadline."""
import importlib.util, os, subprocess, sys, time, unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('session', Path(__file__).with_name('session.py'))
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)

# ipe-live stand-in: ignores SIGTERM, owns a camera-like child in its own session.
STUCK = r"""
import signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGUSR1, lambda *_: open(sys.argv[1] + '.usr1', 'w').write('dumped'))
camera = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], start_new_session=True)
open(sys.argv[1], 'w').write(str(camera.pid))
time.sleep(60)
"""


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return Path(f'/proc/{pid}/stat').read_text().split(')')[-1].split()[0] == 'Z'


class StopChildTests(unittest.TestCase):
    def test_stuck_child_is_dumped_and_its_tree_killed_by_the_deadline(self):
        out = Path(os.environ.get('TMPDIR', '/tmp')) / f'test-session-{os.getpid()}'
        child = subprocess.Popen([sys.executable, '-I', '-c', STUCK, str(out)])
        try:
            for _ in range(100):
                if out.exists() and out.read_text(): break
                time.sleep(.05)
            camera = int(out.read_text())
            began = time.monotonic()
            with patch.object(s, 'log'):
                s.stop_child(child, deadline=began + 1)
            self.assertLess(time.monotonic() - began, 5)
            self.assertEqual(child.returncode, -9)
            self.assertEqual(Path(str(out) + '.usr1').read_text(), 'dumped')
            for _ in range(40):
                if gone(camera): break
                time.sleep(.05)
            self.assertTrue(gone(camera), 'the camera outside the process group survived')
        finally:
            if child.poll() is None:
                child.kill()
            out.unlink(missing_ok=True); Path(str(out) + '.usr1').unlink(missing_ok=True)

    def test_exited_child_is_left_alone(self):
        child = subprocess.Popen([sys.executable, '-I', '-c', 'pass']); child.wait()
        with patch.object(s.proctree, 'kill_tree', return_value=[]) as kill:
            s.stop_child(child, deadline=time.monotonic())
        # Only the subreaper sweep for adopted orphans runs.
        kill.assert_called_once_with(os.getpid(), include_root=False)


if __name__ == '__main__':
    unittest.main()
