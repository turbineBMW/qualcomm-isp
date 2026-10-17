#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Build only the private GStreamer bridge against installed development APIs."""
import argparse
from pathlib import Path
import shlex
import subprocess

p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('output',type=Path)
p.add_argument('--version',default='1.6.9-icp-recovery');a=p.parse_args()
a.output.mkdir(exist_ok=True)
include=a.output/'gst'
if not include.exists(): include.symlink_to(a.source.resolve(),target_is_directory=True)
(a.output/'config.h').write_text(f'''#define PACKAGE "pipewire"
#define PACKAGE_VERSION "{a.version}"
#define HAVE_GSTREAMER_DEVICE_PROVIDER 1
#define HAVE_GSTREAMER_DMA_DRM 1
#define HAVE_GSTREAMER_SHM_ALLOCATOR 1
''')
flags=shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs',
    'libpipewire-0.3','gstreamer-1.0','gstreamer-base-1.0','gstreamer-video-1.0',
    'gstreamer-audio-1.0','gstreamer-net-1.0','gstreamer-allocators-1.0'],text=True))
subprocess.run(['cc','-O2','-g','-Wall','-Wextra','-Wno-unused-parameter','-fPIC','-shared',
    '-Wl,-z,defs','-I'+str(a.output),*map(str,sorted(a.source.glob('*.c'))),
    '-o',str(a.output/'libgstpipewire.so'),*flags,'-lm'],check=True)
