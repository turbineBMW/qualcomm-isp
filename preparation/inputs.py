# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Derive the qualified Surface inputs from owner-supplied tuning, without saved blobs."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'runtime'))
import pd10
from chromatix import Chromatix

DLL_SHA256 = 'c241b7fbb2ec54e439752a1ea7ad25da10ca740012a54bd0e7a87ea94a141c35'
TUNING_SHA256 = '2c1c7fd9090e0bf338f44bd9de785509c1fbebc975facc5286f12865cf675f1d'
REFERENCE_GAINS = [1.884930453, 1.903106151]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_identity(path, expected, label):
    actual = sha(path)
    if actual != expected:
        raise ValueError(f'Unsupported {label} SHA-256: {actual}; expected {expected}')


def descriptor(chrom, name, selector, size):
    matches = []
    for off in range(0xd0 + 56 * len(chrom.params), sum(chrom.sec[0]) - 55, 56):
        label = chrom.f[off:off + 32].split(b'\0')[0].decode()
        sel, data_off, length = struct.unpack_from('<III', chrom.f, off + 40)
        if label == name and sel == selector:
            matches.append(chrom.data(data_off, length))
    if len(matches) != 1 or len(matches[0]) != size:
        raise ValueError(f'Unexpected {name} descriptor for selector {selector}')
    return matches[0]


def config(width, height, seed=False):
    """API 2.52 image descriptors: full input/reference/display and PD10 pyramids."""
    result = bytearray(5984)
    for image in (0, 4, 8, 10):
        base = image * 96
        struct.pack_into('<4I', result, base, 20, width, height, 26)
        struct.pack_into('<4I', result, base + 64, width, height, width, height // 2)
    for image, factor in ((1, 4), (2, 16), *((() if seed else ((5, 4), (11, 4))))):
        w, h, stride, slice_height = pd10.dims(width, height, factor)
        base = image * 96
        struct.pack_into('<3I', result, base, 10, w, h)
        struct.pack_into('<4I', result, base + 64, stride, slice_height, 0, 0)
    struct.pack_into('<I', result, 0x1750, 1)  # single-frame batch
    return bytes(result)


def initial_iq(coefficients, seed=False):
    """Construct enables/clamps and geometry; ANR/TF/DSX/LTM are filled by the DLL."""
    result = bytearray(0x1834)
    for index in range(4):
        result[0xbe0 + index * 20:0xbec + index * 20] = coefficients
    for base in (0xe00, 0xe10):
        struct.pack_into('<4I', result, base, 0, 255, 0, 255)
    # Retain the qualified initial (inactive) DSX slots. The builder replaces
    # pass 0 for 3840x2176; the unused slots keep the original 1920x1080 geometry.
    for index in range(4):
        for plane in range(2):
            scale = 1 << plane
            struct.pack_into('<8I', result, 0xf94 + index * 64 + plane * 32,
                             0, 0, 4 << 17, 4 << 17, 3 << 16, 3 << 16,
                             1920 // (4 * scale), 1080 // (4 * scale))
    if not seed:
        for off in (0xc44, 0xca4, 0xca8, 0xcb0, 0xcf0, 0xcfc, 0xd00):
            struct.pack_into('<I', result, off, 1)
    return bytes(result)


def prepare(tuning, destination):
    require_identity(tuning, TUNING_SHA256, 'IMX681 tuning')
    chrom = Chromatix(tuning)
    colour = load('parity-colour')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    coefficients = descriptor(chrom, 'ds4to1v11_ipe', 4, 88)[28:40]
    data = {
        'anr-reserve.bin': descriptor(chrom, 'anr10_ipe_v2', 4, 964)[48:964],
        # Preserve selector 157 used by the accepted bank. It differs from
        # selector 4 at the trailing descriptor word; do not silently substitute.
        'tf-reserve.bin': descriptor(chrom, 'tf21_ipe_v2', 157, 396)[68:396],
        'ltm-reserve.bin': descriptor(chrom, 'ltm16_ipe_v2', 4, 776)[40:760],
        'ds4-coefficients.bin': coefficients,
        'iq-full.bin': initial_iq(coefficients),
        'iq-seed.bin': initial_iq(coefficients, seed=True),
        'cfg.bin': config(3840, 2176),
        'seed-cfg.bin': config(3840, 2176, seed=True),
    }
    dsx = colour.get_module(chrom, 'dsx10', 2)
    regions = [chrom.data(p[1], p[2]) for p in dsx if p[0] == 'region' and p[2]]
    if len(regions) != 1 or len(regions[0]) != 2824:
        raise ValueError('Unexpected base IPE DSX region')
    data['dsx-region.bin'] = regions[0]
    for name, blob in data.items():
        (destination / name).write_bytes(blob)
    return {name: hashlib.sha256(blob).hexdigest() for name, blob in data.items()}


def ife_profile(tuning):
    """The accepted active settings, with a locally extracted gamma curve."""
    colour = load('parity-colour')
    chrom = Chromatix(tuning)
    curves = colour.regions(chrom, colour.get_module(chrom, 'gamma15', 1))
    if len(curves) != 1 or len(curves[0]) != 771:
        raise ValueError('Unexpected IFE gamma layout')
    curve = curves[0]
    if curve[:257] != curve[257:514] or curve[:257] != curve[514:]:
        raise ValueError('Expected identical IFE gamma channels')
    gamma = [int(v) for v in curve[:256]]
    if gamma != list(curve[:256]):
        raise ValueError('Expected integer IFE gamma knots')
    contract = load('gamma-chain')
    if hashlib.sha256(struct.pack('<256H', *gamma)).hexdigest() != contract.SUPPORTED_IFE:
        raise ValueError('Unqualified IFE gamma curve')
    return {
        'version': 1,
        'ae': {'target': 110, 'centre_weight': 3, 'slow_frame': 2.0,
               'sensor_digital_max': 1.0, 'raw_highlights': True},
        'zoom': 1.0,
        'awb': {'raw_reference_gains': REFERENCE_GAINS, 'blue_bias': 0.94,
                'warmth': 0.4, 'neutral_ratio': 1.0},
        'gamma_lut': gamma,
        'denoise': {'enable': False, 'temporal': 0.35, 'threshold': 2.0,
                    'ramp': 1.5, 'chroma_radius': 0},
        'spatial': {'enable': False, 'luma': [0.8, 0.6, 0.3, 0.0],
                    'sharpen': [0.8, 0.5, 0.0, 0.0], 'despeckle': 1.5,
                    'chroma_median': True, 'chroma': [2.0] * 4,
                    'luma_ratios': [1.0, 0.82, 0.53, 0.30],
                    'chroma_ratios': [1.0, 0.76, 0.43, 0.24],
                    'threads': 4, 'min_noise': 1.5},
        # CCM is disabled; use an explicit identity instead of preserving an
        # unused legacy matrix. No Windows colour matrix is embedded here.
        'ccm_strength': 0.0, 'ccm': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        'lsc_strength': 0.0, 'lsc': {channel: [1.0] * 221 for channel in ('r', 'gr', 'gb', 'b')},
    }
