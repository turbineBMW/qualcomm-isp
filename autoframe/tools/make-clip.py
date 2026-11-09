#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Make an NV12 test clip for ispautoframe from recorded NV12 frames.

Input frames are full-sensor captures (default 3840x2640); the clip is the
publisher's 3840x2160 window taken from them. With --pan the window slides
up and down the taller source over the clip (a sine), so a still face moves
vertically and the controller has something to follow. Frames repeat in turn.

  make-clip.py out.nv12 --frames 150 --pan frame1.nv12 [frame2.nv12 ...]
  gst-launch-1.0 filesrc location=out.nv12 ! rawvideoparse format=nv12 width=3840 height=2160 framerate=30/1 ! \
    ispautoframe draw-boxes=true ! capsfilter caps=video/x-raw,width=1920,height=1080 ! videoconvert ! pngenc ! multifilesink location=out-%03d.png
"""
import argparse
import math
from pathlib import Path
import sys

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('output', type=Path)
    parser.add_argument('frames', type=Path, nargs='+')
    parser.add_argument('--source', default='3840x2640', help='size of the input frames')
    parser.add_argument('--size', default='3840x2160', help='size of the clip frames')
    parser.add_argument('--frames', dest='count', type=int, default=150)
    parser.add_argument('--pan', action='store_true', help='slide the window over the source')
    args = parser.parse_args()
    sw, sh = map(int, args.source.split('x'))
    cw, ch = map(int, args.size.split('x'))
    if cw > sw or ch > sh or cw % 2 or ch % 2:
        sys.exit('clip must fit inside the source, even sizes')
    sources = []
    for path in args.frames:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size != sw * sh * 3 // 2:
            sys.exit(f'{path}: {data.size} bytes, expected {sw * sh * 3 // 2} for {sw}x{sh} NV12')
        sources.append((data[:sw * sh].reshape(sh, sw), data[sw * sh:].reshape(sh // 2, sw)))
    with args.output.open('wb') as out:
        for i in range(args.count):
            y, uv = sources[i % len(sources)]
            fx = fy = 0.5
            if args.pan:
                fy = 0.5 - 0.5 * math.cos(2 * math.pi * i / args.count)
                fx = 0.5 + 0.5 * math.sin(2 * math.pi * i / args.count)
            x = int(round((sw - cw) * fx / 2)) * 2
            top = int(round((sh - ch) * fy / 2)) * 2
            out.write(np.ascontiguousarray(y[top:top + ch, x:x + cw]).tobytes())
            out.write(np.ascontiguousarray(uv[top // 2:(top + ch) // 2, x:x + cw]).tobytes())
    print(f'{args.output}: {args.count} frames of {cw}x{ch} NV12 from {len(sources)} source frame(s)'
          + (' with pan' if args.pan else ''))


if __name__ == '__main__':
    main()
