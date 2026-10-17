#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""PD10 (Qualcomm IPE/IFE DS format): one plane, every 2x2 YUV420 block (4 Y, 1 Cb, 1 Cr, 10 bit each) packed into
8 bytes (60 bits + 4 dummy). Buffer line = 2 image lines; stride = align(align(ceil(w/2)*8, 192), 256) bytes,
slice height = align(ceil(h/2), 32) (kona camximageformatutils.cpp). Bit order inside the 64-bit word is ORDER
(default guess: Y0 Y1 Y2 Y3 Cb Cr from the LSB; verify against the IPE's own DS4 reference output).
usage: pd10.py enc <in.nv12> <w> <h> <factor> <out.pd10>   (box-downscale by factor, 8->10 bit by <<2)
       pd10.py dec <in.pd10> <w> <h> <out.nv12>             (w,h = PD10 image dims; 10->8 bit by >>2)
       pd10.py dims <w> <h> <factor>"""
import sys, numpy as np
# PD10 block (64 bits), measured from the IPE's own DS4 reference output (RESULTS.md 2026-10-17): bytes 0..5 hold the
# 8 MSBs of Y[a], Y[b], Y[c], Y[d], Cb, Cr; bits 48..59 hold the 2 LSBs of the six samples (2 bits each, sample k at
# 48 + 2k); bits 60..63 pad. Luma byte order: PD10_YORDER="00,10,01,11" (default, column-major as decoded) or "00,01,10,11".
YORDER = __import__('os').environ.get('PD10_YORDER', '00,10,01,11').split(',')
def align(v, a): return -(-v // a) * a
def dims(w, h, f):
    dw, dh = align(w, f) // f, align(h, f) // f
    dw += dw & 1; dh += dh & 1
    stride = align(align(-(-dw // 2) * 8, 192), 256); slice_h = align(-(-dh // 2), 32)
    return dw, dh, stride, slice_h
def downscale(nv12, w, h, f):
    y = nv12[:w*h].reshape(h, w).astype(np.float32); uv = nv12[w*h:w*h*3//2].reshape(h//2, w//2, 2).astype(np.float32)
    dw, dh, _, _ = dims(w, h, f)
    yp = np.pad(y, ((0, dh*f - h), (0, dw*f - w)), mode='edge'); Y = yp.reshape(dh, f, dw, f).mean((1, 3))
    uvp = np.pad(uv, ((0, dh*f//2 - h//2), (0, dw*f//2 - w//2), (0, 0)), mode='edge'); UV = uvp.reshape(dh//2, f, dw//2, f, 2).mean((1, 3))
    return np.clip(np.round(Y * 4), 0, 1023).astype(np.uint64), np.clip(np.round(UV * 4), 0, 1023).astype(np.uint64), dw, dh
def _ysplit(Y):
    q = {'00': Y[0::2, 0::2], '01': Y[0::2, 1::2], '10': Y[1::2, 0::2], '11': Y[1::2, 1::2]}
    return [q[k] for k in YORDER]
def encode(Y, UV, dw, dh, stride, slice_h):
    buf = np.zeros((slice_h, stride // 8), np.uint64)
    comps = _ysplit(Y.astype(np.uint64)) + [UV[..., 0].astype(np.uint64), UV[..., 1].astype(np.uint64)]
    word = np.zeros((dh//2, dw//2), np.uint64)
    for k, c in enumerate(comps):
        c = c & np.uint64(1023)
        word |= (c >> np.uint64(2)) << np.uint64(8 * k)
        word |= (c & np.uint64(3)) << np.uint64(48 + 2 * k)
    buf[:dh//2, :dw//2] = word
    return buf.tobytes()
def decode(data, dw, dh):
    _, _, stride, slice_h = dims(dw, dh, 1) if False else (None, None, align(align(-(-dw // 2) * 8, 192), 256), align(-(-dh // 2), 32))
    buf = np.frombuffer(data, np.uint64)[:slice_h * (stride // 8)].reshape(slice_h, stride // 8)[:dh//2, :dw//2]
    comps = [((((buf >> np.uint64(8 * k)) & np.uint64(255)) << np.uint64(2)) | ((buf >> np.uint64(48 + 2 * k)) & np.uint64(3))).astype(np.uint16) for k in range(6)]
    Y = np.zeros((dh, dw), np.uint16)
    for k, key in enumerate(YORDER):
        r, c = int(key[0]), int(key[1]); Y[r::2, c::2] = comps[k]
    UV = np.stack(comps[4:6], -1)
    out = np.concatenate([(Y >> 2).astype(np.uint8).ravel(), (UV >> 2).astype(np.uint8).ravel()])
    return out.tobytes()
if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'dims': print(dims(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])))
    elif cmd == 'enc':
        w, h, f = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]); nv12 = np.fromfile(sys.argv[2], np.uint8)
        Y, UV, dw, dh = downscale(nv12, w, h, f); _, _, stride, slice_h = dims(w, h, f)
        open(sys.argv[6], 'wb').write(encode(Y, UV, dw, dh, stride, slice_h)); print(f'{dw}x{dh} PD10 stride {stride} slice {slice_h} -> {sys.argv[6]} ({stride*slice_h} B)')
    elif cmd == 'dec':
        dw, dh = int(sys.argv[3]), int(sys.argv[4]); open(sys.argv[5], 'wb').write(decode(open(sys.argv[2], 'rb').read(), dw, dh)); print(f'decoded {dw}x{dh} -> {sys.argv[5]}')
