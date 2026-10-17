#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Real shared mappings and sockets: ownership, failures and descriptor lifetime."""
import fcntl
import importlib.util
import mmap
import os
import socket
from pathlib import Path
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('handoff', Path(__file__).with_name('ipe-handoff.py'))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.p = h.Producer()
        self.c = h.Consumer(os.dup(self.p.fd), os.dup(self.p.child.fileno()))
        self.p.child_started()
        self.errors = []

    def tearDown(self):
        self.p.close()
        self.c.close()
        self.assertEqual(self.errors, [])

    def worker(self, target):
        def run():
            try:
                target()
            except BaseException as e:
                self.errors.append(repr(e))
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def test_external_descriptors_ready_handshake(self):
        # A persistent publisher creates the memfd and socketpair; the worker adopts them.
        fd = os.memfd_create('publisher-owned', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, h.SIZE)
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        producer = h.Producer.from_fds(os.dup(fd), b.detach())
        self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & h.SEALS, h.SEALS)
        keep = os.dup(fd)   # Consumer closes the descriptor it is given
        consumer = h.Consumer(fd, a.detach(), wait_ready=True, timeout=5)
        frame = bytes(range(256)) * (h.SIZE // 256)
        done = []
        thread = threading.Thread(target=lambda: done.append(producer.send(frame, 7, 123)))
        thread.start()
        seq, ts, data = consumer.receive()
        self.assertEqual((seq, ts, data), (7, 123, frame))
        consumer.acknowledge(); thread.join(3)
        self.assertTrue(done)
        with self.assertRaises(ValueError):   # a sealed memfd cannot be adopted twice
            h.Producer.from_fds(os.dup(keep), socket.socketpair()[0].detach())
        os.close(keep); producer.close(); consumer.close()

    def test_exact_pixels_and_reuse_after_ack(self):
        frames = [bytes([x])*h.SIZE for x in (0, 17, 255, 93)]
        retained = []
        def consume():
            for i in range(16):
                seq, stamp, data = self.c.receive()
                self.assertEqual((seq, stamp), (i, i+123))
                self.assertEqual(data, frames[i%4])
                retained.append(data)
                self.c.acknowledge()
        t = self.worker(consume)
        for i in range(16):
            self.p.send(frames[i%4], i, i+123)
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertTrue(all(data == frames[i%4] for i,data in enumerate(retained)))

    def test_slow_ack_preserves_owned_frame(self):
        ready = threading.Event()
        def consume():
            _, _, data = self.c.receive()
            ready.set()
            time.sleep(.15)
            self.assertEqual(self.c.mapping[:], data)
            self.c.acknowledge()
        t = self.worker(consume)
        began = time.monotonic()
        self.p.send(b'a'*h.SIZE, 1, 0)
        self.assertTrue(ready.is_set())
        self.assertGreater(time.monotonic()-began, .15)
        t.join(3)

    def test_stalled_reader_timeout_poison(self):
        with self.assertRaises(TimeoutError):
            self.p.send(b'a'*h.SIZE, 1, 0, timeout=.05)
        with self.assertRaisesRegex(RuntimeError, 'poisoned'):
            self.p.send(b'b'*h.SIZE, 2, 0)
        self.assertEqual(self.c.mapping[:], b'a'*h.SIZE)

    def test_stop_interrupts_wait(self):
        stop = threading.Event()
        timer = threading.Timer(.05, stop.set)
        timer.start()
        with self.assertRaises(InterruptedError):
            self.p.send(bytes(h.SIZE), 1, 0, stop)
        timer.join()
        self.assertTrue(self.p.failed)

    def test_peer_death_detected(self):
        self.c.close()
        with self.assertRaises((BrokenPipeError, ConnectionResetError, EOFError)):
            self.p.send(bytes(h.SIZE), 1, 0)
        self.assertTrue(self.p.failed)

    def test_wrong_ack_poison(self):
        def consume():
            self.c.receive()
            self.c.socket.send(h.ACK.pack(b'ACK1', 999, time.monotonic_ns()))
        t = self.worker(consume)
        with self.assertRaises(ValueError):
            self.p.send(bytes(h.SIZE), 1, 0)
        t.join(3)
        self.assertTrue(self.p.failed)

    def test_packet_bounds_and_duplicate_guards(self):
        self.p.socket.send(b'x'*(h.FRAME.size+1))
        with self.assertRaises(ValueError):
            self.c.receive()
        self.p.socket.send(h.FRAME.pack(b'IPE1', 5, 0))
        self.c.receive()
        with self.assertRaises(RuntimeError):
            self.c.receive()
        self.c.acknowledge()
        self.p.socket.recv(100)
        self.p.socket.send(h.FRAME.pack(b'IPE1', 5, 0))
        with self.assertRaises(ValueError):
            self.c.receive()

    def test_read_only_and_sealed_size(self):
        with self.assertRaises(TypeError):
            self.c.mapping[0] = 7
        other = h.Producer()
        try:
            with self.assertRaises(PermissionError):
                os.pwrite(other.fd, b'x', 0)
            for size in (1, h.SIZE*2):
                with self.assertRaises(PermissionError):
                    os.ftruncate(other.fd, size)
            with self.assertRaises(PermissionError):
                mmap.mmap(other.fd, h.SIZE, prot=mmap.PROT_WRITE)
            self.assertEqual(fcntl.fcntl(other.fd, fcntl.F_GET_SEALS), h.SEALS)
        finally:
            other.close()

    def test_invalid_input_does_not_transfer(self):
        for frame, seq, stamp in [(b'x', 1, 0), (bytes(h.SIZE), -1, 0), (bytes(h.SIZE), 1, -1)]:
            with self.assertRaises(ValueError):
                self.p.send(frame, seq, stamp)
        self.assertFalse(self.p.failed)

    def test_short_ack_poison(self):
        self.c.socket.send(b'ACK1')
        with self.assertRaises(ValueError):
            self.p.send(bytes(h.SIZE), 1, 0)
        self.assertTrue(self.p.failed)

    def test_unsealed_mapping_rejected_without_fd_leak(self):
        before = len(os.listdir('/proc/self/fd'))
        fd = os.memfd_create('invalid', os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, h.SIZE)
        with self.assertRaises(ValueError):
            h.Consumer(fd, os.dup(self.p.socket.fileno()))
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_no_descriptor_leak(self):
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(40):
            p = h.Producer()
            c = h.Consumer(os.dup(p.fd), os.dup(p.child.fileno()))
            p.child_started()
            p.close()
            c.close()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
