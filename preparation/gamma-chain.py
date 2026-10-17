#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Validate the measured IFE gamma -> LTM table0 -> IPE gamma contract.

This is a lab constraint for the tested Windows tuning and LTM161 path. It
does not infer a curve from image brightness or accept arbitrary IFE profiles.
"""
import hashlib
import math
from pathlib import Path
import struct

import yaml

SUPPORTED_DLL = 'c241b7fbb2ec54e439752a1ea7ad25da10ca740012a54bd0e7a87ea94a141c35'
SUPPORTED_IFE = '571958a9d97b407114febbc20f43094ea705d8e471f21c8012e2406111218088'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def profile_curve(path):
    profile = yaml.safe_load(path.read_text())
    values = profile.get('gamma_lut') if isinstance(profile, dict) else None
    if (not isinstance(values, list) or len(values) != 256 or
        any(type(v) not in (int, float) or not math.isfinite(v) or
            v != int(v) or not 0 <= v <= 4095 for v in values)):
        raise ValueError('Gamma contract requires an explicit 256-entry integer IFE gamma_lut')
    return [int(v) for v in values]


def verify(profile, expected_ife, iq, gamma, expected_gamma, dll_sha256):
    if dll_sha256 != SUPPORTED_DLL:
        raise ValueError('Windows DLL version has not been measured for this gamma contract')
    if len(iq) != 0x1834 or len(gamma) != 3072:
        raise ValueError('Unexpected IPE IQ/gamma size')
    if len(expected_ife) != 771 or any(expected_ife[:257] != expected_ife[ch*257:(ch+1)*257] for ch in (1,2)):
        raise ValueError('The measured IFE contract requires identical RGB gamma curves')
    expected = [int(v) for v in expected_ife[:256]]
    if any(a != b for a,b in zip(expected,expected_ife[:256])):
        raise ValueError('Unexpected non-integer Windows IFE knots')
    if digest(struct.pack('<256H',*expected)) != SUPPORTED_IFE:
        raise ValueError('Windows IFE curve has not been measured against LTM table0')
    ltm = dict(zip(('enable','gamma_enable','rgamma_enable','la_enable','mask_enable','nonlinear_input','table'),
                   struct.unpack_from('<7I',iq,0x1238)))
    if any(ltm[key] != value for key,value in
           {'enable':1,'gamma_enable':1,'rgamma_enable':1,'nonlinear_input':1,'table':0}.items()):
        raise ValueError('IPE LTM gamma configuration differs from the measured table0 path')
    if struct.unpack_from('<I',iq,0xca8)[0] != 1 or gamma != expected_gamma:
        raise ValueError('IPE output gamma differs from the selected Windows curve')
    report = dict(status='unverified-input', ltm=ltm, windows_dll_sha256=dll_sha256, ipe_gamma_sha256=digest(gamma),
                  expected_ife_knots_sha256=digest(struct.pack('<256H',*expected)),
                  note='Only table0 with both LTM gamma flags and nonlinear input has been validated. Hardware ramp tests use the supported NPS topology. This is a transfer-curve contract, not Windows scene/colour parity.')
    if profile is not None:
        values = profile_curve(profile)
        if values != expected:
            raise ValueError('Input IFE gamma does not match the measured LTM table0 contract')
        report.update(status='verified-profile', profile=str(profile.resolve()),
                      profile_sha256=digest(profile.read_bytes()), maximum_knot_difference=0)
    return report
