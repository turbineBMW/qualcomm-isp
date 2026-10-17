#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Pad NV12 with repeated bottom rows, or remove only trailing rows; no rescaling.

Usage: nv12-height.py input output width input-height output-height
Used to work around the current IPE DC16 bottom-edge issue at 2160 lines:
pad to 2176 before processing, retain padded temporal references, then export
the first 2160 luma rows and 1080 chroma rows of the display result.
"""
from pathlib import Path
import sys


def change_height(data, width, height, output_height):
    if any(x <= 0 or x % 2 for x in (width, height, output_height)):
        raise ValueError('NV12 dimensions must be positive and even')
    if len(data) != width * height * 3 // 2:
        raise ValueError('NV12 input length does not match its dimensions')
    y, uv = data[:width * height], data[width * height:]
    if output_height <= height:
        return y[:width * output_height] + uv[:width * output_height // 2]
    extra = output_height - height
    return y + y[-width:] * extra + uv + uv[-width:] * (extra // 2)


if __name__ == '__main__':
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    src, dst = map(Path, sys.argv[1:3])
    w, h, out_h = map(int, sys.argv[3:])
    dst.write_bytes(change_height(src.read_bytes(), w, h, out_h))
