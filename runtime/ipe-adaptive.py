#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Runtime-only tuning selection and bounded lab tone feedback.

No oracle, subprocess or proprietary input parser runs here. Sensor AE/AWB
belongs to libcamera. This module consumes captured-frame controls and selects
precomputed IPE programs; the display-luma policy is not Windows AE or TMC.
"""
import hashlib
import json
import math
from pathlib import Path
import struct

import numpy as np

REFERENCE = (1.884930453, 1.903106151)
ENVELOPE = .051
LOCUS_RANGE = (2300, 7500)
ANCHOR_CCT = 5000


class Locus:
    """The same anchored illuminant locus the inline AWB tracks: CCT from colour gains."""
    def __init__(self, references, reference_gains=REFERENCE, anchor_cct=ANCHOR_CCT, cct_range=LOCUS_RANGE):
        pts = sorted(((r['nominal_cct'], math.log(1/r['rg']), math.log(1/r['bg'])) for r in references
                      if cct_range[0] <= r['nominal_cct'] <= cct_range[1]), reverse=True)
        if len(pts) < 2:
            raise ValueError('Locus needs at least two references')
        self.points = [list(p) for p in pts]
        at = self.interpolate(self.position(anchor_cct))
        for pt in self.points:
            pt[1] += math.log(reference_gains[0]) - at[0]
            pt[2] += math.log(reference_gains[1]) - at[1]

    def position(self, cct):
        p = self.points
        if cct >= p[0][0]:
            return 0.0
        for i in range(1, len(p)):
            if cct >= p[i][0]:
                return i - 1 + (p[i-1][0] - cct) / (p[i-1][0] - p[i][0])
        return float(len(p) - 1)

    def interpolate(self, t):
        p = self.points
        t = min(max(t, 0.0), len(p) - 1.0)
        i = min(int(t), len(p) - 2)
        u = t - i
        return (p[i][1]*(1-u) + p[i+1][1]*u, p[i][2]*(1-u) + p[i+1][2]*u)

    def cct_at(self, t):
        p = self.points
        t = min(max(t, 0.0), len(p) - 1.0)
        i = min(int(t), len(p) - 2)
        u = t - i
        return p[i][0]*(1-u) + p[i+1][0]*u

    def cct_from_gains(self, gains):
        """Nearest locus position to the gains, as (cct, distance in log units)."""
        lg = (math.log(gains[0]), math.log(gains[1]))
        best, best_t = 1e9, 0.0
        p = self.points
        for i in range(len(p) - 1):
            ax, ay, bx, by = p[i][1], p[i][2], p[i+1][1], p[i+1][2]
            dx, dy = bx-ax, by-ay
            ln = dx*dx + dy*dy
            u = min(max(((lg[0]-ax)*dx + (lg[1]-ay)*dy) / ln, 0.0), 1.0) if ln > 0 else 0.0
            d = math.hypot(ax + u*dx - lg[0], ay + u*dy - lg[1])
            if d < best:
                best, best_t = d, i + u
        return self.cct_at(best_t), best


class ColourSets:
    """post.bin per (CCT, gain band) from prepare-colour-sets.py, with hysteresis on CCT."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        info = json.loads((self.path/'colour-sets.json').read_text())
        if info['version'] != 1 or info['file'] != 'post.bin' or info['gains'] != [2**(i/4) for i in range(25)]:
            raise ValueError('Unsupported colour sets')
        self.ccts = sorted(info['ccts'])
        self.files = {}
        for e in info['entries']:
            f = self.path/f"cct{e['cct']}"/f"g{e['gain_index']:02d}"/'post.bin'
            data = f.read_bytes()
            if len(data) != e['size'] or hashlib.sha256(data).hexdigest() != e['sha256']:
                raise ValueError(f'Colour set checksum mismatch: {f}')
            self.files[e['cct'], e['gain_index']] = f
        if len(self.files) != len(self.ccts)*25:
            raise ValueError('Incomplete colour sets')
        self.current = None

    def select(self, cct, gain_index):
        if not math.isfinite(cct):
            raise ValueError('Invalid CCT')
        cct = min(max(cct, self.ccts[0]), self.ccts[-1])
        nearest = min(self.ccts, key=lambda c: abs(math.log(c/cct)))
        if self.current is not None and self.current != nearest:
            # Stay with the current set until the estimate passes 60% of the
            # way to the neighbour, so a noisy estimate does not flap.
            lo, hi = sorted((self.current, nearest))
            edge = math.exp(math.log(lo) + 0.6*(math.log(hi)-math.log(lo))) if nearest > self.current else \
                   math.exp(math.log(hi) - 0.6*(math.log(hi)-math.log(lo)))
            if (nearest > self.current and cct < edge) or (nearest < self.current and cct > edge):
                nearest = self.current
        self.current = nearest
        return self.files[nearest, gain_index], dict(cct=nearest, estimate=cct, gain_index=gain_index)


def context(fields):
    try:
        exposure = int(fields['ExposureTime'])
        gain, digital = (float(fields[k]) for k in ('AnalogueGain', 'DigitalGain'))
        wb = tuple(float(x) for x in fields['ColourGains'].strip('[] ').split(','))
        if not 200 <= exposure <= 67000 or not math.isfinite(gain) or not 1 <= gain <= 64:
            return None
        # Sensor digital gain follows analogue gain in the dark; noise scales
        # with the product, so the program bank is selected on total gain.
        if not math.isfinite(digital) or not 1 <= digital <= 16 or not 1 <= gain * digital <= 64:
            return None
        # Any plausible IFE channel gain is valid: white balance is applied in
        # the Bayer domain and does not select IPE programs. The old
        # reference envelope only suited the daylight paper reference.
        if len(wb) != 2 or any(not math.isfinite(x) or not 0.25 <= x <= 4.0 for x in wb):
            return None
        if fields['AeEnable'] not in ('true','false') or fields['AwbEnable'] not in ('true','false'):
            return None
        black = tuple(int(x) for x in fields['SensorBlackLevels'].strip('[] ').split(','))
        if black != (4096,)*4:
            return None
        return dict(exposure_us=exposure, gain=gain * digital, analogue_gain=gain, digital_gain=digital, wb=wb,
                    ae=fields['AeEnable']=='true', awb=fields['AwbEnable']=='true')
    except (KeyError, ValueError, OverflowError):
        return None


def compatible(a,b):
    left, right = context(a), context(b)
    return left is not None and left == right


def luma(data, width=3840, height=2160):
    if memoryview(data).nbytes not in (width*height*3//2, width*height):
        raise ValueError('Expected native NV12 image or its luma plane')
    samples = np.frombuffer(data,np.uint8,count=width*height).reshape(height,width)[::8,::8].astype(float)
    h,w = samples.shape
    centre = samples[h//4:h*3//4,w//4:w*3//4]
    mean = (samples.sum()+2*centre.sum())/(samples.size+2*centre.size)
    return dict(weighted_mean=float(mean),mean=float(samples.mean()),p99=float(np.percentile(samples,99)))


class Bank:
    def __init__(self, path, dwell=4):
        self.path = Path(path).resolve()
        self.info = json.loads((self.path/'bank.json').read_text())
        r = self.info
        if (r['version'],r['width'],r['height']) != (1,3840,2176):
            raise ValueError('Unsupported IPE bank')
        self.tone_steps = len(r['tones'])
        if r['gains'] != [2**(i/4) for i in range(25)] or self.tone_steps not in (17, 25) or \
                r['tones'] != [2**(i/8) for i in range(self.tone_steps)]:
            raise ValueError('Unexpected tuning grid')
        self.max_drc = r['tones'][-1]
        self.entries = {}
        for entry in r['entries']:
            gi,ti = entry['gain_index'],entry['tone_index']
            if not 0<=gi<25 or not 0<=ti<self.tone_steps or entry['id'] != f'g{gi:02d}-t{ti:02d}' or (gi,ti) in self.entries:
                raise ValueError('Invalid bank entry')
            if entry['gain'] != r['gains'][gi] or entry['drc'] != r['tones'][ti]:
                raise ValueError('Bank coordinates disagree')
            directory = self.path/entry['id']
            required = {'cfg.bin','seed-cfg.bin','seed-iq.bin','seed-striping.bin',
                        'iq-ltm.bin','striping.bin','pre-ltm.bin','post.bin','ltm-luts.bin',
                        'gamma.bin','asf-luts.bin','dsx.bin','dsx-luts.bin',
                        'nps-anr-p0.bin','nps-anr-p1.bin','nps-anr-p2.bin',
                        'nps-tf-p0.bin','nps-tf-p1.bin'}
            if set(entry['files']) != required:
                raise ValueError('Incomplete bank file manifest')
            for name,expected in entry['files'].items():
                if Path(name).name != name:
                    raise ValueError('Invalid bank filename')
                data = (directory/name).read_bytes()
                if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                    raise ValueError(f'Bank checksum mismatch: {entry["id"]}/{name}')
            if struct.unpack_from('<II',(directory/'cfg.bin').read_bytes(),4) != (3840,2176):
                raise ValueError('Bank geometry mismatch')
            self.entries[gi,ti] = entry
        if len(self.entries) != 25*self.tone_steps:
            raise ValueError('Incomplete IPE bank')
        self.gain_index = None
        self.last_gain_index = None
        self.tone_index = None
        self.dwell = dwell
        self.candidate = None
        self.candidate_count = 0

    def select(self,gain,drc):
        if not all(math.isfinite(x) for x in (gain,drc)) or not 1<=gain<=64 or not 1<=drc<=self.max_drc+1e-9:
            raise ValueError('Controls outside tuning bank')
        gev = math.log2(gain)
        gi = min(24,max(0,int(math.floor(gev*4+.5))))
        if self.gain_index is not None and abs(gev-self.gain_index/4) <= .15:
            gi = self.gain_index
        tev = math.log2(drc)*8
        ti = min(self.tone_steps-1,max(0,int(math.floor(tev+.5))))
        if self.tone_index is not None and abs(tev-self.tone_index) <= .6:
            ti = self.tone_index
        # A different program must be asked for on `dwell` consecutive frames
        # before it replaces the current one, so a noisy estimate at a
        # quantization edge does not flap the denoise and tone programs.
        if self.gain_index is not None and (gi,ti) != (self.gain_index,self.tone_index):
            if (gi,ti) == self.candidate:
                self.candidate_count += 1
            else:
                self.candidate,self.candidate_count = (gi,ti),1
            if self.candidate_count < self.dwell:
                gi,ti = self.gain_index,self.tone_index
        else:
            self.candidate,self.candidate_count = None,0
        self.gain_index = gi
        self.last_gain_index = gi
        self.tone_index = ti
        entry = self.entries[gi,ti]
        return self.path/entry['id'], dict(id=entry['id'],gain=entry['gain'],drc=entry['drc'],
            actual_gain=gain,gain_error_ev=math.log2(entry['gain']/gain),requested_drc=drc)


class Tone:
    def __init__(self,target=125,max_gain=4.0):
        self.gain = 1.5
        self.target = target
        self.max_gain = max_gain
        self.previous = None
        self.stable = 0

    def observe(self,ctx,output_mean,age_ms):
        if not math.isfinite(output_mean) or not 0<=output_mean<=255 or not math.isfinite(age_ms) or age_ms<0 or age_ms>1000:
            self.stable = 0
            return 'invalid-or-stale'
        if not ctx['ae']:
            self.stable = 0
            self.previous = ctx
            return 'manual-exposure-hold'
        sensitivity = ctx['gain']*ctx['exposure_us']
        old = self.previous
        self.previous = ctx
        if old is None or not old['ae']:
            self.stable = 0
            return 'waiting-for-stable-controls'
        change = abs(math.log(sensitivity/(old['gain']*old['exposure_us'])))
        wb_change = max(abs(math.log(x/y)) for x,y in zip(ctx['wb'],old['wb']))
        if change>.25 or wb_change>.02:
            # Hold the current DRC: the bounded feedback below re-centres it
            # once the controls settle, without a shadow-crushing snap to 1.5.
            self.stable = 0
            return 'large-control-change-hold'
        if change>.02 or wb_change>.005:
            self.stable = 0
            return 'waiting-for-stable-controls'
        self.stable += 1
        if self.stable<2:
            return 'waiting-for-stable-controls'
        error = math.log2(self.target/max(output_mean,1))
        if abs(error)<.05:
            return 'within-luma-deadband'
        next_gain = min(self.max_gain,max(1,self.gain*2**max(-.125,min(.125,error*.5))))
        if next_gain == self.gain:
            return 'tone-limit'
        self.gain = next_gain
        return 'bounded-display-luma-feedback'
