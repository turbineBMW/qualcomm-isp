#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Build host CDM register programs (REG_CONT) and IpeIQSettings enables for IPE PPS modules
on this chip (register map = kona titan480_ipe.h + 0xa00 for the PPS blocks; pipeline 10-bit,
internal RGB channel order G, B, R as in CamX's CC13 packing).
usage: ipeprog.py <iq_in> <iq_out> <pre_cmds_out> <post_cmds_out> [cst] [ce] [cc=<gain>]"""
import os, struct, sys
IQ_EN = {'cst': 0xc44, 'ce': 0xcb0, 'cc': 0xca4, 'sat': 0xca4, 'glut': 0xca8, 'scurve': 0xca8}
def regcont(off, regs): return struct.pack('<II', (3 << 24) | len(regs), off) + b''.join(struct.pack('<I', r & 0xffffffff) for r in regs)
def s13(v): return int(round(v)) & 0x1fff
def s12(v): return int(round(v)) & 0xfff
def s11(v): return int(round(v)) & 0x7ff
def s15(v): return int(round(v)) & 0x7fff
CENTER = 512   # chroma mid-level in the PPS pipeline units (10-bit assumed; adjust if the round trip shows a shift)
def cst_yuv2rgb(center=None):
    """CST 1.2 (Titan680 layout, DLL IPECST12Titan680::PackIQRegisterSetting, instance 2 = PPS at 0x14468), 18 regs:
    per channel {m0|m1<<16 (13b Q10), m2, o<<21 (11b out offset), s<<21 (11b in offset), cmin<<22, cmax<<22 (10b)}.
    in = (Y, Cb, Cr); out channels G, B, R; chroma centre removed through the output offset."""
    c = CENTER if center is None else center
    Q = 1024; ch = [((1.0, -0.344, -0.714), (0.344 + 0.714) * c),   # G
                    ((1.0, 1.772, 0.0), -1.772 * c),                 # B
                    ((1.0, 0.0, 1.402), -1.402 * c)]                 # R
    regs = []
    for (m0, m1, m2), o in ch:
        regs += [s13(m0 * Q) | (s13(m1 * Q) << 16), s13(m2 * Q), s11(o) << 21, 0, 0, 1023 << 22]   # 10-bit variant: o 11b@21, s 11b@21, clamp 10b@22 (measured)
    return regcont(int(os.environ.get("CST_BASE", "0x11068"), 0), regs)   # instance 0 = 0x11068, 2 = 0x14468, 3 = 0x14668
def ce_rgb2yuv():
    """CE 1.2.1 (Titan680, 9 regs at 0x11668), semantics measured on the patch chart (lab RESULTS.md 2026-10-15):
    inputs arrive as (ch0, ch1, ch2) = (G, B, R); all Q8, 10-bit pipeline.
      Y  = V0*R + V1*G + V2*B + K            r1 = V0 | V1<<16, r2 = V2, r0 = K<<21
      Cb = a(sign B-G) * ((B-G) + b*(R-G)) + KCB   r3 = a_pos | a_neg<<16, r4 = b (Q8 signed), r7 = KCB<<19
      Cr = c(sign R-G) * ((R-G) + d*(B-G)) + KCR   r5 = c_pos | c_neg<<16, r6 = d (Q8 signed), r8 = KCR<<19
    BT.601 full range: Cb = 0.5(B-G) - 0.169(R-G), Cr = 0.5(R-G) - 0.081(B-G)."""
    v0, v1, v2 = round(0.299 * 256), round(0.587 * 256), round(0.114 * 256)
    a, b = 128, round(-0.169 / 0.5 * 256); c, d = 128, round(-0.081 / 0.5 * 256)
    regs = [0 << 21, s12(v0) | (s12(v1) << 16), s12(v2), s12(a) | (s12(a) << 16), s12(b) | (s12(b) << 16), s12(c) | (s12(c) << 16), s12(d) | (s12(d) << 16), 512 << 19, 512 << 19]
    return regcont(0x11668, regs)


def cc_matrix(M, k=(0, 0, 0), q=0):
    """CC 1.3.1 with a full RGB matrix M[out][in] (rows/cols in R,G,B order) and offsets k (10-bit).
    Register columns are per INPUT channel G, B, R; rows within a column are outputs G, B, R."""
    sc = 1 << (7 + q)
    def c(o, i): return int(round(M[o][i] * sc)) & 0xfff
    R, G, B = 0, 1, 2
    regs = [c(G, G) | (c(B, G) << 16), c(R, G),        # input G column: G->G, B<-G ; R<-G
            c(G, B) | (c(B, B) << 16), c(R, B),        # input B column
            c(G, R) | (c(B, R) << 16), c(R, R),        # input R column
            (int(k[1]) & 0x1fff) << 19, (int(k[2]) & 0x1fff) << 19, (int(k[0]) & 0x1fff) << 19, q]
    return regcont(0x15768, regs)
def cc_saturation(s):
    """Saturation s around BT.601 luma: M = (1-s)*[w;w;w] + s*I."""
    w = (0.299, 0.587, 0.114)
    M = [[(1 - s) * w[j] + (s if i == j else 0) for j in range(3)] for i in range(3)]
    return cc_matrix(M)
def cc_gain(g):
    """CC 1.3.1 (Titan680 layout, DLL IPECC131Titan680::PackIQRegisterSetting), 10 regs at 0x15768:
    A col (input G) {c0|c3<<16, c6}, B col (input B) {c1|c4<<16, c7}, C col (input R) {c2|c5<<16, c8},
    k0<<19, k1<<19, k2<<19, qfactor (Q7+q). Diagonal: c0 = G->G, c4 = B->B, c8 = R->R."""
    q = round(g * 128)
    return regcont(0x15768, [q, 0, q << 16, 0, 0, q, 0, 0, 0, 0])


def scurve_table(strength, out='glut-lut.bin'):
    """Contrast S-curve around mid grey: x + strength * sin(2*pi*x) / (2*pi) style, 256 entries x 3."""
    import math
    def f(t): return min(1.0, max(0.0, t - strength * math.sin(2 * math.pi * t) / (2 * math.pi)))
    vals = [int(round(1023 * f(i / 255.0))) for i in range(256)] + [1023]
    ent = [(vals[i] & 0x3ff) | (((vals[i + 1] - vals[i]) & 0x3ff) << 10) for i in range(256)]
    open(out, 'wb').write(b''.join(struct.pack('<I', e) for e in ent) * 3); return out
def glut_table(gamma, out='glut-lut.bin'):
    """Gamma 1.5 LUT (kona gamma15setting): 256 entries per channel, entry = value10 | (delta10 << 10),
    delta = next - this (10-bit two's complement). Same table for the three channels (sel 1, 2, 3)."""
    vals = [int(round(1023 * (i / 255.0) ** (1.0 / gamma))) for i in range(256)] + [1023]
    ent = [(vals[i] & 0x3ff) | (((vals[i + 1] - vals[i]) & 0x3ff) << 10) for i in range(256)]
    tbl = b''.join(struct.pack('<I', e) for e in ent)
    open(out, 'wb').write(tbl * 3); return out
if __name__ == '__main__':
    iq = bytearray(open(sys.argv[1], 'rb').read()); pre = b''; post = b''
    for mod in sys.argv[5:]:
        name, _, arg = mod.partition('=')
        struct.pack_into('<I', iq, IQ_EN[name], 1)
        if name == 'cst': pre += cst_yuv2rgb()
        if name == 'ce': post += ce_rgb2yuv()
        if name == 'cc': post += cc_gain(float(arg or 1.0))
        if name == 'sat': post += cc_saturation(float(arg or 1.0))
        if name == 'scurve': print('LUT table ->', scurve_table(float(arg or 0.5)))
        if name == 'glut': print('LUT table ->', glut_table(float(arg or 1.0)), '(use ICP_LUT_PROGRAMS=18:0x14e08:1,2,3:glut-lut.bin)')
    open(sys.argv[2], 'wb').write(iq); open(sys.argv[3], 'wb').write(pre); open(sys.argv[4], 'wb').write(post)
    print(f'iq -> {sys.argv[2]} ({sys.argv[5:]}), pre {len(pre)} B, post {len(post)} B')
