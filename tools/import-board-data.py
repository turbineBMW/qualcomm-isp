#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Copy locally supplied board data; no downloading or redistribution implied."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--bank', type=Path, required=True)
p.add_argument('--ife', type=Path, required=True)
p.add_argument('--awb', type=Path, required=True)
p.add_argument('--destination', type=Path, required=True)
a = p.parse_args()
if not (a.bank/'bank.json').is_file() or not a.ife.is_file() or not a.awb.is_file():
    p.error('Supply a complete program bank, IFE tuning file and AWB reference file')
a.destination.mkdir(parents=True, exist_ok=False)
shutil.copytree(a.bank, a.destination/'bank')
shutil.copyfile(a.ife, a.destination/'ife.yaml')
shutil.copyfile(a.awb, a.destination/'awb-reference.json')
manifest = {str(f.relative_to(a.destination)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(a.destination.rglob('*')) if f.is_file()}
(a.destination/'local-data-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(f'Imported {len(manifest)} files to {a.destination}; local use, not for publication')
