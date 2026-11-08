#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Prepare an opt-in rear P010-interior bank from validated owner assets.

Preserves tone, color, sharpening and temporal-filter programs. Rebuilds ANR
with the corrected reserve records and ten-bit input, and regenerates striping.
No hardware or installation access. Generated assets must remain local.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(HERE.parent/'runtime'))
from inputs import DLL_SHA256, anr_reserve, descriptor, load, require_identity
from chromatix import Chromatix
from sensor_layout import LAYOUTS
spec=importlib.util.spec_from_file_location('adaptive',HERE.parent/'runtime/ipe-adaptive.py')
adaptive=importlib.util.module_from_spec(spec);spec.loader.exec_module(adaptive)
ANR=((0x4870,0,1),(0x4880,4,0x20),(0x4900,0x84,1),(0x4940,0x88,0xc),
     (0x4970,0xb8,0x16),(0x49d0,0x110,0xe),(0x4a28,0x148,0x2d),
     (0x4adc,0x1fc,0x7d),(0x4cf0,0x3f0,0x4b))

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('assets','tuning','dll','oracle','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--jobs',type=int,default=2)
    a=p.parse_args()
    if not 1<=a.jobs<=4:raise ValueError('Expected 1..4 jobs')
    if os.geteuid()==0:raise ValueError('Run preparation without privilege')
    out=a.output.absolute();out=out.parent.resolve()/out.name
    if out.exists() or out.is_symlink() or out.is_relative_to(HERE.parent):
        raise ValueError('Output must be new and outside the source repository')
    require_identity(a.dll,DLL_SHA256,'camera DLL')
    require_identity(a.tuning,LAYOUTS['ov13858']['sha256'],'rear tuning')
    source=adaptive.Bank(a.assets/'bank')
    if source.info.get('precision','nv12')!='nv12':raise ValueError('Expected source NV12 bank')
    awb=json.loads((a.assets/'awb-reference.json').read_text())
    if awb.get('source_sha256')!=LAYOUTS['ov13858']['sha256']:
        raise ValueError('Source assets belong to a different sensor')
    if source.info.get('dll_sha256')!=DLL_SHA256:
        raise ValueError('Source bank uses an unqualified DLL')
    out.mkdir(parents=True);bank=out/'bank';bank.mkdir();build=out/'preparation';build.mkdir()
    for name in ('ife.yaml','awb-reference.json'):shutil.copyfile(a.assets/name,out/name)
    c=Chromatix(a.tuning);colour=load('parity-colour');mod=colour.get_module(c,'anr10',0)
    regions=[c.data(x[1],x[2]) for x in mod if x[0]=='region' and x[2]==8640]
    if len(regions)!=7:raise ValueError('Unexpected ANR region count')
    intervals=colour.triggers(c,mod,7)
    (build/'reserve.bin').write_bytes(anr_reserve(descriptor(c,'anr10_ipe_v2',4,964)))
    env={k:v for k,v in os.environ.items() if not k.startswith('SLRUN_')};env['SLRUN_QUIET']='1'
    oracle=[str(a.oracle.resolve()),str(a.dll.resolve())]
    def run(log,*args):
        r=subprocess.run(oracle+[str(v) for v in args],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,check=True,timeout=30)
        log.write_text(r.stdout);return r.stdout
    gains=[]
    for gi,gain in enumerate(source.info['gains']):
        d=build/f'g{gi:02d}';d.mkdir()
        trigger=max(1,gain/2) if gain>=8 else gain
        lo,hi,t=colour.choose(intervals,trigger)
        for name,data in [('left.bin',regions[lo]),('right.bin',regions[hi])]: (d/name).write_bytes(data)
        if lo==hi:(d/'region.bin').write_bytes(regions[lo])
        else:run(d/'interpolate.log','interp','anr',d/'left.bin',d/'right.bin',t,d/'region.bin')
        log=run(d/'anr.log','anr',d/'region.bin',build/'reserve.bin',3,3840,2176,10,d/'regs.bin',d/'fw.bin',d/'chrom.bin')
        if 'anr calc rc=1' not in log or 'anr flow rc=0' not in log:raise ValueError('ANR calculation failed')
        regs=(d/'regs.bin').read_bytes();programs=[]
        for i in range(3):
            programs.append(b''.join(struct.pack('<II',(3<<24)|n,off)+regs[i*0x51c+start:i*0x51c+start+n*4] for off,start,n in ANR))
        gains.append((programs,(d/'fw.bin').read_bytes()))
    def entry(old):
        e=json.loads(json.dumps(old));d=bank/e['id'];d.mkdir();src=a.assets/'bank'/e['id']
        for f in e['files']:shutil.copyfile(src/f,d/f)
        programs,fw=gains[e['gain_index']]
        for i,b in enumerate(programs):(d/f'nps-anr-p{i}.bin').write_bytes(b)
        maximum=0
        for seed in (False,True):
            cfgname,iqname,stripname=('seed-cfg.bin','seed-iq.bin','seed-striping.bin') if seed else ('cfg.bin','iq-ltm.bin','striping.bin')
            cfg=bytearray((d/cfgname).read_bytes())
            for n in (0,4,8,10):
                off=n*96;fmt=20 if n==8 and not seed else 19
                struct.pack_into('<I',cfg,off,fmt)
                stride=3840 if fmt==20 else 7680
                struct.pack_into('<4I',cfg,off+64,stride,2176,stride,1088)
            (d/cfgname).write_bytes(cfg)
            iq=bytearray((d/iqname).read_bytes());iq[0x498:0x578]=fw
            for i in range(4):struct.pack_into('<I',iq,0x498+i*0x38,int(i<3))
            for off in (0xe00,0xe10):struct.pack_into('<4I',iq,off,0,1023 if seed else 255,0,1023 if seed else 255)
            (d/iqname).write_bytes(iq)
            log=run(d/(stripname+'.log'),'run',d/cfgname,d/iqname,'0x60800',0,d/stripname,1,3840,2176)
            match=re.search(r'scratchBufferSize=(\d+)',log)
            if 'Execute rc=0 ' not in log or not match:raise ValueError('Striping calculation failed')
            maximum=max(maximum,int(match[1]))
        if maximum>6<<20:raise ValueError('Scratch requirement exceeds qualified allocation')
        (d/'precision.json').write_text(json.dumps(dict(mode='p010-internal',scratch_bytes=6<<20))+'\n')
        e['files']['precision.json']={}
        e['files']={n:dict(size=(d/n).stat().st_size,sha256=sha(d/n)) for n in e['files']}
        e['scratch_required']=maximum
        return e
    entries=[]
    with ThreadPoolExecutor(a.jobs) as pool:
        futures=[pool.submit(entry,e) for e in source.info['entries']]
        for future in as_completed(futures):
            entries.append(future.result())
            if len(entries)%125==0:print(f'Prepared {len(entries)}/{len(futures)}',flush=True)
    info=json.loads(json.dumps(source.info));info.update(precision='p010-internal',entries=sorted(entries,key=lambda e:(e['gain_index'],e['tone_index'])))
    info['precision_source']=dict(bank_sha256=sha(a.assets/'bank/bank.json'),tuning_sha256=sha(a.tuning),dll_sha256=sha(a.dll),oracle_sha256=sha(a.oracle),builder_sha256=sha(Path(__file__)),note='Corrected ANR reserve; P010 interior, NV12 display. Source tone/color/TF/sharpening retained.')
    (bank/'bank.json').write_text(json.dumps(info,indent=2)+'\n')
    adaptive.Bank(bank)
    print('Verified complete precision bank:',out)

if __name__=='__main__':main()
