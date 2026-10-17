#!/usr/bin/env python3 -I
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Build the IPE FRAME_PROCESS payload for firmware API 2.52 (Surface CICP.FW.5.2-00008).

The camx kona headers (2.0.88) that ipeframe.c uses have a DIFFERENT
IpeFrameProcessData layout: 2.52 adds cdmProgramArray* words up to +0xa4,
requestId at 0xa8, a timestamp u64 at 0xac, frameOpMode at 0xb4,
numFrameSetsInBatch at 0xb8 (the mbn frame handler reads words 0, 4, 36..164
and 184), and frameSets start at 0xbc with ImageDescriptor 96 B (ImageInfo one
u32 longer than 2.0.88). IpeIQSettings is 0x1834 B in 2.52 (the firmware
cache-invalidates exactly that many bytes at iqSettingsAddr).

usage: ipeframe2.py <outdir> <w> <h> <in_iova> <out_iova> <scratch_iova>
                    <scratch_size> <cdm_iova> <cdm_size> <iq_iova> <progarr_iova>
                    <striping_iova> <striping_size>
writes <outdir>/{iq.bin,frame.bin,pa.bin}
"""
import os
from pathlib import Path
import struct
import sys

IQ_SIZE = 0x1834          # IpeIQSettings, API 2.52 (verified: fw invalidates 0x1834)
FRAMESET_SIZE = 0x648     # 1608 = buffers[62]*16 + 8 + GeoIcaParameters(608)
N_IMAGES = 62
IN_FULL, OUT_DISPLAY, SCRATCH = 0, 8, 16

# IpeIQSettings 2.52 offsets (DWARF of CAMERA_ICP_AAAAAA.elf, _IpeIQSettings)
IQ_VIDEO_LUMA_CLAMP = 0x0E00       # IpeClampParameters {luma{min,max}, chroma{min,max}}
IQ_VIDEO_CLAMP = 0x0E00
IQ_DISPLAY_CLAMP = 0x0E10


def build_iq(w, h):
    """All IQ modules off; output clamps open (clampMin 0 / clampMax 255)."""
    iq = bytearray(IQ_SIZE)
    for base in (IQ_VIDEO_CLAMP, IQ_DISPLAY_CLAMP):
        struct.pack_into('<IIII', iq, base, 0, 255, 0, 255)   # luma min/max, chroma min/max
    return bytes(iq)


def build_frame(w, h, in_iova, out_iova, scr_iova, scr_size, cdm_iova, cdm_size,
                iq_iova, pa_iova, striping_iova, striping_size, opmode=1):
    """IpeFrameProcessData 2.52 wire layout, one frame set, NRT, host striping."""
    payload_size = 0xBC + FRAMESET_SIZE
    p = bytearray(payload_size)
    u32 = lambda off, v: struct.pack_into('<I', p, off, v)

    u32(0x00, striping_iova)          # stripingLibOutAddr (serialized striping output)
    u32(0x04, iq_iova)                # iqSettingsAddr
    u32(0x08, 0)                      # ubwcStatsBufferAddress
    u32(0x0C, 0)                      # ubwcStatsBufferSize
    u32(0x10, cdm_iova)               # cdmBufferAddress
    u32(0x14, cdm_size)               # cdmBufferSize
    u32(0x18, int(os.environ.get('ICP_CORES', '1'), 0))  # maxNumOfCoresToUse
    u32(0x1C, 0)                      # targetCompletionTimeInNs (QoS off)
    u32(0x20, pa_iova)                # cdmProgramArrayBase (the empty array)
    if any(os.environ.get(k) for k in ('ICP_PRELTM_CMDS', 'ICP_POSTLTM_CMDS',
                                       'ICP_PRELTM_LUT_PROGRAMS', 'ICP_LUT_PROGRAMS')):
        u32(0x24, pre_array_offset()) # cdmProgramArrayPreLtmAddr: offset from cdmProgramArrayBase
        u32(0x28, 0x40)               # cdmProgramArrayPostLtmAddr
    # NPS pass programs: ICP_ANR_CMDS / ICP_TF_CMDS = comma list of per-pass program files (full, DC4, DC16, DC64);
    # ICP_ANR_FULL_CMDS / ICP_TF_FULL_CMDS = single full-pass file. Arrays live at +0x200.. (above the post-LTM array,
    # which grows 24 B per LUT program): ANR passes at 0x200/0x220/0x240/0x260, TF passes at 0x280/0x2a0/0x2c0/0x2e0.
    for i, path in enumerate(nps_list('ANR')):
        if path: u32(0x2C + 4 * i, 0x200 + 0x20 * i)   # cdmProgramArrayAnrFullPass/Dc4/Dc16/Dc64Addr
    for i, path in enumerate(nps_list('TF')):
        if path: u32(0x3C + 4 * i, (0x400 + 0x80 * i) if os.environ.get('ICP_DSX_IN_TF') else (0x280 + 0x20 * i))   # cdmProgramArrayTfFullPass/Dc4/Dc16/Dc64Addr
    if os.environ.get('ICP_DSX_CMDS'):
        u32(0x4C, 0x300)              # cdmProgramArrayDsxDc4Addr: DSX (full pass TF out -> DS4 reference)
    # 0x24..0xA4: all cdmProgramArray* offsets stay 0 (no LUT/ICA/ANR/TF/DSX programs)
    u32(0xA8, 1)                      # requestId
    struct.pack_into('<Q', p, 0xAC, 0)  # timestamp
    u32(0xB4, opmode)                 # frameOpMode: REAL_TIME_STREAM=0 / NON_REAL_TIME_STREAM=1
    u32(0xB8, 1)                      # numFrameSetsInBatch

    # frameSets[0].buffers[IPE_IO_IMAGES]: FrameBuffers = bufferPtr[2] + metadataBufferPtr[2]
    fs = 0xBC

    def buf(image, plane, iova):
        struct.pack_into('<I', p, fs + image * 16 + plane * 4, iova)

    buf(IN_FULL, 0, in_iova)          # main input luma
    buf(IN_FULL, 1, in_iova + w * h)  # main input chroma (NV12)
    # display output bound at OUT_DISPLAY (8, IPE_IO_IMAGES) plus any indices in
    # ICP_OUT_AT (probe: the fw may index the output buffer by the IpeOutput enum,
    # where DISPLAY = 1 -- IpeOutputToBufferPtr does buffers[finalOutputName]).
    out_at = {OUT_DISPLAY}
    for tok in filter(None, os.environ.get('ICP_OUT_AT', '').split(',')):
        out_at.add(int(tok))
    for img in sorted(out_at):
        buf(img, 0, out_iova)         # display output luma
        buf(img, 1, out_iova + w * h)
    buf(SCRATCH, 0, scr_iova)         # IPE scratch buffer
    for tok in filter(None, os.environ.get('ICP_EXTRA_BUFS', '').split(',')):
        img, iova, ew, eh = (int(x, 0) for x in tok.split(':'))
        buf(img, 0, iova)
        buf(img, 1, iova + ew * eh)
    # frameSets[0].cdmProgramArrayIca1/Ica2 + icaGeoParameters: zero
    return bytes(p)


def nps_list(kind):
    """Per-pass NPS program files for ANR or TF: ICP_<kind>_CMDS="full,dc4,dc16,dc64" (empty entries allowed) or the
    single-file ICP_<kind>_FULL_CMDS. Returns a 4-list of paths or None."""
    lst = os.environ.get(f'ICP_{kind}_CMDS')
    if lst: out = [x or None for x in lst.split(',')]
    else: out = [os.environ.get(f'ICP_{kind}_FULL_CMDS') or None]
    return (out + [None] * 4)[:4]


def _regcont(cmds):
    return cmds


def pre_array_offset():
    # A pre-LTM LUT needs a second descriptor: +0x20 would overlap the post
    # array at +0x40. Reserve +0x600..+0x700 for the expanded pre-LTM array.
    return 0x600 if os.environ.get('ICP_PRELTM_LUT_PROGRAMS') else 0x20


def build_pa(pa_iova=0):
    """cdmProgramArrayBase buffer.
    ICP_PRELTM_CMDS / ICP_POSTLTM_CMDS = files with raw CDM commands (REG_CONT/REG_RANDOM).
    Layout: pre-LTM CDMProgramArray at +0x20 (program bytes at +0x100), post-LTM array at
    +0x40 (program bytes at +0x800). Each array = {allocator, numPrograms, CdmProgram[]};
    CdmProgram (24 B) = {cdmBaseAndLength u64 {BASE:32, RESERVED:12, LEN:20 = bytes-1},
    programType (72 = GENERIC), uID, hasSingleReg, bufferAllocatedInternally}.
    Offset 0 in IpeFrameProcessData means 'no array', so both arrays sit above 0."""
    pre = os.environ.get('ICP_PRELTM_CMDS'); post = os.environ.get('ICP_POSTLTM_CMDS')
    anr = nps_list('ANR'); tfp = nps_list('TF'); dsx = os.environ.get('ICP_DSX_CMDS')
    if not pre and not post and not any(anr) and not any(tfp) and not dsx and not os.environ.get('ICP_LUT_PROGRAMS') and not os.environ.get('ICP_PRELTM_LUT_PROGRAMS'):
        return struct.pack('<II', 0, 0)
    pa = bytearray(0x10000)
    # NPS pass programs (fw-owned types: 66 = IPE_ANR_CYLPF_PROGRAM, 67 = IPE_TF_PROGRAM; kona FillNPSCDMProgram,
    # hasSingleReg 0). Registers = kona Titan480 NPS block + 0x1000. Program bytes from +0x8000 in 2 KB slots.
    slot = 0x8000
    tf_base, tf_step = ((0x400, 0x80) if os.environ.get('ICP_DSX_IN_TF') else (0x280, 0x20))
    for arr_base, step, ptype, paths in ((0x200, 0x20, 66, anr), (tf_base, tf_step, 67, tfp)):
        for i, path in enumerate(paths):
            if not path: continue
            cmds = open(path, 'rb').read(); assert len(cmds) <= 0x800
            struct.pack_into('<II', pa, arr_base + step * i, 0, 1)
            struct.pack_into('<QIIII', pa, arr_base + step * i + 8, (pa_iova + slot) | ((len(cmds) - 1) << 44), ptype, 0, 0, 0)
            pa[slot:slot + len(cmds)] = cmds; slot += 0x800
    if dsx:
        # ICP_DSX_CMDS="regs.bin:luts.bin" (luts = 4 x 0x400 from slrun dsx: luma h, luma v, chroma h, chroma v).
        # Array at +0x300 like the DLL's node (IPEDSX101Titan680::WriteLUTInDMI): type 68 IPE_DSX_PROGRAM (REG_CONT ranges
        # 0x8a68..), type 23 IPE_DSX_Y_LUT (DMI cfg 0x8808 sel 1,2 x 0x300 = 96 x u64), type 24 IPE_DSX_C_LUT (DMI cfg 0x8a08
        # sel 1,2 x 0x180 = 48 x u64). Program bytes at +0xc000/+0xc800/+0xcc00, LUT data at +0xd000.
        regpath, lutpath = dsx.split(':'); cmds = open(regpath, 'rb').read(); luts = open(lutpath, 'rb').read()
        pa[0xc000:0xc000 + len(cmds)] = cmds
        ydmi = b''.join(struct.pack('<III', (0x300 - 1) | (1 << 24), pa_iova + 0xd000 + k * 0x400, 0x8808 | ((k + 1) << 24)) for k in range(2))
        cdmi = b''.join(struct.pack('<III', (0x180 - 1) | (1 << 24), pa_iova + 0xd000 + (2 + k) * 0x400, 0x8a08 | ((k + 1) << 24)) for k in range(2))
        pa[0xc800:0xc800 + len(ydmi)] = ydmi; pa[0xcc00:0xcc00 + len(cdmi)] = cdmi; pa[0xd000:0xd000 + len(luts)] = luts
        progs = [struct.pack('<QIIII', (pa_iova + 0xc000) | ((len(cmds) - 1) << 44), 68, 0, 0, 0),
                 struct.pack('<QIIII', (pa_iova + 0xc800) | ((len(ydmi) - 1) << 44), 23, 0, 0, 0),
                 struct.pack('<QIIII', (pa_iova + 0xcc00) | ((len(cdmi) - 1) << 44), 24, 0, 0, 0)]
        if os.environ.get('ICP_DSX_IN_TF'):
            n = struct.unpack_from('<I', pa, 0x404)[0]
            for q, pr in enumerate(progs): pa[0x408 + 24 * (n + q):0x408 + 24 * (n + q + 1)] = pr
            struct.pack_into('<I', pa, 0x404, n + len(progs))
        else:
            struct.pack_into('<II', pa, 0x300, 0, len(progs))
            for q, pr in enumerate(progs): pa[0x308 + 24 * q:0x308 + 24 * (q + 1)] = pr
    for arr_off, prog_off, path in ((pre_array_offset(), 0x100, pre), (0x40, 0x800, post)):
        if not path:
            struct.pack_into('<II', pa, arr_off, 0, 0)
            continue
        cmds = Path(path).read_bytes()
        assert 0 < len(cmds) <= (0x100 if prog_off == 0x100 else 0x400), 'PPS register program overlaps the next slot'
        struct.pack_into('<II', pa, arr_off, 0, 1)
        struct.pack_into('<QIIII', pa, arr_off + 8, (pa_iova + prog_off) | ((len(cmds) - 1) << 44), 72, 0, 0, 0)
        pa[prog_off:prog_off + len(cmds)] = cmds
    # LUT programs (DMI): ICP_LUT_PROGRAMS="type:dmireg:sel,sel,..:file[;type:...]". Each DMI command
    # (cdmpayload _cdm_dmi) = {LEN | 1<<24, SRC (IOVA of the table), dmireg | sel<<24}; one table per sel,
    # tables packed back to back in <file>. DMI command lists go at +0xc00.., tables at +0x1000...
    # LTM LUTs belong to the pre-LTM array. Both arrays share the existing
    # DMI command/data storage; their descriptors must remain separate.
    dmi_off, data_off = 0xc00, 0x1000
    for arr_off, envkey in ((pre_array_offset(), 'ICP_PRELTM_LUT_PROGRAMS'), (0x40, 'ICP_LUT_PROGRAMS')):
        specs = [x for x in os.environ.get(envkey, '').split(';') if x]
        n = struct.unpack_from('<I', pa, arr_off + 4)[0]
        for spec in specs:
            ptype, dmireg, sels, path = spec.split(':'); ptype, dmireg = int(ptype, 0), int(dmireg, 0)
            data = Path(path).read_bytes(); items = sels.split(',')
            sizes = [int(x.split('@')[1], 0) if '@' in x else None for x in items]; sels = [int(x.split('@')[0], 0) for x in items]
            per = len(data) // len(sels); sizes = [sz if sz is not None else per for sz in sizes]
            assert all(sz > 0 for sz in sizes) and sum(sizes) == len(data), 'LUT sizes must exactly cover the data'
            assert data_off + len(data) <= 0x8000, 'LUT data overlaps NPS programs'
            assert dmi_off + 12 * len(sels) <= 0x1000, 'DMI commands overlap LUT data'
            assert arr_off + 8 + 24 * (n + 1) <= (0x700 if arr_off == 0x600 else 0x100), 'LUT descriptors overlap another slot'
            cmds = b''; pos = 0
            for sel, sz in zip(sels, sizes):
                src = pa_iova + data_off + pos; pos += sz
                cmds += struct.pack('<III', (sz - int(os.environ.get('ICP_DMI_LEN_MINUS', '1'))) | (1 << 24), src, dmireg | (sel << 24))
            pa[data_off:data_off + len(data)] = data; data_off += (len(data) + 0xff) & ~0xff
            pa[dmi_off:dmi_off + len(cmds)] = cmds
            struct.pack_into('<QIIII', pa, arr_off + 8 + 24 * n, (pa_iova + dmi_off) | ((len(cmds) - 1) << 44), ptype, int(os.environ.get('ICP_LUT_UID', '0'), 0), 0, 0)
            n += 1; dmi_off += (len(cmds) + 0x3f) & ~0x3f
        struct.pack_into('<I', pa, arr_off + 4, n)
    return bytes(pa)


if __name__ == '__main__':
    outdir = sys.argv[1]
    w, h = int(sys.argv[2]), int(sys.argv[3])
    in_iova, out_iova = int(sys.argv[4], 0), int(sys.argv[5], 0)
    scr_iova, scr_size = int(sys.argv[6], 0), int(sys.argv[7])
    cdm_iova, cdm_size = int(sys.argv[8], 0), int(sys.argv[9])
    iq_iova, pa_iova = int(sys.argv[10], 0), int(sys.argv[11], 0)
    striping_iova, striping_size = int(sys.argv[12], 0), int(sys.argv[13])
    mode = sys.argv[14] if len(sys.argv) > 14 else 'nrt'
    opmode = {'rt': 0, 'nrt': 1, 'nr2': 2}.get(mode, 1)  # fw IpeJobGenerator: opmode 2 => expects ONE job
    os.makedirs(outdir, exist_ok=True)
    open(f'{outdir}/iq.bin', 'wb').write(build_iq(w, h))
    open(f'{outdir}/frame.bin', 'wb').write(
        build_frame(w, h, in_iova, out_iova, scr_iova, scr_size, cdm_iova, cdm_size,
                    iq_iova, pa_iova, striping_iova, striping_size, opmode))
    open(f'{outdir}/pa.bin', 'wb').write(build_pa(pa_iova))
    print(f'iq {IQ_SIZE:#x} B, frame {0xBC + FRAMESET_SIZE} B, pa 8 B -> {outdir} ({mode})')
