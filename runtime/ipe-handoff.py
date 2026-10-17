#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""One bounded CPU frame shared with the publisher; ACK transfers ownership.

The producer alone retains a writable mapping. Resize/future-write seals are
set before descriptors cross the process boundary. The reader copies into its
own storage before acknowledging, so GStreamer never retains this mapping.
This is a CPU transport, not a DMA-BUF or hardware fence implementation.
"""
import fcntl
import mmap
import os
import select
import socket
import struct
import time

SIZE = 3840 * 2160 * 3 // 2
FRAME = struct.Struct('<4sQQ')
ACK = struct.Struct('<4sQQ')
SEALS = (fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK |
         fcntl.F_SEAL_FUTURE_WRITE | fcntl.F_SEAL_SEAL)


def packet(sock, size):
    data, _, flags, _ = sock.recvmsg(size)
    if not data:
        raise EOFError('Shared-frame peer disconnected')
    if flags & socket.MSG_TRUNC or len(data) != size:
        raise ValueError('Invalid shared-frame packet size')
    return data


READY = struct.Struct('<4sQ')


class Producer:
    def __init__(self, fd=None, control_fd=None):
        """Own the writable frame mapping.

        Without arguments the producer creates the memfd and a socketpair for a
        child publisher (child_args/child_fds). With `fd` and `control_fd` it
        adopts descriptors handed over by an already running publisher: the
        memfd must be unsealed and SIZE bytes; the producer maps it writable,
        applies the seals, then sends READY so the consumer can verify the
        seals before mapping read-only.
        """
        external = fd is not None
        if external != (control_fd is not None):
            raise ValueError('External handoff needs both a memfd and a control socket')
        self.fd = fd if external else os.memfd_create('ipe-publisher-frame', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        self.mapping = self.socket = self.child = None
        self.failed = False
        self.last_sequence = -1
        self.count = 0
        try:
            if external:
                if os.fstat(self.fd).st_size != SIZE:
                    raise ValueError('External shared frame has the wrong size')
                if fcntl.fcntl(self.fd, fcntl.F_GET_SEALS) & (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_FUTURE_WRITE):
                    raise ValueError('External shared frame is already write-sealed')
            else:
                os.ftruncate(self.fd, SIZE)
            self.mapping = mmap.mmap(self.fd, SIZE, flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE)
            fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, SEALS)
            if external:
                self.socket = socket.socket(fileno=control_fd)
                self.socket.setblocking(False)
                ready = READY.pack(b'RDY1', time.monotonic_ns())
                if self.socket.send(ready) != len(ready):
                    raise RuntimeError('Partial ready packet')
            else:
                self.socket, self.child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                self.socket.setblocking(False)
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_fds(cls, fd, control_fd):
        return cls(fd, control_fd)

    def child_args(self):
        return ['--shared-fd', str(self.fd), '--control-fd', str(self.child.fileno())]

    def child_fds(self):
        return (self.fd, self.child.fileno())

    def child_started(self):
        # Closing the duplicate is essential for detecting publisher death.
        self.child.close()
        self.child = None
        os.close(self.fd)
        self.fd = -1

    def send(self, frame, sequence, timestamp, stop=None, timeout=3):
        if self.failed:
            raise RuntimeError('Shared-frame transport is poisoned')
        if len(frame) != SIZE or sequence <= self.last_sequence or timestamp < 0:
            raise ValueError('Invalid frame size, sequence or timestamp')
        header = FRAME.pack(b'IPE1', sequence, timestamp)
        began = time.monotonic_ns()
        deadline = time.monotonic() + timeout
        try:
            self.mapping[:] = frame
            if self.socket.send(header) != len(header):
                raise RuntimeError('Partial shared-frame descriptor')
            while True:
                if stop is not None and stop.is_set():
                    raise InterruptedError('Shared-frame handoff stopped')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Publisher did not acknowledge its shared frame')
                if select.select([self.socket], [], [], min(.1, remaining))[0]:
                    magic, acknowledged, accepted = ACK.unpack(packet(self.socket, ACK.size))
                    if magic != b'ACK1' or acknowledged != sequence or not began <= accepted <= time.monotonic_ns():
                        raise ValueError('Invalid shared-frame acknowledgement')
                    self.last_sequence = sequence
                    self.count += 1
                    return accepted
        except BaseException:
            # Never overwrite a frame after an uncertain ownership transfer.
            self.failed = True
            raise

    def close(self):
        if self.socket is not None:
            # Drain an ACK left unread by an interrupted send so the consumer
            # sees EOF rather than a reset when this end closes.
            try:
                while self.socket.recv(ACK.size, socket.MSG_DONTWAIT):
                    pass
            except OSError:
                pass
        for name in ('child', 'socket', 'mapping'):
            item = getattr(self, name, None)
            if item is not None:
                item.close()
                setattr(self, name, None)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Consumer:
    def __init__(self, fd, control_fd, wait_ready=False, timeout=30):
        self.mapping = None
        self.socket = socket.socket(fileno=control_fd)
        self.last_sequence = -1
        self.pending = None
        try:
            if wait_ready:
                # The producer seals the memfd we created; map only afterwards.
                self.socket.settimeout(timeout)
                try:
                    magic, _ = READY.unpack(packet(self.socket, READY.size))
                finally:
                    self.socket.settimeout(None)
                if magic != b'RDY1':
                    raise ValueError('Expected the producer ready packet')
            if os.fstat(fd).st_size != SIZE or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS != SEALS:
                raise ValueError('Shared frame has invalid bounds or seals')
            self.mapping = mmap.mmap(fd, SIZE, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(fd)

    def receive(self):
        if self.pending is not None:
            raise RuntimeError('Previous shared frame has not been acknowledged')
        magic, sequence, timestamp = FRAME.unpack(packet(self.socket, FRAME.size))
        if magic != b'IPE1' or sequence <= self.last_sequence:
            raise ValueError('Invalid or repeated shared-frame descriptor')
        self.pending = sequence
        # bytes is the fast PyGObject Gst.Buffer.fill input. Passing a
        # memoryview invokes per-element marshalling on this installed build.
        return sequence, timestamp, self.mapping[:]

    def acknowledge(self):
        if self.pending is None:
            raise RuntimeError('No shared frame to acknowledge')
        accepted = time.monotonic_ns()
        payload = ACK.pack(b'ACK1', self.pending, accepted)
        if self.socket.send(payload) != len(payload):
            raise RuntimeError('Partial shared-frame acknowledgement')
        self.last_sequence = self.pending
        self.pending = None
        return accepted

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        self.socket.close()
