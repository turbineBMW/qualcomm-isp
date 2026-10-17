#!/usr/bin/python3 -I
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Talk to the ICP firmware through /sys/kernel/debug/camss-icp (run as root)."""
import struct, sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

D = '/sys/kernel/debug/camss-icp'
VERBOSE = True   # per-command prints; long-running sessions switch this off
CREATE_HANDLE, CREATE_HANDLE_ACK = 0x1010008, 0x1020008
ASYNC_DIRECT, ASYNC_DIRECT_ACK = 0x101000a, 0x102000a
ASYNC_INDIRECT, ASYNC_INDIRECT_ACK = 0x101000e, 0x102000e
HANDLE_BPS, HANDLE_IPE_RT, HANDLE_IPE_NRT = 1, 2, 3
OP = dict(BPS_CONFIG_IO=1, BPS_FRAME=2, BPS_ABORT=3, BPS_DESTROY=4,
          IPE_CONFIG_IO=5, IPE_FRAME=6, IPE_ABORT=7, IPE_DESTROY=8)


def send(pkt, want):
    with open(f'{D}/cmd', 'wb', buffering=0) as f:
        f.write(struct.pack('<I', want) + pkt)
    with open(f'{D}/cmd', 'rb') as f:
        return f.read()


def alloc(size):
    with open(f'{D}/alloc', 'w') as f:
        f.write(str(size))
    with open(f'{D}/alloc') as f:
        idx, iova, sz = f.read().split('\n')[-2].split()
    return int(idx), int(iova, 16), int(sz)


def buf_write(idx, data, off=0):
    with open(f'{D}/buf{idx}', 'r+b', buffering=0) as f:
        f.seek(off)
        f.write(data)


def buf_read(idx, n, off=0):
    with open(f'{D}/buf{idx}', 'rb') as f:
        f.seek(off)
        return f.read(n)


def create_handle(htype, ud=0x1234):
    pkt = struct.pack('<IIIQQ', 28, CREATE_HANDLE, htype, ud, 0)
    r = send(pkt, CREATE_HANDLE_ACK)
    size, typ, err, handle, u1, u2 = struct.unpack('<IIIIQQ', r[:32])
    if VERBOSE:
        print(f'create_handle type {htype}: err {err} handle 0x{handle:08x}')
    return err, handle


def async_cmd(handle, opcode, indirect=None, direct=b'', ud=0x5678):
    if indirect is not None:
        pkt = struct.pack('<IIIQQIII', 40, ASYNC_INDIRECT, opcode, ud, 0, 1, handle, indirect)
        want = ASYNC_INDIRECT_ACK
    else:
        body = struct.pack('<IQQII', opcode, ud, 0, 1, handle) + direct
        pkt = struct.pack('<II', 8 + len(body), ASYNC_DIRECT) + body
        want = ASYNC_DIRECT_ACK
    r = send(pkt, want)
    size, typ, op, u1, u2, err = struct.unpack('<IIIQQI', r[:32])
    if VERBOSE or err:
        print(f'async op {opcode}: ack size {size} err {err} data {r[32:size].hex()}')
    return err, r[32:size]


def set_debug(level):
    # SYS_SET_PROPERTY has no reply. want=0 returns as soon as the packet is
    # queued (the lab wait loop compares against the cleared message type), so
    # session setup no longer spends 5 s per property on a timeout.
    pkt = struct.pack('<6I', 24, 0x10003, 1, 1, level, 1)
    send(pkt, 0)
    print(f'debug level 0x{level:x} sent')


def set_prop(words, label):
    # SYS_SET_PROPERTY has no reply: queue it and return immediately (want=0).
    pkt = struct.pack(f'<{3 + len(words)}I', 12 + 4 * len(words), 0x10003, 1, *words)
    send(pkt, 0)
    print(f'property {label} sent')


def sys_setup():
    # as the downstream driver does on open: UBWC configuration for BPS and IPE
    # (fetch 0x7083, write 0x1620f from the device tree), IPE/BPS power collapse off
    set_prop([0x9, 0x7083, 0x1620f, 0x7083, 0x1620f], 'UBWC_CONFIG_EX')
    set_prop([0x5, 0, 0x10001], 'IPEBPS_PC off')


def mem_map(handle, regions, unmap=False):
    # HFI_IPEBPS_CMD_OPCODE_MEM_MAP (0xe): let the firmware map the command-buffer
    # regions it reads with its own CPU (frame data, IQ settings, CDM programs)
    body = struct.pack('<QI', 0, len(regions)) + b''.join(struct.pack('<II', a, n) for a, n in regions)
    return async_cmd(handle, 0xf if unmap else 0xe, direct=body)


def image_file_specs(w, h):
    """Describe image files and size the shared PD10 allocation before hardware access."""
    import pd10
    specs = []
    pool_bytes = 0
    for mode, var in (('in', 'ICP_IMG_IN'), ('out', 'ICP_IMG_OUT')):
        for tok in filter(None, os.environ.get(var, '').split(',')):
            img, path = tok.split(':', 1)
            img = int(img)
            ds = {1: 4, 5: 4, 11: 4, 2: 16, 6: 16, 12: 16}.get(img)
            size = w * h * 3 // 2
            if ds:
                _, _, stride, slice_h = pd10.dims(w, h, ds)
                size = stride * slice_h
                pool_bytes = ((pool_bytes + 0xfff) & ~0xfff) + size
            if mode == 'in' and Path(path).stat().st_size != size:
                raise ValueError(f'Image {img}: {path} must contain exactly {size} bytes')
            specs.append((mode, img, path, size, ds))
    return specs, max(2 << 20, (pool_bytes + (1 << 20) - 1) & ~((1 << 20) - 1))


def frame(cfg_path, in_path, out_path, w=1920, h=1080, override=None,
          striping_path=None, striping_size=None):
    import subprocess, importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = '/tmp/icplab'
    os.makedirs(tmp, exist_ok=True)
    # NRT builds the Snapshot topology, which does not attach the display output
    # (no display write client, output stays grey). The RT (preview/video) handle
    # builds TopologyBuilderCreateRealTime_Main_Tf4_Tf1, which does.
    rt = os.environ.get('ICP_IPE_RT', '0') == '1'
    nv12 = w * h * 3 // 2
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        raise ValueError('NV12 dimensions must be positive and even')
    if Path(in_path).stat().st_size != nv12:
        raise ValueError(f'Main input must contain exactly {nv12} bytes for {w}x{h}')
    specs, pd_pool_size = image_file_specs(w, h)
    bi, in_iova, _ = alloc(nv12)
    bo, out_iova, _ = alloc(nv12)
    bs, scr_iova, scr_size = alloc(int(os.environ.get("ICP_SCRATCH", str(4 << 20)), 0))   # 5-pass streams need > 1 MB (fw pass offsets up to ~0x130000 for 1080p)
    bc, cdm_iova, cdm_size = alloc(1 << 20)
    bq, iq_iova, _ = alloc(65536)
    bp, pa_iova, _ = alloc(65536)   # cdmProgramArrayBase buffer: arrays, programs, LUT data, NPS pass programs from +0x8000
    bf, fr_iova, _ = alloc(65536)
    bk, cfg_iova, _ = alloc(65536)
    LEVEL = {1: 1, 2: 2, 3: 3, 5: 1, 6: 2, 7: 3, 11: 1, 12: 2, 13: 3}
    def dsdim(n, lv):
        for _ in range(lv):
            n = -(-n // 4)
        return n
    extra = []
    for tok in filter(None, os.environ.get('ICP_EXTRA_IMAGES', '').split(',')):
        img = int(tok); lv = LEVEL.get(img, 0)
        ew, eh = dsdim(w, lv), dsdim(h, lv)
        idx, iova, _ = alloc(max(4096, ew * eh * 3 // 2))
        buf_write(idx, b'\x80' * (ew * eh * 3 // 2))
        extra.append((img, idx, iova, ew, eh))
    # full-size image buffers with content: ICP_IMG_IN="img:file[,img:file]" (written before the run),
    # ICP_IMG_OUT="img:file[,...]" (grey before, saved after). Used for the TF reference input (4) / output (10).
    imgfiles = []; small = []
    for mode, img, path, size, ds in specs:
        if ds:   # Share page-aligned PD10 planes; 4K needs more than the old fixed 2 MB pool.
            if not small: small.extend(alloc(pd_pool_size)[:2] + (0,))
            sidx, siova, soff = small; soff = (soff + 0xfff) & ~0xfff
            assert soff + size <= pd_pool_size
            idx, iova, off = sidx, siova + soff, soff; small[2] = soff + size
        else:
            idx, iova, _ = alloc(size); off = 0
        buf_write(idx, Path(path).read_bytes() if mode == 'in' else b'\x80' * size, off)
        extra.append((img, idx, iova, w, h)); imgfiles.append((mode, img, idx, path, size, off))
    os.environ['ICP_EXTRA_BUFS'] = ','.join(f'{i}:{hex(a)}:{ew}:{eh}' for i, _, a, ew, eh in extra)
    buf_write(bi, open(in_path, 'rb').read()[:nv12])
    buf_write(bo, b'\x80' * nv12)          # grey: shows whether the IPE wrote at all
    buf_write(bk, open(cfg_path, 'rb').read())
    # serialized striping-library output at its own IOVA: either the file given on
    # the command line, or auto-generated for this w/h by ipestriping.py (default).
    if striping_path:
        blob = open(striping_path, 'rb').read()
    else:
        spec = importlib.util.spec_from_file_location('ipestriping', f'{here}/ipestriping.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # one output line/pixel-range entry per configured output image:
        # 8=display->18, 9=video->19, 10=ref full->21+22 (luma+chroma windows)
        IMG2OUT = {8: (18,), 9: (19,), 10: (21, 22)}
        entries = {18}
        for tok in filter(None, os.environ.get('ICP_EXTRA_IMAGES', '').split(',')):
            entries.update(IMG2OUT.get(int(tok), ()))
        # ICP_OUT_ENTRIES: extra lineRange/pixelRange output entries (probe for the
        # finalOutputName numbering the write-client gate reads: IpeOutput 0/1,
        # IPE_IO_IMAGES 8/9, or striping-coordinate 18/19; input-range entries are
        # safe to fill -- probe mode already wrote 1..8/17 with tiny windows).
        for tok in filter(None, os.environ.get('ICP_OUT_ENTRIES', '').split(',')):
            entries.add(int(tok))
        blob = mod.build(w, h, out_entries=tuple(sorted(entries)))
    bso, striping_iova, _ = alloc(max(len(blob), striping_size or 0))
    buf_write(bso, blob)
    striping_size = len(blob)
    print(f'striping output {len(blob)} bytes at iova 0x{striping_iova:08x}')
    subprocess.run(['python3', '-I', f'{here}/ipeframe2.py', tmp, str(w), str(h),
                    hex(in_iova), hex(out_iova), hex(scr_iova), str(scr_size),
                    hex(cdm_iova), str(cdm_size), hex(iq_iova), hex(pa_iova),
                    hex(striping_iova), str(striping_size),
                    ('rt' if rt else os.environ.get('ICP_IPE_OPMODE', 'nrt'))], check=True)
    iq_file = os.environ.get('ICP_IQ_FILE') or f'{tmp}/iq.bin'   # ICP_IQ_FILE: use this IpeIQSettings blob (same one given to slrun)
    buf_write(bq, open(iq_file, 'rb').read())
    print(f'iq settings from {iq_file}')
    if override == 'zero':
        pass                                   # buffer stays all zero
    elif override == 'min':
        buf_write(bf, struct.pack('<27I', *([0] * 24 + [1, 1, 1])))  # requestId, opMode NRT, 1 frame set
    else:
        buf_write(bf, open(f'{tmp}/frame.bin', 'rb').read())
    buf_write(bp, open(f'{tmp}/pa.bin', 'rb').read())
    if os.environ.get('ICP_SYS_SETUP', '1') == '1':
        sys_setup()
    err, hd = create_handle(HANDLE_IPE_RT if rt else HANDLE_IPE_NRT)
    if err or not hd:
        raise RuntimeError(f'IPE handle creation failed: err={err}, handle={hd}')
    stage = 'CONFIG_IO'
    err, data = async_cmd(hd, OP['IPE_CONFIG_IO'], indirect=cfg_iova)
    if not err and os.environ.get('ICP_MEM_MAP', '1') == '1':
        regions = [(fr_iova, 65536), (iq_iova, 65536), (pa_iova, 65536), (cdm_iova, cdm_size)]
        if striping_iova:
            regions.append((striping_iova, striping_size))
        stage = 'MEM_MAP'
        err, _ = mem_map(hd, regions)
    if not err:
        stage = 'FRAME_PROCESS'
        err, data = async_cmd(hd, OP['IPE_FRAME'], indirect=fr_iova)
    out = buf_read(bo, nv12)
    open(out_path, 'wb').write(out)
    changed = sum(1 for i in range(0, nv12, 997) if out[i] != 0x80)
    print(f'output saved to {out_path}; sampled bytes changed from grey: {changed}/{nv12 // 997 + 1}')
    cdmdump = buf_read(bc, 1 << 20)
    def in_cdm(iova):
        # 4-aligned search: an unaligned byte search matches crossing artifacts, e.g. the
        # fetch pair (value 0x438, reg 0x20f0) contains the byte pattern of 0x20f00000 at +2
        b = struct.pack('<I', iova)
        return any(cdmdump[o:o+4] == b for o in range(0, len(cdmdump) - 4, 4))
    print(f'display out iova 0x{out_iova:08x} in CDM program: {in_cdm(out_iova)}')
    for img, idx, iova, ew, eh in extra:
        n = min(ew * eh * 3 // 2, 4096 * 64)
        d = buf_read(idx, n)
        ch = sum(1 for i in range(0, n, 97) if d[i] != 0x80)
        print(f'extra image {img} ({ew}x{eh}) at 0x{iova:08x}: changed {ch}/{n // 97 + 1}, in CDM program: {in_cdm(iova)}')
    for mode, img, idx, path, size, off in imgfiles:
        if mode == 'out':
            open(path, 'wb').write(buf_read(idx, size, off)); print(f'image {img} saved to {path}')
    for name, idx, sz in (('cdm', bc, 1 << 20), ('scratch', bs, 1 << 20)):
        data = buf_read(idx, sz)
        used = next((i + 4 for i in range(sz - 4, -1, -4) if data[i:i + 4] != b'\0\0\0\0'), 0)
        if used:
            open(f'{out_path}.{name}.bin', 'wb').write(data[:min(used + 64, sz)])
            print(f'{name} buffer: first {used + 64} bytes non-zero, saved to {out_path}.{name}.bin')
        else:
            print(f'{name} buffer: all zero')
    destroy_err, _ = async_cmd(hd, OP['IPE_DESTROY'], direct=struct.pack('<Q', 0))
    # Preserve the output/debug buffers and release the handle before reporting
    # failure. A completed command can still carry CAMERAICP_EBADPARM, and its
    # untouched grey output must not be mistaken for a successful image.
    if err:
        raise RuntimeError(f'IPE {stage} failed with HFI error {err}; diagnostic buffers retained')
    if destroy_err:
        raise RuntimeError(f'IPE DESTROY failed with HFI error {destroy_err}')


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'frame':
        kw = {}
        if os.environ.get('ICP_W'):
            kw['w'] = int(os.environ['ICP_W'])
        if os.environ.get('ICP_H'):
            kw['h'] = int(os.environ['ICP_H'])
        if len(sys.argv) > 5 and sys.argv[5].endswith('.bin'):
            kw['striping_path'] = sys.argv[5]
        if len(sys.argv) > 6:
            kw['striping_size'] = int(sys.argv[6], 0)
        frame(sys.argv[2], sys.argv[3], sys.argv[4],
              override=sys.argv[5] if len(sys.argv) > 5 and not sys.argv[5].endswith('.bin') else None,
              **kw)
    if cmd == 'savefw':
        data = open(f'{D}/lastfw', 'rb').read()
        open(sys.argv[2], 'wb').write(data)
        print(f'saved {len(data)} bytes of the previous firmware image to {sys.argv[2]}')
    if cmd == 'debug':
        set_debug(int(sys.argv[2], 0))
    if cmd == 'ipe-config-io':
        cfg = open(sys.argv[2], 'rb').read()
        idx, iova, _ = alloc(65536)
        buf_write(idx, cfg)
        print(f'config io data {len(cfg)} bytes at iova 0x{iova:08x} (buf{idx})')
        err, h = create_handle(HANDLE_IPE_NRT)
        if err or not h:
            sys.exit(1)
        err, data = async_cmd(h, OP['IPE_CONFIG_IO'], indirect=iova)
        if len(data) >= 8:
            scratch, cdm = struct.unpack('<II', data[:8])
            print(f'IpeConfigIoResult? scratchMemSize {scratch} cdmBufferSize {cdm}')
        async_cmd(h, OP['IPE_DESTROY'], direct=struct.pack('<Q', 0))
