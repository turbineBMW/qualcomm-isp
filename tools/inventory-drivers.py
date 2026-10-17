#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Inventory camera candidates in an extracted Windows driver directory; never execute them."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

MAX_INF_BYTES = 4 * 1024 * 1024
SENSOR = re.compile(r'(?<![a-z0-9])(?:imx\d+[a-z0-9]*|ov\d+[a-z0-9]*|vd55[a-z0-9]+|hi\d{3,}[a-z0-9]*|gc\d{3,}[a-z0-9]*)(?![a-z0-9])', re.I)
CAMERA = re.compile(r'camera|webcam|qccam|camx|qcdevicemft|sensormodule|chromatix|(?:^|[._-])(?:tuned|tuning|icp|ipe|bps|camss)(?:[._-]|$)', re.I)
PAYLOAD_SUFFIXES = {'.dll', '.sys', '.bin', '.mbn', '.elf', '.mdt', '.dat', '.xml', '.json', '.cat', '.ini'}


def camera_hint(value):
    return bool(CAMERA.search(value) or SENSOR.search(value))


def uncomment(line):
    """INF semicolons start comments only outside quoted strings."""
    quoted = False
    for i, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == ';' and not quoted:
            return line[:i].strip()
    return line.strip()


def unquote(value):
    value = value.strip()
    return value[1:-1].replace('""', '"') if value.startswith('"') and value.endswith('"') else value


def parse_inf(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
        encoding = 'utf-16'
    else:
        try:
            text = data.decode('utf-8-sig')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            text = data.decode('cp1252')
            encoding = 'windows-1252'
    if '\0' in text:
        raise ValueError('unsupported INF encoding')
    sections = defaultdict(list)
    section, pending = '', ''
    for raw in text.splitlines():
        line = pending + uncomment(raw)
        if line.endswith('\\'):
            pending = line[:-1]
            continue
        pending = ''
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().casefold()
        elif '=' in line:
            key, value = line.split('=', 1)
            sections[section].append((key.strip(), value.strip()))
    if pending or 'version' not in sections:
        raise ValueError('incomplete INF or missing Version section')
    # Neutral Strings are used first; English/localized definitions fill gaps.
    strings = {}
    for name in ['strings', 'strings.0409', *sorted(sections)]:
        if name == 'strings' or name.startswith('strings.'):
            for key, value in sections[name]:
                strings.setdefault(key.casefold(), unquote(value))

    def expand(value):
        for _ in range(8):
            new = re.sub(r'%([^%]+)%', lambda m: strings.get(m[1].casefold(), m[0]), value)
            if new == value:
                break
            value = new
        return unquote(value)

    version = {key.casefold(): expand(value) for key, value in sections['version']}
    metadata = {key: version[key] for key in ('class', 'classguid', 'provider', 'driverver') if key in version}
    metadata['catalog_files'] = sorted({value for key, value in version.items()
                                        if key == 'catalogfile' or key.startswith('catalogfile.')})
    metadata['encoding'] = encoding
    refs = set()
    unsafe_refs = 0
    for name, entries in sections.items():
        if name != 'sourcedisksfiles' and not name.startswith('sourcedisksfiles.'):
            continue
        for filename, value in entries:
            fields = next(csv.reader([value], skipinitialspace=True))
            subdir = expand(fields[1]) if len(fields) > 1 else ''
            filename = expand(filename).replace('\\', '/')
            subdir = subdir.replace('\\', '/')
            candidate = PurePosixPath(subdir) / filename
            if (candidate.is_absolute() or '..' in candidate.parts or ':' in str(candidate)
                    or not filename or filename.startswith('/')):
                unsafe_refs += 1
                continue
            refs.add(candidate.as_posix())
    clean_text = '\n'.join(uncomment(line) for line in text.splitlines())
    return metadata, refs, unsafe_refs, camera_hint(clean_text)


def regular_file(path):
    """Refuse special files and leaf symlinks even if changed since enumeration."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('not a regular file')
    return os.fdopen(fd, 'rb')


def identity(path, limit=None):
    with regular_file(path) as source:
        before = os.fstat(source.fileno())
        if limit is not None and before.st_size > limit:
            raise ValueError('INF exceeds 4 MiB parsing limit')
        digest, chunks, total = hashlib.sha256(), [], 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if limit is not None:
                if total > limit:
                    raise ValueError('INF exceeds 4 MiB parsing limit')
                chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or total != before.st_size:
            raise ValueError('file changed while reading')
    return {'size': total, 'sha256': digest.hexdigest()}, b''.join(chunks)


def classify(name):
    lower = name.casefold()
    suffix = Path(name).suffix.casefold()
    if suffix == '.inf':
        return 'driver-inf'
    if suffix in ('.mbn', '.elf', '.mdt') or re.search(r'\.b\d\d$', lower):
        return 'firmware-candidate'
    if 'tuned' in lower or 'tuning' in lower or 'chromatix' in lower:
        return 'tuning-candidate'
    if 'sensormodule' in lower:
        return 'sensor-module-candidate'
    if suffix == '.dll':
        return 'camera-dll-candidate'
    if suffix == '.sys':
        return 'driver-binary'
    return 'package-support-file'


def inventory(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('input must be an extracted driver directory')
    warnings, files = [], {}

    def warn(path, reason):
        warnings.append({'path': path, 'reason': reason})

    def walk(directory):
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            warn(directory.relative_to(root).as_posix(), error.strerror or 'directory read failed')
            return
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            try:
                mode = child.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode) or (hasattr(path, 'is_junction') and path.is_junction()):
                    warn(relative, 'symlink or junction skipped')
                elif stat.S_ISDIR(mode):
                    walk(path)
                elif stat.S_ISREG(mode):
                    files[relative] = path
                else:
                    warn(relative, 'special file skipped')
            except OSError as error:
                warn(relative, error.strerror or 'file stat failed')

    walk(root)
    selected, infs, cache = defaultdict(set), [], {}
    paths_by_case = defaultdict(list)
    siblings = defaultdict(list)
    for relative, path in files.items():
        paths_by_case[relative.casefold()].append(relative)
        siblings[PurePosixPath(relative).parent.as_posix()].append(relative)
        if camera_hint(path.name) and (path.suffix.casefold() in PAYLOAD_SUFFIXES or re.search(r'\.b\d\d$', path.name, re.I)):
            selected[relative].add('camera-related filename (heuristic)')

    scanned_infs = 0
    for relative, path in files.items():
        if path.suffix.casefold() != '.inf':
            continue
        scanned_infs += 1
        try:
            cache[relative], data = identity(path, MAX_INF_BYTES)
            metadata, refs, unsafe, text_hint = parse_inf(data)
        except (OSError, ValueError, csv.Error) as error:
            reason = ('unsupported or malformed INF encoding' if isinstance(error, UnicodeError)
                      else error.strerror if isinstance(error, OSError) else str(error))
            warn(relative, reason)
            if camera_hint(path.name):
                selected[relative].add('camera-related INF filename (metadata unavailable)')
            continue
        reasons = []
        if metadata.get('class', '').casefold() in ('camera', 'image'):
            reasons.append('Camera/Image setup class (candidate, not hardware identification)')
        if camera_hint(path.name):
            reasons.append('camera-related INF filename')
        if text_hint:
            reasons.append('camera-related INF text (heuristic)')
        if not reasons:
            continue
        selected[relative].update(reasons)
        parent = PurePosixPath(relative).parent
        found, unmatched = [], []
        for reference in sorted(refs):
            matches = paths_by_case.get((parent / reference).as_posix().casefold(), [])
            if not matches:
                unmatched.append(reference)
            for match in matches:
                found.append(match)
                selected[match].add('SourceDisksFiles entry in ' + relative)
        for sibling in siblings[parent.as_posix()]:
            if files[sibling].suffix.casefold() in PAYLOAD_SUFFIXES:
                selected[sibling].add('same directory as ' + relative + ' (heuristic)')
        if unsafe:
            warn(relative, f'{unsafe} absolute or parent-traversing SourceDisksFiles entries ignored')
        infs.append({'path': relative, **metadata, 'selection_reasons': reasons,
                     'source_files_found': sorted(set(found)), 'source_entries_not_found': unmatched})

    records = []
    for relative in sorted(selected):
        path = files[relative]
        try:
            details = cache.get(relative)
            if details is None:
                details, _ = identity(path)
        except (OSError, ValueError) as error:
            warn(relative, error.strerror if isinstance(error, OSError) else str(error))
            continue
        records.append({'path': relative, 'kind': classify(path.name), **details,
                        'sensor_name_hints': sorted({m[0].lower() for m in SENSOR.finditer(path.name)}),
                        'selection_reasons': sorted(selected[relative])})
    by_name = defaultdict(list)
    for record in records:
        by_name[PurePosixPath(record['path']).name.casefold()].append(record)
    duplicates = []
    for name, entries in sorted(by_name.items()):
        if len(entries) < 2:
            continue
        variants = defaultdict(list)
        for entry in entries:
            variants[entry['sha256']].append(entry['path'])
        duplicates.append({'filename': name, 'identical': len(variants) == 1,
                           'variants': [{'sha256': digest, 'paths': sorted(paths)}
                                        for digest, paths in sorted(variants.items())]})
    return {'schema': 1, 'tool': 'qualcomm-isp/inventory-drivers',
            'scan_complete': not warnings,
            'summary': {'regular_files_seen': len(files), 'inf_files_examined': scanned_infs,
                        'camera_inf_candidates': len(infs), 'candidate_files': len(records),
                        'conflicting_filenames': sum(not group['identical'] for group in duplicates)},
            'camera_infs': sorted(infs, key=lambda entry: entry['path']), 'files': records,
            'duplicate_filenames': duplicates,
            'warnings': sorted(warnings, key=lambda entry: (entry['path'], entry['reason'])),
            'notes': ['Paths are relative to the supplied directory; no root path, host identity or raw file contents are included.',
                      'Selection and sensor names are hints, not proof of installed hardware, package membership or ISP compatibility.',
                      'All SourceDisksFiles decorations are considered; unmatched entries may belong to other architectures or an incomplete export.',
                      'INF parsing is a limited metadata reader, not Windows SetupAPI. No signatures are verified and no preferred variant is selected.',
                      'A complete scan means no reported read/skip errors, not that all camera components were identified.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Extracted OEM package or Windows driver export; never an installer executable')
    parser.add_argument('--output', type=Path, help='New JSON file outside the scanned directory; default: stdout')
    args = parser.parse_args()
    try:
        root = args.directory.resolve(strict=True)
        if not root.is_dir():
            raise ValueError('input must be an extracted driver directory')
        if args.output:
            output = args.output.absolute()
            output = output.parent.resolve(strict=True) / output.name
            if output.exists() or output.is_symlink():
                raise ValueError('output already exists; choose a new report filename')
            if output.is_relative_to(root):
                raise ValueError('output must be outside the scanned directory')
        report = inventory(root)
        payload = json.dumps(report, indent=2, ensure_ascii=True) + '\n'
        if args.output:
            with output.open('x', encoding='utf-8') as destination:
                destination.write(payload)
        else:
            sys.stdout.write(payload)
        print(f"Found {report['summary']['candidate_files']} candidate files, {report['summary']['camera_inf_candidates']} camera INF candidates; {len(report['warnings'])} warning(s).", file=sys.stderr)
        return 0 if report['scan_complete'] else 1
    except (OSError, ValueError) as error:
        # Do not print absolute input paths in diagnostics either.
        parser.error(error.strerror if isinstance(error, OSError) else str(error))


if __name__ == '__main__':
    sys.exit(main())
