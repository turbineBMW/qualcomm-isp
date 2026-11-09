#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Check accelerated native-4K PD10 and P010 widening against NumPy references."""
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

# P010 widening: every code preserved as code << 8, rows padded like the input.
widen = ctypes.CDLL(str(HERE/'pd10-fast.so')).p010_widen_planes
widen.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
widen.restype = ctypes.c_int
for valid in (h, 2160, 2):
    y = rng.integers(0,256,w*valid,dtype=np.uint8)
    uv = rng.integers(0,256,w*valid//2,dtype=np.uint8)
    out = ctypes.create_string_buffer(size*2)
    start = time.perf_counter()
    assert widen(y.ctypes.data, uv.ctypes.data, w, valid, h, out, len(out)) == 0
    elapsed = (time.perf_counter()-start)*1000
    yy = np.concatenate([y.reshape(valid,w), np.repeat(y[-w:][None], h-valid, 0)])
    uu = np.concatenate([uv.reshape(valid//2,w), np.repeat(uv[-w:][None], h//2-valid//2, 0)])
    reference = (np.concatenate([yy.ravel(), uu.ravel()]).astype('<u2') << 8).tobytes()
    assert out.raw == reference, valid
    results.append(dict(case=f'widen-valid{valid}', byte_identical=True, widen_ms=elapsed))
assert widen(y.ctypes.data, uv.ctypes.data, w, 2161, h, out, len(out)) == -1
assert widen(y.ctypes.data, uv.ctypes.data, w, h, h, out, len(out)-2) == -1
print(json.dumps(results, indent=2))
print(f'PASS: {len(results)} byte-identical reductions and widenings; invalid input rejected')
