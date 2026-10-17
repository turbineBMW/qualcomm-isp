#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Private cam DMA-BUF leases: completion -> CPU read -> ACK -> camera reuse.

Only selected frames are copied. CPU cache sync is not a hardware fence: cam
sends completion after libcamera request completion and retains that request
until the exact ACK. IPE still consumes a separate padded DMA allocation.
"""
import array
from collections import deque
from contextlib import contextmanager
import errno
import fcntl
import json
import mmap
import os
import socket
import struct
import time

W, H = 3840, 2160
SIZE = W * H * 3 // 2
DMA_BUF_IOCTL_SYNC = 0x40086200  # _IOW('b', 0, struct dma_buf_sync), Linux arm64


def sync(fd, end=False):
    deadline = time.monotonic() + 1
    while True:
        try:
            fcntl.ioctl(fd, DMA_BUF_IOCTL_SYNC, struct.pack('Q', 1 | (4 if end else 0)))
            return
        except OSError as exc:
            if exc.errno not in (errno.EINTR, errno.EAGAIN) or time.monotonic() >= deadline:
                raise


class Buffer:
    def __init__(self, planes, fds):
        self.fds, self.maps, self.planes = [], [], []
        self.identities = set()
        try:
            if len(planes) != 2 or len(fds) != 2:
                raise ValueError('Expected two NV12 plane descriptors')
            unique = {}
            for plane, fd, needed in zip(planes, fds, (W*H, W*H//2)):
                offset, length = plane['offset'], plane['length']
                size = os.lseek(fd, 0, os.SEEK_END)
                if (type(offset) is not int or type(length) is not int or
                        not 0 <= offset <= size or not needed <= length <= size-offset or size > 32<<20):
                    raise ValueError('Plane lies outside its DMA-BUF or native geometry')
                st = os.fstat(fd)
                key = st.st_dev, st.st_ino
                self.identities.add(key)
                if key not in unique:
                    duplicate = os.dup(fd)
                    self.fds.append(duplicate)
                    mapped = mmap.mmap(duplicate, size, flags=mmap.MAP_SHARED,
                                       prot=mmap.PROT_READ, trackfd=False)
                    self.maps.append(mapped)
                    unique[key] = mapped
                self.planes.append((unique[key], offset, needed))
        except BaseException:
            self.close()
            raise

    def copy(self):
        started = []
        try:
            for fd in self.fds:
                sync(fd)
                started.append(fd)
            data = bytearray(SIZE)
            pos = 0
            for mapped, offset, size in self.planes:
                with memoryview(mapped) as view:
                    data[pos:pos+size] = view[offset:offset+size]
                pos += size
            return data
        finally:
            # End every successful START even when a later START/copy fails.
            # Any END failure prevents the caller from ACKing this buffer.
            errors = []
            for fd in reversed(started):
                try:
                    sync(fd, end=True)
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]

    @contextmanager
    def view(self):
        """Yield read-only (Y, UV) plane views under the same cache-sync bracket as copy().

        The views are released before the bracket ends, so no mapped storage
        escapes; the caller must finish reading inside the block.
        """
        started, views = [], []
        try:
            for fd in self.fds:
                sync(fd)
                started.append(fd)
            for mapped, offset, size in self.planes:
                views.append(memoryview(mapped)[offset:offset+size])
            yield tuple(views)
        finally:
            for view in views:
                view.release()
            errors = []
            for fd in reversed(started):
                try:
                    sync(fd, end=True)
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]

    def close(self):
        for mapped in self.maps:
            mapped.close()
        self.maps.clear()
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()


class Receiver:
    def __init__(self, path, uid=None, gid=None):
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        self.peer = None
        self.buffers, self.active = {}, {}
        self.last_sequence = -1
        self.started = False
        self.footer = None
        self.poisoned = False
        self.copied_frames = self.copied_bytes = self.received = self.released = 0
        self.copy_ms = deque(maxlen=200000)   # bounded for long sessions; audits compare min(copied, 200000)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        os.chown(path, self.uid, self.gid)
        self.listener.listen(1)
        self.listener.settimeout(.2)

    def accept(self):
        self.peer, _ = self.listener.accept()
        self.peer.settimeout(.2)
        self.listener.close()
        pid, uid, gid = struct.unpack('3i', self.peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != self.uid:
            raise ValueError('Lease sender is not the desktop user')
        self.peer_pid = pid

    def receive(self):
        if self.poisoned:
            raise RuntimeError('Lease transport is poisoned')
        fds = []
        try:
            data, anc, flags, _ = self.peer.recvmsg(8192, socket.CMSG_SPACE(2*4), socket.MSG_CMSG_CLOEXEC)
            for level, kind, payload in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array('i')
                    values.frombytes(payload[:len(payload)//values.itemsize*values.itemsize])
                    fds.extend(values)
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise ValueError('Truncated lease packet')
            if not data:
                if self.footer is None:
                    raise EOFError('Camera lease peer ended without a footer')
                return None
            msg = json.loads(data)
            kind = msg['type']
            if kind != 'buffer' and fds:
                raise ValueError('Unexpected descriptors')
            if kind == 'buffer':
                ident = msg['id']
                if self.started or type(ident) is not int or not 0 <= ident < 16 or ident in self.buffers:
                    raise ValueError('Invalid buffer registration')
                buffer = Buffer(msg['planes'], fds)
                if any(buffer.identities & old.identities for old in self.buffers.values()):
                    buffer.close()
                    raise ValueError('Different capture slots alias the same storage')
                self.buffers[ident] = buffer
            elif kind == 'start':
                count = msg['buffers']
                if (self.started or not 4 <= count <= 16 or set(self.buffers) != set(range(count)) or
                        (msg['version'], msg['width'], msg['height'], msg['stride']) != (1, W, H, W)):
                    raise ValueError('Unsupported lease geometry or pool')
                self.started = True
            elif kind == 'frame':
                ident, seq, ts = (msg[k] for k in ('id','sequence','timestamp_ns'))
                if (not self.started or self.footer or ident not in self.buffers or ident in self.active or
                        type(seq) is not int or seq <= self.last_sequence or type(ts) is not int or ts <= 0 or
                        not isinstance(msg['metadata'], dict) or
                        any(not isinstance(v,str) for v in msg['metadata'].values())):
                    raise ValueError('Invalid, reordered or already leased frame')
                self.active[ident] = seq
                self.last_sequence = seq
                self.received += 1
                return msg
            elif kind == 'end':
                if (not self.started or self.footer or msg['failed'] or msg['sent'] != self.received or
                        msg['released'] + msg['pending'] != msg['sent'] or msg['released'] > self.released or
                        not 0 <= msg['pending'] <= msg['max_pending'] <= len(self.buffers)):
                    raise ValueError('Invalid or failed camera lease footer')
                self.footer = msg
            else:
                raise ValueError('Unknown lease packet')
            return {}  # registration/footer, not a frame
        except socket.timeout:
            raise
        except BaseException:
            self.poisoned = True
            raise
        finally:
            for fd in fds:
                os.close(fd)

    def copy(self, frame):
        if self.poisoned or self.active.get(frame['id']) != frame['sequence']:
            raise RuntimeError('Copy requires an active matching lease')
        began = time.monotonic_ns()
        try:
            data = self.buffers[frame['id']].copy()
        except BaseException:
            self.poisoned = True
            raise
        self.copy_ms.append((time.monotonic_ns()-began)/1e6)
        self.copied_frames += 1
        self.copied_bytes += len(data)
        return data

    @contextmanager
    def view(self, frame):
        """Read the leased planes in place. Counted like a copy: pixels were read."""
        if self.poisoned or self.active.get(frame['id']) != frame['sequence']:
            raise RuntimeError('View requires an active matching lease')
        began = time.monotonic_ns()
        try:
            with self.buffers[frame['id']].view() as planes:
                yield planes
        except BaseException:
            self.poisoned = True
            raise
        self.copy_ms.append((time.monotonic_ns()-began)/1e6)
        self.copied_frames += 1
        self.copied_bytes += SIZE

    def release(self, frame):
        ident, seq = frame['id'], frame['sequence']
        if self.poisoned or self.active.get(ident) != seq:
            raise RuntimeError('Release requires an active matching lease')
        try:
            packet = f'ACK1 {ident} {seq}'.encode()
            if self.peer.send(packet) != len(packet):
                raise RuntimeError('Short release packet')
            del self.active[ident]
            self.released += 1
        except BaseException:
            self.poisoned = True
            raise

    def summary(self):
        return dict(received=self.received, released=self.released, copied_frames=self.copied_frames,
                    copied_bytes=self.copied_bytes, copy_ms=list(self.copy_ms),
                    buffers=len(self.buffers), footer=self.footer, poisoned=self.poisoned)

    def close(self):
        # The caller first stops the camera and joins its reader. Do not release
        # uncertain leases on failure: camera.stop quiesces hardware instead.
        if self.peer:
            self.peer.close()
        self.listener.close()
        for buffer in self.buffers.values():
            buffer.close()
        self.buffers.clear()
        self.path.unlink(missing_ok=True)
