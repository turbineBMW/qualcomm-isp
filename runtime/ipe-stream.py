#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Persistent, serialized IPE session using precomputed native-4K programs.

Private lab interface only. Own the camera lock and load the matching modules
before constructing a session. No Windows DLL or per-frame DMA allocation is used.

The session has two input/output slots so one frame can be prepared and the
previous output read back while the firmware processes the current frame.
Hardware submission itself stays serialized: one IPE_FRAME is in flight at a
time, and the temporal references still alternate per submission.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import importlib.util
import json
import mmap
import os
from pathlib import Path
import struct
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import icplab
import ipeframe2
import pd10
import numpy as np

SLOTS = 2
# A P010 input is 24 MiB; one pwrite() copies it into the uncached coherent
# buffer at about 2.4 GB/s on one core. The driver copies each call
# independently and without a lock, so disjoint ranges of one write are issued
# concurrently. IPE_WRITE_THREADS=1 restores the single copy for comparison.
WRITE_THREADS = max(1, min(8, int(os.environ.get('IPE_WRITE_THREADS', '4'))))
PARALLEL_WRITE_MIN = 4 << 20
_writers = None


def writers():
    global _writers
    if _writers is None:
        _writers = ThreadPoolExecutor(WRITE_THREADS, 'dma-write')
    return _writers


def precision_layout(programs):
    """Accept the existing NV12 path or an explicitly described P010 interior.

    P010 keeps denoised colors at ten bits until display output. Its seed writes
    directly into a reference buffer. Serialized submissions share one hardware
    input; preparation for the other slot retains its expanded input on the CPU
    and submit() copies it in with several concurrent writes.
    """
    p = Path(programs)
    cfg, seed = ((p / name).read_bytes() for name in ('cfg.bin', 'seed-cfg.bin'))
    if len(cfg) != 5984 or len(seed) != 5984:
        raise ValueError('Unexpected image configuration size')
    if struct.unpack_from('<I', cfg)[0] == 20:
        return 1, 8 << 20
    metadata = json.loads((p / 'precision.json').read_text())
    if metadata != {'mode': 'p010-internal', 'scratch_bytes': 6 << 20}:
        raise ValueError('Unqualified precision configuration')
    for config, seeding in ((cfg, False), (seed, True)):
        for number in (0, 4, 8, 10):
            fmt = 20 if number == 8 and not seeding else 19
            off = number * 96
            if struct.unpack_from('<3I', config, off) != (fmt, 3840, 2176):
                raise ValueError('Unexpected P010 image format or dimensions')
            stride = 3840 if fmt == 20 else 7680
            if struct.unpack_from('<4I', config, off + 64) != (stride, 2176, stride, 1088):
                raise ValueError('Unexpected P010 plane layout')
    return 2, 6 << 20


def expand_nv12(data):
    """Preserve each eight-bit code exactly in MSB-aligned P010 storage."""
    return (np.frombuffer(data, np.uint8).astype('<u2') << 8).tobytes()


@contextmanager
def program_environment(programs, full, overrides=None):
    saved = {k: v for k, v in os.environ.items() if k.startswith('ICP_')}
    for k in saved:
        del os.environ[k]
    p = Path(programs)
    overrides = dict(overrides or {})
    def file(name):
        return str(Path(overrides[name]).resolve() if name in overrides else p / name)
    values = dict(ICP_ANR_CMDS=','.join(str(p / f'nps-anr-p{i}.bin') for i in range(3)))
    if full:
        values.update(ICP_PRELTM_CMDS=file('pre-ltm.bin'), ICP_POSTLTM_CMDS=file('post.bin'),
                      ICP_PRELTM_LUT_PROGRAMS=f'17:0x15508:1@96,2@256,3@256,4@512,5@256,6@64,7@64:{p}/ltm-luts.bin',
                      ICP_LUT_PROGRAMS=f'18:0x14e08:1,2,3:{p}/gamma.bin;20:0x11c08:1@1024,2@1024,3@1024,4@1024,5@1024,6@1024,9@68,10@1024,11@1024:{p}/asf-luts.bin',
                      ICP_TF_CMDS=f'{p}/nps-tf-p0.bin,{p}/nps-tf-p1.bin,',
                      ICP_DSX_CMDS=f'{p}/dsx.bin:{p}/dsx-luts.bin')
    os.environ.update(values)
    try:
        yield
    finally:
        for k in list(os.environ):
            if k.startswith('ICP_'):
                del os.environ[k]
        os.environ.update(saved)


class Buffer:
    def __init__(self, size):
        self.index, self.iova, self.size = icplab.alloc(size)
        self.mapping = None
        self.fd = os.open(f'{icplab.D}/buf{self.index}', os.O_RDWR | os.O_CLOEXEC)

    def write(self, data, offset=0):
        view = memoryview(data).cast('B')
        if offset < 0 or offset + view.nbytes > self.size:
            raise ValueError('DMA write outside buffer')
        if view.nbytes < PARALLEL_WRITE_MIN or WRITE_THREADS < 2:
            return self.write_range(view, offset)
        step = (view.nbytes + WRITE_THREADS - 1) // WRITE_THREADS
        step = (step + 4095) & ~4095
        parts = [(view[start:start + step], offset + start) for start in range(0, view.nbytes, step)]
        # map() re-raises the first failure once every range has finished.
        for _ in writers().map(lambda part: self.write_range(*part), parts):
            pass

    def write_range(self, view, offset):
        while view:
            n = os.pwrite(self.fd, view, offset)
            if n <= 0:
                raise OSError('Short DMA write')
            offset += n
            view = view[n:]

    def read(self, size, offset=0):
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ValueError('DMA read outside buffer')
        data = bytearray(size)
        view = memoryview(data)
        while view:
            n = os.preadv(self.fd, [view], offset)
            if n <= 0:
                raise OSError('Short DMA read')
            view = view[n:]
            offset += n
        return data

    def map(self):
        if self.mapping is None:
            fd = os.open(f'{icplab.D}/map{self.index}', os.O_RDONLY | os.O_CLOEXEC)
            try:
                self.mapping = mmap.mmap(fd, self.size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, trackfd=False)
            finally:
                os.close(fd)
        return self.mapping

    def read_mapped_into(self, destination, size, offset=0, destination_offset=0):
        """Copy mapped DMA bytes into caller-owned storage (bytearray or writable memoryview)."""
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ValueError('DMA read outside buffer')
        view = memoryview(destination)
        if destination_offset < 0 or destination_offset + size > view.nbytes:
            raise ValueError('Destination too small for DMA read')
        if size == 0:
            return
        # memcpy uses the userspace vector implementation on arm64. Read only
        # after the serialized IPE completion; the DMA mapping remains uncached.
        src = np.frombuffer(self.map(), np.uint8, count=size, offset=offset)
        dst = (ctypes.c_uint8 * size).from_buffer(view, destination_offset)
        ctypes.memmove(dst, src.ctypes.data, size)

    def read_mapped(self, size, offset=0):
        data = bytearray(size)
        self.read_mapped_into(data, size, offset)
        return data

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        os.close(self.fd)


class Session:
    def __init__(self, programs, setup=True, striping_capacity=0, readback="mmap", retain_times=0):
        if readback not in ("read", "mmap", "verify"):
            raise ValueError("Invalid readback mode")
        self.readback_mode = readback
        self.p = Path(programs).resolve()
        self.w, self.h = struct.unpack_from('<II', (self.p / 'cfg.bin').read_bytes(), 4)
        if (self.w, self.h) != (3840, 2176):
            raise ValueError('Only the verified padded native-4K configuration is supported')
        self.n = self.w * self.h * 3 // 2
        self.internal_bytes, self.scratch_bytes = precision_layout(self.p)
        self.hd, self.full, self.sequence, self.previous = None, False, 0, 0
        self.buffers = []
        self.times = deque(maxlen=retain_times or None)
        self.failed = False
        self.config_count = 0
        self.lock = threading.RLock()
        self.pending_inputs = [None] * SLOTS
        self.staging = [None] * SLOTS      # reusable CPU copies of the widened P010 inputs
        self.reducer = self.widener = None
        if (HERE / 'pd10-fast.so').exists():
            lib = ctypes.CDLL(str(HERE / 'pd10-fast.so'))
            self.reducer = lib.pd10_reduce_planes
            self.reducer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                     ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_void_p, ctypes.c_size_t]
            self.reducer.restype = ctypes.c_int
            self.widener = lib.p010_widen_planes
            self.widener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                     ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
            self.widener.restype = ctypes.c_int
        try:
            self.inputs = ([self.allocate(self.n * 2)] * SLOTS if self.internal_bytes == 2 else
                           [self.allocate(self.n) for _ in range(SLOTS)])
            self.outputs = [self.allocate(self.n) for _ in range(SLOTS)]
            self.references = [self.allocate(self.n * self.internal_bytes) for _ in range(2)]
            self.scratch = self.allocate(self.scratch_bytes)
            self.cdm = self.allocate(1 << 20)
            self.iq = self.allocate(65536)
            self.pa = self.allocate(65536)
            self.frame = self.allocate(65536)
            self.cfg = self.allocate(65536)
            self.striping = self.allocate(max([striping_capacity] +
                [(self.p / x).stat().st_size for x in ('striping.bin', 'seed-striping.bin')]))
            self.pd_sizes = {f: pd10.dims(self.w, self.h, f)[2] * pd10.dims(self.w, self.h, f)[3] for f in (4, 16)}
            offset = 0
            self.pd_offsets = {}
            keys = [(f'input4:{s}', 4) for s in range(SLOTS)] + [(f'input16:{s}', 16) for s in range(SLOTS)]
            for key, factor in keys + [('ref0', 4), ('ref1', 4)]:
                self.pd_offsets[key] = offset
                offset = (offset + self.pd_sizes[factor] + 4095) & ~4095
            self.pd = self.allocate(offset)
            self.frames = {}
            self.program_arrays = {}
            self.overrides = {}
            self.load_programs(self.p)
            if setup:
                icplab.sys_setup()
            self.configure(False)
        except BaseException:
            self.close()
            raise

    # Single-slot names kept for existing tools and tests.
    @property
    def input(self):
        return self.inputs[0]

    @property
    def output(self):
        return self.outputs[0]

    def allocate(self, size):
        b = Buffer(size)
        self.buffers.append(b)
        return b

    def load_programs(self, path, overrides=None):
        """Validate/relocate both phases before changing the active program.

        `overrides` maps a program file name (currently post.bin or
        pre-ltm.bin) to a file outside the entry directory, so the colour
        program can follow the colour temperature while everything else
        follows gain and tone.
        """
        p = Path(path).resolve()
        if precision_layout(p) != (self.internal_bytes, self.scratch_bytes):
            raise ValueError('Precision change requires a new session')
        overrides = {k: Path(v).resolve() for k, v in (overrides or {}).items()}
        for name, file in overrides.items():
            if name not in ('post.bin', 'pre-ltm.bin') or not file.is_file() or file.stat().st_size > 0x800:
                raise ValueError(f'Invalid program override: {name}')
        frames, arrays = {}, {}
        for full in (False,True):
            names = ('cfg.bin','iq-ltm.bin','striping.bin') if full else ('seed-cfg.bin','seed-iq.bin','seed-striping.bin')
            for name,buffer in zip(names,(self.cfg,self.iq,self.striping)):
                if (p/name).stat().st_size > buffer.size:
                    raise ValueError(f'Program exceeds allocated DMA buffer: {name}')
            if struct.unpack_from('<II',(p/names[0]).read_bytes(),4) != (self.w,self.h):
                raise ValueError('Program geometry change requires a new session')
            with program_environment(p,full,overrides):
                # Slot 0 addresses are templates; submit() rewrites images 0 and 8 per slot.
                frames[full] = ipeframe2.build_frame(self.w,self.h,self.inputs[0].iova,self.outputs[0].iova,
                    self.scratch.iova,self.scratch.size,self.cdm.iova,self.cdm.size,self.iq.iova,
                    self.pa.iova,self.striping.iova,(p/names[2]).stat().st_size)
                arrays[full] = ipeframe2.build_pa(self.pa.iova)
        with self.lock:
            self.p,self.frames,self.program_arrays = p,frames,arrays
            self.overrides = overrides

    def select_programs(self,path,keep_history=False,overrides=None):
        """Switch programs. By default the next frame is a seed pass; with
        keep_history and an established full configuration, the existing
        temporal references stay valid and the next frame is a full pass."""
        if self.failed:
            raise RuntimeError('IPE session failed; restart required')
        wanted = {k: Path(v).resolve() for k, v in (overrides or {}).items()}
        if Path(path).resolve() == self.p and wanted == getattr(self, 'overrides', {}):
            return False
        with self.lock:
            self.load_programs(path, overrides)
            self.configure(bool(keep_history and self.full))
        return True

    def command(self, opcode, **kwargs):
        err, data = icplab.async_cmd(self.hd, opcode, **kwargs)
        if err:
            self.failed = True
            raise RuntimeError(f'IPE command {opcode} failed with HFI error {err}')
        return data

    def destroy(self):
        with self.lock:
            if self.hd is not None:
                hd, self.hd = self.hd, None
                err, _ = icplab.async_cmd(hd, icplab.OP['IPE_DESTROY'], direct=struct.pack('<Q', 0))
                if err:
                    self.failed = True
                    raise RuntimeError(f'IPE destroy failed: {err}')

    def configure(self, full):
        with self.lock:
            self.destroy()
            self.full = full
            for buffer, name in [(self.cfg, 'cfg.bin' if full else 'seed-cfg.bin'),
                                 (self.iq, 'iq-ltm.bin' if full else 'seed-iq.bin'),
                                 (self.striping, 'striping.bin' if full else 'seed-striping.bin')]:
                buffer.write((self.p / name).read_bytes())
            self.pa.write(self.program_arrays[full])
            err, hd = icplab.create_handle(icplab.HANDLE_IPE_NRT)
            if err or not hd:
                raise RuntimeError(f'IPE create failed: {err}')
            self.hd = hd
            self.command(icplab.OP['IPE_CONFIG_IO'], indirect=self.cfg.iova)
            err, _ = icplab.mem_map(self.hd, [(b.iova, b.size) for b in (self.frame, self.iq, self.pa, self.cdm, self.striping)])
            if err:
                raise RuntimeError(f'IPE map failed: {err}')
            self.config_count += 1

    def reduce(self, y, uv, valid_height, factors=(4, 16)):
        """PD10 DS4/DS16 of an NV12 frame given as separate plane buffers.

        Rows from valid_height to the padded height repeat the last valid row,
        exactly like the padded IPE input. Returns {factor: bytearray}.
        """
        y, uv = memoryview(y), memoryview(uv)
        if y.nbytes != self.w * valid_height or uv.nbytes != self.w * valid_height // 2:
            raise ValueError('Plane sizes do not match the declared height')
        out = {f: bytearray(self.pd_sizes[f]) for f in factors}
        if self.reducer:
            args = []
            for f in (4, 16):
                buf = out.get(f)
                args += [(ctypes.c_uint8 * len(buf)).from_buffer(buf) if buf is not None else None,
                         len(buf) if buf is not None else 0]
            # ctypes needs a pointer to the plane bytes. Both read-only mmap views
            # and bytes export the buffer protocol; np.frombuffer gives the address
            # without copying, and the reducer never writes its inputs.
            ya, ua = (np.frombuffer(v, np.uint8).ctypes.data for v in (y, uv))
            if self.reducer(ya, ua, self.w, valid_height, self.h, *args):
                raise RuntimeError('PD10 reduction failed')
            return out
        data = np.concatenate([np.frombuffer(y, np.uint8), np.frombuffer(uv, np.uint8)])
        if valid_height != self.h:
            from importlib import util
            spec = util.spec_from_file_location('nv12_height', HERE / 'nv12-height.py')
            mod = util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            data = np.frombuffer(mod.change_height(data.tobytes(), self.w, valid_height, self.h), np.uint8)
        for f in factors:
            yy, uvv, dw, dh = pd10.downscale(data, self.w, self.h, f)
            _, _, stride, sh = pd10.dims(self.w, self.h, f)
            out[f][:] = pd10.encode(yy, uvv, dw, dh, stride, sh)
        return out

    def downscale(self, data, factor):
        if len(data) != self.n:
            raise ValueError('Invalid padded NV12 frame length')
        view = memoryview(data)
        return self.reduce(view[:self.w * self.h], view[self.w * self.h:], self.h, (factor,))[factor]

    def widen(self, y, uv, valid, slot):
        """Stage the padded, MSB-aligned P010 input for `slot` in reusable CPU memory.

        The two slots share one hardware input (the second does not fit the
        dedicated ICP pool next to the references), so the expanded frame
        waits here until submit() owns the input. The staging buffer of a
        slot is only rewritten after that slot's submission returned.
        """
        staging = self.staging[slot]
        if staging is None:
            staging = self.staging[slot] = bytearray(self.n * 2)
        plane = self.w * self.h
        if self.widener:
            ya, ua = (np.frombuffer(v, np.uint8).ctypes.data for v in (y, uv))
            out = (ctypes.c_uint8 * len(staging)).from_buffer(staging)
            if self.widener(ya, ua, self.w, valid, self.h, out, len(staging)):
                raise RuntimeError('P010 widening failed')
            return staging
        padded = np.frombuffer(staging, '<u2')
        yy = padded[:plane].reshape(self.h, self.w)
        uu = padded[plane:].reshape(self.h // 2, self.w)
        yy[:valid] = np.frombuffer(y, np.uint8).reshape(valid, self.w)
        uu[:valid // 2] = np.frombuffer(uv, np.uint8).reshape(valid // 2, self.w)
        if valid < self.h:
            yy[valid:] = yy[valid - 1]
            uu[valid // 2:] = uu[valid // 2 - 1]
        padded <<= 8
        return staging

    def prepare(self, slot, data=None, planes=None, ds4=None, ds16=None):
        """Write one input slot: the padded frame and its PD10 reductions.

        Either `data` (a padded 3840x2176 NV12 frame) or `planes` (the Y and UV
        planes of a native 3840x2160 frame, padded on the way in) is required.
        Safe to run while another slot is being processed by the firmware.
        """
        if self.failed:
            raise RuntimeError('IPE session failed; restart required')
        if slot not in range(SLOTS) or (data is None) == (planes is None):
            raise ValueError('prepare needs a valid slot and exactly one frame source')
        for factor, packed in ((4, ds4), (16, ds16)):
            if packed is not None and len(packed) != self.pd_sizes[factor]:
                raise ValueError(f'Invalid DS{factor} buffer length')
        start = time.monotonic()
        plane = self.w * self.h
        if data is not None:
            if len(data) != self.n:
                raise ValueError('Invalid padded NV12 frame length')
            view = memoryview(data)
            y, uv, valid = view[:plane], view[plane:], self.h
            if self.internal_bytes == 2:
                self.pending_inputs[slot] = self.widen(y, uv, valid, slot)
            else:
                self.inputs[slot].write(view)
        else:
            y, uv = (memoryview(p) for p in planes)
            valid = y.nbytes // self.w
            if (valid % 2 or valid > self.h or y.nbytes != self.w * valid or
                    uv.nbytes != self.w * valid // 2):
                raise ValueError('Plane sizes do not describe an even-height native frame')
            pad = self.h - valid
            target = self.inputs[slot]
            if self.internal_bytes == 2:
                self.pending_inputs[slot] = self.widen(y, uv, valid, slot)
            else:
                target.write(y, 0)
                if pad:
                    target.write(bytes(y[-self.w:]) * pad, self.w * valid)
                target.write(uv, plane)
                if pad:
                    target.write(bytes(uv[-self.w:]) * (pad // 2), plane + self.w * valid // 2)
        input_done = time.monotonic()
        need = tuple(f for f, packed in ((4, ds4), (16, ds16)) if packed is None)
        reduced = self.reduce(y, uv, valid, need) if need else {}
        self.pd.write(ds4 if ds4 is not None else reduced[4], self.pd_offsets[f'input4:{slot}'])
        self.pd.write(ds16 if ds16 is not None else reduced[16], self.pd_offsets[f'input16:{slot}'])
        done = time.monotonic()
        return dict(input_write_ms=(input_done-start)*1000, reduce_ms=(done-input_done)*1000,
                    prepare_ms=(done-start)*1000)

    def submit(self, slot, prepared=None):
        """Submit one prepared slot. Returns a token for readback/complete."""
        if slot not in range(SLOTS):
            raise ValueError('Invalid slot')
        with self.lock:
            if self.failed:
                raise RuntimeError('IPE session failed; restart required')
            start = time.monotonic()
            if self.internal_bytes == 2:
                payload = self.pending_inputs[slot]
                if payload is None:
                    raise ValueError('P010 input slot has not been prepared')
                # The previous IPE_FRAME has returned before this write. The
                # other preparation thread never touches the shared DMA input;
                # the staging buffer stays allocated for this slot's next frame.
                self.inputs[slot].write(payload)
                self.pending_inputs[slot] = None
            frame = bytearray(self.frames[self.full])
            struct.pack_into('<I', frame, 0xa8, self.sequence + 1)
            struct.pack_into('<Q', frame, 0xac, time.monotonic_ns())

            def image(number, iova, chroma=None):
                struct.pack_into('<II', frame, 0xbc + number * 16, iova, iova if chroma is None else chroma)

            plane = self.w * self.h
            internal_plane = plane * self.internal_bytes
            current = 1 - self.previous
            image(ipeframe2.IN_FULL, self.inputs[slot].iova, self.inputs[slot].iova + internal_plane)
            if not self.full and self.internal_bytes == 2:
                image(ipeframe2.OUT_DISPLAY, self.references[current].iova,
                      self.references[current].iova + internal_plane)
            else:
                image(ipeframe2.OUT_DISPLAY, self.outputs[slot].iova, self.outputs[slot].iova + plane)
            image(1, self.pd.iova + self.pd_offsets[f'input4:{slot}'])
            image(2, self.pd.iova + self.pd_offsets[f'input16:{slot}'])
            if self.full:
                image(4, self.references[self.previous].iova, self.references[self.previous].iova + internal_plane)
                image(5, self.pd.iova + self.pd_offsets[f'ref{self.previous}'])
                image(10, self.references[current].iova, self.references[current].iova + internal_plane)
                image(11, self.pd.iova + self.pd_offsets[f'ref{current}'])
            self.frame.write(frame)
            submit = time.monotonic()
            seeded = not self.full
            # Reference rotation and the sequence advance with the submission so
            # the next frame can be submitted before this output is read back.
            self.previous = current
            self.sequence += 1
            token = dict(slot=slot, sequence=self.sequence, seed=seeded, current=current,
                         times=dict(prepared or {}, prepare_ms=(prepared or {}).get('prepare_ms', 0) + (submit-start)*1000))
            try:
                self.command(icplab.OP['IPE_FRAME'], indirect=self.frame.iova)
            except BaseException:
                self.failed = True
                raise
            token['times']['firmware_ms'] = (time.monotonic()-submit)*1000
            token['completed_ns'] = time.monotonic_ns()
            return token

    def readback(self, token, height=None, destination=None):
        """Read the token's output; optionally only the first `height` rows.

        Returns NV12 for display, or P010 for a ten-bit seed. A failed
        read poisons the session: the hardware advanced but CPU state did not.
        """
        slot = token['slot']
        rows = self.h if height is None else height
        if rows % 2 or not 0 < rows <= self.h:
            raise ValueError('Invalid readback height')
        seed_p010 = token['seed'] and self.internal_bytes == 2
        stride = self.w * (2 if seed_p010 else 1)
        plane = stride * self.h
        luma_size = stride * rows
        size = luma_size * 3 // 2
        if destination is None:
            destination = bytearray(size)
        elif memoryview(destination).nbytes != size:
            raise ValueError('Destination size does not match the readback')
        start = time.monotonic()
        try:
            out = self.references[token['current']] if seed_p010 else self.outputs[slot]
            if self.readback_mode == 'read':
                view = memoryview(destination)
                view[:luma_size] = out.read(luma_size, 0)
                view[luma_size:] = out.read(luma_size // 2, plane)
            else:
                out.read_mapped_into(destination, luma_size, 0, 0)
                out.read_mapped_into(destination, luma_size // 2, plane, luma_size)
            read_done = time.monotonic()
            verify_ms = 0
            if self.readback_mode == 'verify':
                check = bytearray(size)
                view = memoryview(check)
                view[:luma_size] = out.read(luma_size, 0)
                view[luma_size:] = out.read(luma_size // 2, plane)
                if destination != check:
                    raise RuntimeError('Mapped DMA read differs from debugfs read')
                verify_ms = (time.monotonic()-read_done)*1000
        except BaseException:
            self.failed = True
            raise
        token['times'].update(readback_ms=(read_done-start)*1000, verify_ms=verify_ms)
        token['read'] = rows
        return destination

    def complete(self, token, result=None):
        """Finish a submission: seed references after a seed pass, record timing."""
        if token.get('done'):
            raise RuntimeError('Token already completed')
        start = time.monotonic()
        reference_done = start
        if token['seed']:
            if self.failed:
                raise RuntimeError('IPE session failed; restart required')
            if result is None or len(result) != self.n * self.internal_bytes:
                result = self.readback(token)
            with self.lock:
                current = token['current']
                view = memoryview(result)
                plane = self.w * self.h
                if self.internal_bytes == 2:
                    # The seed already resides in this reference. Preserve its
                    # fractional codes when constructing the PD10 reference.
                    samples = np.frombuffer(view, '<u2').astype(np.float32) / 256
                    yy, uv, dw, dh = pd10.downscale(samples, self.w, self.h, 4)
                    _, _, stride, sh = pd10.dims(self.w, self.h, 4)
                    reduced = pd10.encode(yy, uv, dw, dh, stride, sh)
                else:
                    self.references[current].write(view)
                    reduced = self.reduce(view[:plane], view[plane:], self.h, (4,))[4]
                self.pd.write(reduced, self.pd_offsets[f'ref{current}'])
                reference_done = time.monotonic()
                self.configure(True)
        t = token['times']
        t.update(reference_ms=(reference_done-start)*1000,
                 configure_ms=(time.monotonic()-reference_done)*1000 if token['seed'] else 0)
        for key in ('input_write_ms', 'reduce_ms', 'readback_ms', 'verify_ms'):
            t.setdefault(key, 0)
        t['total_ms'] = sum(t[k] for k in ('prepare_ms', 'firmware_ms', 'readback_ms', 'verify_ms',
                                            'reference_ms', 'configure_ms'))
        token['done'] = True
        self.times.append(dict(sequence=token['sequence'], seed=token['seed'], slot=token['slot'], **t))
        return token

    def process(self, data, ds4=None, ds16=None):
        """Serialized convenience path: prepare, submit, read and complete one frame."""
        if self.failed:
            raise RuntimeError('IPE session failed; restart required')
        if len(data) != self.n:
            raise ValueError('Invalid padded NV12 frame length')
        for factor, packed in ((4, ds4), (16, ds16)):
            if packed is not None and len(packed) != self.pd_sizes[factor]:
                raise ValueError(f'Invalid DS{factor} buffer length')
        slot = self.sequence % SLOTS
        prepared = self.prepare(slot, data=data, ds4=ds4, ds16=ds16)
        token = self.submit(slot, prepared)
        result = self.readback(token)
        self.complete(token, result)
        return result

    def reset_history(self):
        self.configure(False)

    def close(self):
        try:
            self.destroy()
        finally:
            for b in self.buffers:
                b.close()
            self.buffers.clear()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--programs', type=Path, required=True)
    p.add_argument('--replay', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--frames', type=int, default=16)
    p.add_argument('--readback', choices=('read', 'mmap', 'verify'), default='mmap')
    a = p.parse_args()
    if not 2 <= a.frames <= 1000:
        raise ValueError('Replay count must be 2..1000')
    a.out.mkdir(exist_ok=False, parents=True)
    frames = [(a.replay / f'frame-cam0-stream0-{i:06d}.nv12').read_bytes() for i in (58, 59)]
    packed = [[(a.replay / f'{label}-ds{f}.pd10').read_bytes() for f in (4, 16)] for label in ('A', 'B')]
    session = Session(a.programs, readback=a.readback)
    try:
        for i in range(a.frames):
            # Alternate the saved pair to exercise changing input and references.
            result = session.process(frames[i % 2], *packed[i % 2])
            if i < 2 or i == a.frames - 1:
                (a.out / f'out-{i:04d}.nv12').write_bytes(result)
        report = dict(readback=a.readback, frames=session.sequence, configure_calls=session.config_count,
                      allocations=len(session.buffers), timings=list(session.times),
                      programs=str(a.programs.resolve()), input=str(a.replay.resolve()))
        if a.readback == 'verify':
            spec = importlib.util.spec_from_file_location('mapping_checks', HERE/'check-ipe-mapping.py')
            checks = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(checks)
            report['mapping_checks'] = [checks.check(b) for b in session.outputs]
        (a.out / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    finally:
        session.close()


if __name__ == '__main__':
    main()
