#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Bounded, fixed-control OV13858 IFE-to-IPE experiment on Denali OLED.

Requires the qualified installed front-camera runtime and a separately prepared
experimental work directory. This is a lab capture, not a PipeWire publisher.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import select
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=90)
    parser.add_argument('--raw-stats', action='store_true', help='Record rear paired RAW statistics')
    args = parser.parse_args()
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        parser.error('Use ASKPASS_REASON="..." sudo -A python -I ...')
    if not 8 <= args.frames <= 300:
        parser.error('--frames must be 8..300')
    account = pwd.getpwuid(int(os.environ['SUDO_UID']))
    if account.pw_uid == 0:
        parser.error('Capture must run as a non-root user')
    work = args.work.resolve(strict=True)
    out = args.output.absolute()
    if out.exists() or out.is_symlink():
        parser.error('--output must be a new directory')
    source = Path(__file__).resolve().parents[2]
    if (out.parent.resolve() / out.name).is_relative_to(source):
        parser.error('Keep captures outside the source repository')
    cam = work / 'build/src/apps/cam/cam'
    for path in (cam, work / 'ife.yaml', work / 'manual.yaml', work / 'anchor/cfg.bin'):
        if not path.is_file():
            parser.error(f'Missing experimental input: {path}')
    sys.path.insert(0, '/usr/lib/qualcomm-isp')
    import isp_config
    os.environ['QUALCOMM_ISP_CAPTURE_UID'] = str(account.pw_uid)
    preflight = isp_config.preflight()
    # This existing manifest proves the installed firmware/kernel transport,
    # not rear-camera qualification. The rear sensor is resolved separately.
    media, sensor = isp_config.sensor({'sensor_entity_prefix': 'ov13858 '})
    if preflight['board'] != 'surface-pro-11-denali-oled':
        raise ValueError('Only Denali OLED has been tested')
    userenv = ['runuser', '-u', account.pw_name, '--', 'env',
               f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}',
               f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{account.pw_uid}/bus']
    userctl = userenv + ['systemctl', '--user']
    services = ['qualcomm-isp-publisher.service', 'wireplumber.service']
    active = {s: subprocess.run(userctl + ['is-active', '--quiet', s]).returncode == 0 for s in services}
    param = Path('/sys/module/qcom_camss/parameters/ipp_pokes')
    original = param.read_text()
    if original.strip():
        raise ValueError('Refusing to replace existing IPP register overrides')
    out.mkdir(mode=0o755)
    os.chown(out, account.pw_uid, account.pw_gid)
    report = dict(frames=[], requested_frames=args.frames, kernel=os.uname().release,
                  mode='fixed-controls, live FIFO, serialized IPE', cleanup_errors=[])
    lock = session = child = fifo_fd = log = None
    loaded = changed_graph = False

    def run(*cmd):
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    def stop(signum, frame):
        raise InterruptedError(f'Signal {signum}')

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for service, on in active.items():
            if on:
                run(*userctl, 'stop', service)
        lock = open('/run/lock/camss-icp-lab.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path('/sys/module/qcom_camss_icp').exists():
            raise RuntimeError('ICP already loaded')
        changed_graph = True
        run('media-ctl', '-d', media, '-r')
        run('v4l2-ctl', '-d', sensor, '--set-ctrl=test_pattern=0')
        # Emulate the compiled CSID patch on the already installed kernel.
        param.write_text('0x330=0x1000000\n')
        run('modprobe', 'qcom_camss_icp')
        loaded = True
        spec = importlib.util.spec_from_file_location('rear_ipe', '/usr/lib/qualcomm-isp/ipe-stream.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.icplab.VERBOSE = False
        session = module.Session(work / 'anchor', readback='mmap')
        fifo = out / 'capture.fifo'
        os.mkfifo(fifo, 0o600)
        os.chown(fifo, account.pw_uid, account.pw_gid)
        # Keep a writer open across cam's per-frame opens, avoiding false EOF.
        fifo_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        log = (out / 'capture.log').open('w')
        extra_env = []
        if args.raw_stats:
            raw = out / 'paired-raw'
            raw.mkdir()
            os.chown(raw, account.pw_uid, account.pw_gid)
            extra_env = ['LIBCAMERA_CAMSS_RAW_STATS=' + str(raw), 'LIBCAMERA_LOG_LEVELS=Camss:DEBUG']
        command = userenv + extra_env + ['LIBCAMERA_CAMSS_OV13858_ISP=1',
                            'LIBCAMERA_CAMSS_TUNING=' + str(work / 'ife.yaml'),
                            'LD_LIBRARY_PATH=' + str(work / 'build/src/libcamera') + ':' + str(work / 'build/src/libcamera/base'),
                            str(cam), '-c', '/base/soc@0/cci@ac15000/i2c-bus@1/camera@10',
                            '--stream', 'width=3840,height=2160,pixelformat=NV12',
                            f'--capture={args.frames}', '--metadata', '--script', str(work / 'manual.yaml'),
                            '--file=' + str(fifo)]
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        started = time.monotonic()
        for index in range(args.frames):
            data = bytearray()
            deadline = time.monotonic() + 10
            size = 3840 * 2160 * 3 // 2
            while len(data) < size:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'Incomplete IFE frame {index}: {len(data)}/{size}')
                ready, _, _ = select.select([fifo_fd], [], [], 0.2)
                if ready:
                    data.extend(os.read(fifo_fd, size - len(data)))
                elif child.poll() is not None:
                    raise RuntimeError(f'Capture stopped early with status {child.returncode}')
            slot = index % module.SLOTS
            view = memoryview(data)
            prepared = session.prepare(slot, planes=(view[:3840*2160], view[3840*2160:]))
            token = session.submit(slot, prepared)
            result = session.readback(token, height=2160)
            session.complete(token)
            report['frames'].append(dict(index=index, seed=token['seed'],
                                         input_sha256=hashlib.sha256(data).hexdigest(),
                                         output_sha256=hashlib.sha256(result).hexdigest(),
                                         **token['times']))
            if index == args.frames - 1:
                (out / 'ife.nv12').write_bytes(data)
                (out / 'ipe.nv12').write_bytes(result)
            if (index + 1) % 30 == 0:
                print(f'Processed {index + 1}/{args.frames} live rear frames', flush=True)
        report['elapsed_seconds'] = time.monotonic() - started
        report['capture_returncode'] = child.wait(timeout=10)
        if child.returncode:
            raise RuntimeError(f'Capture failed: {child.returncode}')
        report['completed'] = True
    finally:
        # Attempt every cleanup even if an earlier action fails.
        actions = []
        if child and child.poll() is None:
            def terminate_capture():
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
            actions.append(('capture', terminate_capture))
        if session:
            actions.append(('IPE session', session.close))
        if loaded:
            actions.append(('ICP module', lambda: run('rmmod', 'qcom_camss_icp')))
        if changed_graph:
            actions.extend([('IPP override', lambda: param.write_text(original)),
                            ('media graph', lambda: run('media-ctl', '-d', media, '-r'))])
        if fifo_fd is not None:
            actions.append(('FIFO', lambda: os.close(fifo_fd)))
        if log:
            actions.append(('capture log', log.close))
        for label, action in actions:
            try:
                action()
            except Exception as exc:
                report['cleanup_errors'].append(f'{label}: {exc}')
        if lock:
            lock.close()
        report['runtime_usage'] = int(Path('/sys/bus/platform/devices/ac01000.icp/power/runtime_usage').read_text())
        report['module_unloaded'] = not Path('/sys/module/qcom_camss_icp').exists()
        for service, on in reversed(list(active.items())):
            if on:
                try:
                    run(*userctl, 'start', service)
                except Exception as exc:
                    report['cleanup_errors'].append(f'{service}: {exc}')
        (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        for path in out.iterdir():
            os.chown(path, account.pw_uid, account.pw_gid)
    if report['cleanup_errors'] or not report['module_unloaded'] or report['runtime_usage']:
        raise RuntimeError('Cleanup incomplete; inspect results.json')
    print(json.dumps({k: v for k, v in report.items() if k != 'frames'}), flush=True)


if __name__ == '__main__':
    main()
