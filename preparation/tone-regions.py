#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Select nested Windows TMC/LTM tuning using explicit, uncalibrated triggers.

This supports the local non-LED DRC/HDR-AEC/lux tree only. Interpolation stays
in the Windows callbacks; no assumption that every region field is a float.
"""
import hashlib
import math
import struct


def tree(chrom, colour, kind):
    name, occurrence, size, leaves = {'tmc': ('tmc14', 1, 368, 3),
                                      'ltm': ('ltm16', 0, 716, 4)}[kind]
    parts = colour.get_module(chrom, name, occurrence)
    controls = next(p for p in parts if p[0] == 'control_var_type')
    if chrom.data(controls[1], controls[2]) != struct.pack('<3I', 2, 5, 0):
        raise ValueError('Unsupported tone trigger axes; expected DRC/HDR-AEC/lux')
    byid = {p[3]: p for p in parts}
    root = next(p for p in parts if p[0] == 'mod_' + name + '_trigger_data')
    region_ids = [p[3] for p in parts if p[0] == 'region' and p[2]]

    def read(p, depth):
        expected = (2, 1, leaves)[depth]
        if p[2] != expected * 24:
            raise ValueError('Unexpected tone trigger count')
        result = []
        previous = -math.inf
        for start, end, count, child_id, has_region, region_id in struct.iter_unpack('<2f4I', chrom.data(p[1], p[2])):
            if not all(math.isfinite(x) for x in (start, end)) or start > end or start <= previous:
                raise ValueError('Invalid tone trigger intervals')
            previous = end
            # Serialized references are zero-based parameter indices; the
            # descriptor ID column is one-based in this Chromatix container.
            child, region = byid[child_id + 1], byid[region_id + 1]
            node = dict(interval=[start, end])
            if depth == 2:
                if count or has_region != 1 or region[0] != 'region' or region[2] != size or child[2]:
                    raise ValueError('Invalid tone leaf reference')
                node.update(region=region_ids.index(region_id + 1), data=chrom.data(region[1], region[2]))
            else:
                if count != (1, leaves)[depth] or has_region or region[2] or child[0] != 'trigger':
                    raise ValueError('Invalid tone child reference')
                node['children'] = read(child, depth + 1)
            result.append(node)
        return result

    return read(root, 0)


def select(chrom, colour, kind, drc_gain, lux_index, out, oracle, run):
    if not math.isfinite(drc_gain) or not 1 <= drc_gain <= 16 or not math.isfinite(lux_index):
        raise ValueError('Invalid tone trigger input')
    nodes = tree(chrom, colour, kind)
    # One HDR-AEC child per DRC branch: unity for the current single exposure.
    values = [drc_gain, 1.0, lux_index]
    decisions, weights = [], {}

    def visit(items, depth, path, weight):
        intervals = [n['interval'] for n in items]
        lo, hi, ratio = colour.choose(intervals, values[depth])
        # Windows triggers and interpolation factors are float32.
        ratio = struct.unpack('<f', struct.pack('<f', ratio))[0]
        decisions.append(dict(path=path, axis=depth, intervals=intervals,
                              selection=[lo, hi, ratio]))
        outputs = []
        for index, factor in ([(lo, 1.0)] if lo == hi else [(lo, 1-ratio), (hi, ratio)]):
            node = items[index]
            childpath = path + [index]
            if depth == 2:
                data = node['data']
                weights[node['region']] = weights.get(node['region'], 0) + weight * factor
            else:
                data = visit(node['children'], depth + 1, childpath, weight * factor)
            outputs.append(data)
        if lo == hi:
            return outputs[0]
        stem = kind + '-blend-' + ('-'.join(map(str, path)) or 'root')
        left, right, dest = [out / (stem + suffix) for suffix in ('-left.bin', '-right.bin', '.bin')]
        left.write_bytes(outputs[0]); right.write_bytes(outputs[1])
        run(stem, *oracle, 'interp', kind, left, right, ratio, dest)
        return dest.read_bytes()

    values = [struct.unpack('<f', struct.pack('<f', v))[0] for v in values]
    data = visit(nodes, 0, [], 1.0)
    (out / (kind + '-region.bin')).write_bytes(data)
    return dict(axes=['drc_gain', 'hdr_aec', 'lux_index'], values=values,
                lux_source='explicit, uncalibrated Windows index; not physical lux',
                hdr_source='unity, single exposure; all HDR branches have one child',
                decisions=decisions, leaf_weights=weights,
                region_sha256=hashlib.sha256(data).hexdigest())


def oracle_tree(nodes):
    """Independent DLL tree-walker fixture, retaining all branch intervals."""
    leaves = len(nodes[0]['children'][0]['children'])
    result = struct.pack('<2I', len(nodes), leaves)
    for node in nodes:
        hdr = node['children'][0]
        result += struct.pack('<4f', *node['interval'], *hdr['interval'])
        for leaf in hdr['children']:
            result += struct.pack('<2f', *leaf['interval']) + leaf['data']
    return result
