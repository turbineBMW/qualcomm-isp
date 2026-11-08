#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Precision layout, staging and DMA-address regressions without hardware."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
import numpy as np
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('stream',HERE/'ipe-stream.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class Buffer:
    def __init__(self,iova,size=128):
        self.iova=iova;self.data=bytearray(size);self.writes=[]
    def write(self,data,offset=0):
        self.writes.append((offset,bytes(data)))
        self.data[offset:offset+len(data)]=data
    def read(self,size,offset=0):return self.data[offset:offset+size]
    def read_mapped_into(self,dst,size,offset=0,destination_offset=0):
        dst[destination_offset:destination_offset+size]=self.read(size,offset)

class PrecisionTests(unittest.TestCase):
    def session(self,precision=2):
        x=s.Session.__new__(s.Session)
        x.w=4;x.h=4;x.n=24;x.internal_bytes=precision;x.failed=False
        x.inputs=[Buffer(0x1000)]*2;x.outputs=[Buffer(0x2000),Buffer(0x3000)]
        x.references=[Buffer(0x4000),Buffer(0x5000)]
        x.pending_inputs=[None,None];x.pd=Buffer(0x6000)
        x.pd_sizes={4:8,16:8}
        x.pd_offsets={'input4:0':0,'input4:1':8,'input16:0':16,'input16:1':24,'ref0':32,'ref1':40}
        x.reduce=lambda *args,**kw:{4:bytes(8),16:bytes(8)}
        x.lock=threading.RLock();x.sequence=0;x.previous=0;x.full=False
        x.frames={False:bytes(512),True:bytes(512)};x.frame=Buffer(0x7000,512)
        x.command=lambda *args,**kw:None;x.readback_mode='verify'
        return x

    def test_expansion_preserves_every_input_code(self):
        src=bytes(range(256))
        a=np.frombuffer(s.expand_nv12(src),'<u2')
        np.testing.assert_array_equal(a>>8,np.arange(256))
        self.assertTrue(np.all((a&255)==0))

    def test_staging_pads_and_never_writes_shared_dma_early(self):
        x=self.session();y=bytes(range(8));uv=bytes([121,130,122,131])
        x.prepare(0,planes=(y,uv));first=x.pending_inputs[0]
        x.prepare(1,planes=(bytes([9])*8,bytes([128])*4))
        self.assertEqual(x.inputs[0].writes,[])
        expected=y+y[-4:]*2+uv+uv
        self.assertEqual(first,s.expand_nv12(expected))
        token=x.submit(0)
        self.assertEqual(x.inputs[0].writes,[(0,first)])
        self.assertIsNone(x.pending_inputs[0]);self.assertIsNotNone(x.pending_inputs[1])
        self.assertTrue(token['seed'])
        # Input and seed-reference chroma offsets use two bytes per sample.
        self.assertEqual(struct.unpack_from('<II',x.frame.data,0xbc),(0x1000,0x1020))
        self.assertEqual(struct.unpack_from('<II',x.frame.data,0xbc+8*16),(0x5000,0x5020))
        x.full=True;x.submit(1)
        self.assertEqual(struct.unpack_from('<II',x.frame.data,0xbc+8*16),(0x3000,0x3010))
        self.assertEqual(struct.unpack_from('<II',x.frame.data,0xbc+4*16),(0x5000,0x5020))
        self.assertEqual(struct.unpack_from('<II',x.frame.data,0xbc+10*16),(0x4000,0x4020))

    def test_readback_uses_seed_reference_and_nv12_display(self):
        x=self.session();x.references[1].write(bytes(range(48)));x.outputs[0].write(bytes(range(24)))
        self.assertEqual(x.readback({'slot':0,'seed':True,'current':1,'times':{}}),bytes(range(48)))
        self.assertEqual(x.readback({'slot':0,'seed':False,'current':1,'times':{}}),bytes(range(24)))

    def test_unprepared_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'not been prepared'):self.session().submit(0)

    def test_nv12_preparation_keeps_original_plane_bytes(self):
        x=self.session(1);data=bytes(range(24));x.prepare(0,data=data)
        self.assertEqual(x.inputs[0].writes,[(0,data)])
        self.assertEqual(x.pending_inputs,[None,None])

    def test_layout_rejects_mixed_stride_and_missing_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for name,seed in [('cfg.bin',False),('seed-cfg.bin',True)]:
                b=bytearray(5984)
                for n in (0,4,8,10):
                    fmt=20 if n==8 and not seed else 19;stride=3840 if fmt==20 else 7680
                    struct.pack_into('<3I',b,n*96,fmt,3840,2176)
                    struct.pack_into('<4I',b,n*96+64,stride,2176,stride,1088)
                (p/name).write_bytes(b)
            with self.assertRaises(FileNotFoundError):s.precision_layout(p)
            (p/'precision.json').write_text(json.dumps({'mode':'p010-internal','scratch_bytes':6<<20}))
            self.assertEqual(s.precision_layout(p),(2,6<<20))
            b=bytearray((p/'cfg.bin').read_bytes());struct.pack_into('<I',b,64,3840);(p/'cfg.bin').write_bytes(b)
            with self.assertRaisesRegex(ValueError,'plane layout'):s.precision_layout(p)

if __name__=='__main__':unittest.main()
