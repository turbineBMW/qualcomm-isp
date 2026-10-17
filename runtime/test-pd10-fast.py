#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Check accelerated native-4K PD10 against the existing NumPy reference."""
import ctypes
import json
from pathlib import Path
import sys
import time
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pd10

fn = ctypes.CDLL(str(HERE/'pd10-fast.so')).pd10_reduce
fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint,
               ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
fn.restype = ctypes.c_int
w,h = 3840,2176
size = w*h*3//2
rng = np.random.default_rng(681)
cases = dict(random=rng.integers(0,256,size,dtype=np.uint8).tobytes(),
             black=bytes(size), white=bytes([255])*size)
results = []
for name,data in cases.items():
    src = ctypes.create_string_buffer(data,len(data))
    for factor in (4,16):
        dw,dh,stride,sh = pd10.dims(w,h,factor)
        dst = ctypes.create_string_buffer(stride*sh)
        start = time.perf_counter()
        assert fn(src,len(data),w,h,factor,dst,len(dst)) == 0
        elapsed = (time.perf_counter()-start)*1000
        y,uv,_,_ = pd10.downscale(np.frombuffer(data,np.uint8),w,h,factor)
        assert dst.raw == pd10.encode(y,uv,dw,dh,stride,sh), (name,factor)
        results.append(dict(case=name,factor=factor,byte_identical=True,reduction_ms=elapsed))
assert fn(src,len(data)-1,w,h,4,dst,len(dst)) == -1
print(json.dumps(results, indent=2))
print(f'PASS: {len(results)} byte-identical reductions; invalid input rejected')
