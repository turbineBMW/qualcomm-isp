#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Create the runtime source archive from an allowlist; never include local assets."""
import hashlib
import gzip
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
destination = root/'packaging/runtime/qualcomm-isp-runtime.tar.gz'
destination.parent.mkdir(exist_ok=True)
paths = []
for directory in ('runtime', 'profiles', 'systemd', 'vendor/pipewire-gst'):
    for path in sorted((root/directory).iterdir()):
        if path.is_file() and (path.suffix in ('.py', '.c', '.h', '.json', '.socket', '.service') or path.name == 'COPYING'):
            paths.append(path)
paths.extend(root/name for name in ('tools/build-pipewire.py', 'packaging/config.json', 'docs/PROVENANCE.md', 'docs/FIRMWARE-EXTRACTOR.md', 'LICENSE', 'LICENSES/MIT.txt'))
with destination.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
    for path in sorted(paths):
        info = archive.gettarinfo(str(path), arcname='qualcomm-isp/'+str(path.relative_to(root)))
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ''
        with path.open('rb') as source:
            archive.addfile(info, source)
digest = hashlib.sha256(destination.read_bytes()).hexdigest()
recipe = destination.parent/'PKGBUILD'
text = recipe.read_text()
import re
text = re.sub(r"sha256sums=\('[0-9a-fA-F]+|sha256sums=\('SKIP", "sha256sums=('"+digest, text)
recipe.write_text(text)
print(destination.name, digest)
