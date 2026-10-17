#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Real socket/descriptor/mapping tests; memfds stand in for camera DMA-BUFs.

Only DMA_BUF_IOCTL_SYNC is mocked. Actual camera ioctl success is a live gate.
"""
import array
import errno
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('lease',Path(__file__).with_name('ipe-capture-lease.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.r=m.Receiver(Path(self.temp.name)/'capture.sock')
        self.peer=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        self.peer.connect(str(self.r.path));self.r.accept()
        self.fds=[]
        self.ioctls=[]
        self.syncpatch=patch.object(m.fcntl,'ioctl',side_effect=lambda fd,cmd,data:self.ioctls.append((fd,cmd,struct.unpack('Q',data)[0])))
        self.syncpatch.start()
    def tearDown(self):
        self.syncpatch.stop();self.peer.close();self.r.close()
        for fd in self.fds:os.close(fd)
        self.temp.cleanup()
    def send(self,msg,fds=()):
        self.peer.sendmsg([json.dumps(msg).encode()],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',fds))] if fds else [])
        return self.r.receive()
    def register(self,ident,fd=None,planes=None):
        if fd is None:
            fd=os.memfd_create('camera-fixture',os.MFD_CLOEXEC)
            os.ftruncate(fd,m.SIZE);self.fds.append(fd)
        planes=planes or [dict(offset=0,length=m.W*m.H),dict(offset=m.W*m.H,length=m.W*m.H//2)]
        self.send(dict(type='buffer',id=ident,planes=planes),[fd,fd])
        return fd
    def start(self):
        for i in range(4):self.register(i)
        self.send(dict(type='start',version=1,width=m.W,height=m.H,stride=m.W,buffers=4))
    def frame(self,ident=0,seq=12):
        return self.send(dict(type='frame',id=ident,sequence=seq,timestamp_ns=time.monotonic_ns(),metadata={'AeEnable':'true'}))
    def test_full_frame_copy_survives_reuse(self):
        self.start();fd=self.fds[0]
        expected=(bytes(range(256))*(m.SIZE//256+1))[:m.SIZE];os.pwrite(fd,expected,0)
        f=self.frame();data=self.r.copy(f)
        self.assertEqual(data,expected)
        self.assertEqual([x[2] for x in self.ioctls],[1,5])
        self.r.release(f);self.assertEqual(self.peer.recv(128),b'ACK1 0 12')
        os.pwrite(fd,b'\xff'*m.SIZE,0)
        self.assertEqual(data,expected) # no borrowed map escapes
        self.assertEqual(self.r.copy(self.frame(seq=13)),b'\xff'*m.SIZE)
    def test_view_reads_planes_in_place_under_sync(self):
        self.start();fd=self.fds[0]
        expected=(bytes(range(256))*(m.SIZE//256+1))[:m.SIZE];os.pwrite(fd,expected,0)
        f=self.frame()
        with self.r.view(f) as (y,uv):
            self.assertEqual([x[2] for x in self.ioctls],[1]) # START before any read
            self.assertEqual((y.nbytes,uv.nbytes),(m.W*m.H,m.W*m.H//2))
            self.assertTrue(y.readonly and uv.readonly)
            self.assertEqual(bytes(y)+bytes(uv),expected)
        self.assertEqual([x[2] for x in self.ioctls],[1,5])
        self.assertEqual((self.r.copied_frames,self.r.copied_bytes,len(self.r.copy_ms)),(1,m.SIZE,1))
        self.r.release(f);self.assertEqual(self.peer.recv(128),b'ACK1 0 12')
        with self.assertRaises(RuntimeError):
            with self.r.view(f):pass
        f=self.frame(seq=13)
        with self.assertRaises(ValueError):
            with self.r.view(f):raise ValueError('reader failure')
        self.assertEqual([x[2] for x in self.ioctls][-2:],[1,5]) # bracket ends on failure
        self.assertTrue(self.r.poisoned)
        with self.assertRaises(RuntimeError):self.r.release(f)
    def test_no_copy_or_ack_for_registration(self):
        self.start();self.assertFalse(self.ioctls)
        self.assertEqual(self.r.copied_bytes,0)
        self.peer.settimeout(.01)
        with self.assertRaises(socket.timeout):self.peer.recv(128)
    def test_readonly_maps_and_noninheritable_descriptors(self):
        self.start()
        for b in self.r.buffers.values():
            self.assertTrue(all(not os.get_inheritable(fd) for fd in b.fds))
            with self.assertRaises(TypeError):b.maps[0][0]=1
    def test_duplicate_release_and_copy_rejected(self):
        self.start();f=self.frame();self.r.release(f)
        with self.assertRaises(RuntimeError):self.r.release(f)
        with self.assertRaises(RuntimeError):self.r.copy(f)
    def test_reused_slot_before_ack_poisoned(self):
        self.start();self.frame()
        with self.assertRaises(ValueError):self.frame(seq=13)
        self.assertTrue(self.r.poisoned)
    def test_stale_sequence_rejected(self):
        self.start();f=self.frame();self.r.release(f)
        with self.assertRaises(ValueError):self.frame(1,12)
    def test_out_of_bounds_and_alias_rejected(self):
        with self.assertRaises(ValueError):self.register(0,planes=[dict(offset=0,length=m.W*m.H),dict(offset=m.SIZE,length=m.W*m.H//2)])
    def test_alias_between_capture_slots_rejected(self):
        fd=self.register(0)
        with self.assertRaises(ValueError):self.register(1,fd)
    def test_peer_death(self):
        self.start();f=self.frame();self.peer.close()
        with self.assertRaises(EOFError):self.r.receive()
        with self.assertRaises(RuntimeError):self.r.copy(f)
    def test_clean_footer_retains_pending_leases_until_stop(self):
        self.start();self.frame()
        self.send(dict(type='end',sent=1,released=0,pending=1,max_pending=1,failed=False))
        self.peer.close();self.assertIsNone(self.r.receive())
        self.assertEqual(self.r.footer['pending'],1)
    def test_sync_failure_never_releases(self):
        self.start();f=self.frame()
        with patch.object(m.fcntl,'ioctl',side_effect=OSError(errno.EIO,'fixture')):
            with self.assertRaises(OSError):self.r.copy(f)
        self.assertTrue(self.r.poisoned)
        with self.assertRaises(RuntimeError):self.r.release(f)
    def test_sync_end_failure_poisoned(self):
        self.start();f=self.frame()
        def sync(fd,cmd,data):
            if struct.unpack('Q',data)[0]==5:raise OSError(errno.EIO,'end fixture')
        with patch.object(m.fcntl,'ioctl',side_effect=sync):
            with self.assertRaises(OSError):self.r.copy(f)
        self.assertTrue(self.r.poisoned)
    def test_sync_retry_eintr_and_eagain(self):
        with patch.object(m.fcntl,'ioctl',side_effect=[OSError(errno.EINTR,'x'),OSError(errno.EAGAIN,'x'),0]) as ioctl:
            m.sync(123);self.assertEqual(ioctl.call_count,3)
    def test_truncated_packet_closes_received_descriptors(self):
        self.start();before=len(list(Path('/proc/self/fd').iterdir()))
        self.peer.sendmsg([b'X'*9000],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[self.fds[0]]))])
        with self.assertRaises(ValueError):self.r.receive()
        self.assertEqual(before,len(list(Path('/proc/self/fd').iterdir())))
    def test_close_releases_all_maps_and_fds(self):
        self.start();buffers=list(self.r.buffers.values());fds=[fd for b in buffers for fd in b.fds]
        self.r.close()
        self.assertTrue(all(not b.maps and not b.fds for b in buffers))
        for fd in fds:
            with self.assertRaises(OSError):os.fstat(fd)

if __name__=='__main__':unittest.main()
