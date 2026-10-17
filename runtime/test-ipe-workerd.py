#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Worker daemon protocol with a stubbed session: start/busy/status/stop/EOF."""
import array, importlib.util, json, os, socket, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('workerd', Path(__file__).with_name('ipe-workerd.py'))
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)


class FakeSession:
    instances = []
    fail_next = False

    def __init__(self, args, memfd, control_fd):
        self.started = time.monotonic(); self.dir = Path(args.sessions) / f'fake-{len(self.instances)}'
        self.fds = (os.fstat(memfd).st_size, os.fstat(control_fd).st_mode)
        self.running = not FakeSession.fail_next; FakeSession.fail_next = False
        self.stopped = None; self.instances.append(self)

    def wait_started(self, timeout, stopping=None): return self.running
    def alive(self): return self.running
    def stop(self, timeout=45): self.running = False; self.stopped = time.monotonic(); return 143
    def tail(self, n=12): return ['fake log']


class WorkerdTests(unittest.TestCase):
    def setUp(self):
        FakeSession.instances = []
        self.temp = tempfile.TemporaryDirectory(); sock = Path(self.temp.name) / 'control'
        self.patches = [patch.object(d, 'Session', FakeSession), patch.object(d.os, 'geteuid', return_value=0),
                        patch.object(sys, 'argv', ['workerd', '--socket', str(sock), '--sessions', self.temp.name])]
        for p in self.patches: p.start()
        self.stopping = threading.Event()
        parsed = type('A', (), {})(); parsed.socket = sock; parsed.sessions = self.temp.name; parsed.owner = None; parsed.group = None
        parsed.icp_module = ''; parsed.icp_args = ''; parsed.max_seconds = 0; parsed.start_timeout = 5; parsed.session_args = []
        self.thread = threading.Thread(target=d.serve_forever, args=(parsed, self.stopping), daemon=True); self.thread.start()
        for _ in range(50):
            if sock.exists(): break
            time.sleep(.05)
        self.sock = sock

    def tearDown(self):
        self.stopping.set(); self.thread.join(3)
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def client(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); c.settimeout(5); c.connect(str(self.sock)); return c

    def start(self, c):
        memfd = os.memfd_create('f'); os.ftruncate(memfd, 4096); a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.sendmsg([json.dumps(dict(type='start')).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [memfd, b.fileno()]))])
        os.close(memfd); b.close()
        return json.loads(c.recv(65536)), a

    def test_start_status_busy_stop(self):
        c = self.client()
        reply, keep = self.start(c)
        self.assertEqual(reply['type'], 'started'); self.assertEqual(FakeSession.instances[-1].fds[0], 4096)
        c.send(json.dumps(dict(type='status')).encode()); self.assertTrue(json.loads(c.recv(65536))['running'])
        busy, extra = self.start(c); extra.close()
        self.assertEqual(busy['type'], 'busy'); self.assertEqual(len(FakeSession.instances), 1)
        c.send(json.dumps(dict(type='stop')).encode()); self.assertEqual(json.loads(c.recv(65536))['exit'], 143)
        self.assertIsNotNone(FakeSession.instances[0].stopped)
        c.send(json.dumps(dict(type='status')).encode()); self.assertFalse(json.loads(c.recv(65536))['running'])
        c.close(); keep.close()

    def test_failed_start_reports_log_and_disconnect_stops(self):
        c = self.client(); FakeSession.fail_next = True
        reply, keep = self.start(c); self.assertEqual(reply['type'], 'failed'); self.assertEqual(reply['log'], ['fake log'])
        reply, keep2 = self.start(c); self.assertEqual(reply['type'], 'started')
        c.close()
        for _ in range(50):
            if FakeSession.instances[-1].stopped: break
            time.sleep(.05)
        self.assertIsNotNone(FakeSession.instances[-1].stopped)
        # a second client is served after the first disconnects
        c2 = self.client(); c2.send(json.dumps(dict(type='status')).encode()); self.assertEqual(json.loads(c2.recv(65536))['sessions'], 1); c2.close()
        keep.close(); keep2.close()

    def test_shutdown_with_connected_idle_client(self):
        c = self.client()
        c.send(json.dumps(dict(type='status')).encode()); c.recv(65536)
        self.stopping.set(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(c.recv(65536), b''); c.close()

    def test_shutdown_stops_active_session_without_client_disconnect(self):
        c = self.client(); result, keep = self.start(c)
        self.assertEqual(result['type'], 'started')
        self.stopping.set(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertIsNotNone(FakeSession.instances[-1].stopped)
        self.assertEqual(c.recv(65536), b''); c.close(); keep.close()

    def test_shutdown_during_startup(self):
        entered = threading.Event()
        def waiting(session, timeout, stopping=None):
            entered.set(); stopping.wait(2); return False
        with patch.object(FakeSession, 'wait_started', waiting):
            c = self.client()
            result = []
            t = threading.Thread(target=lambda: result.append(self.start(c)))
            t.start(); self.assertTrue(entered.wait(2)); self.stopping.set()
            t.join(2); self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
            self.assertEqual(result[0][0]['type'], 'failed')
            self.assertIsNotNone(FakeSession.instances[-1].stopped)
            result[0][1].close(); c.close()


if __name__ == '__main__':
    unittest.main()
