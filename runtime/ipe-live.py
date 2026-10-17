#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Bounded native-4K live IPE trial with fixed or adaptive controls.

Run only inside run-ipe-session.sh. Camera and PipeWire clients run as the
desktop user. Temporal history is continuous: each output uses the previous
output as its reference even when capture frames were skipped in between.
A seed pass runs only at start and after a program switch; it is not
published. Capture copy/reduction, firmware processing and readback/publish
run in three overlapping stages with two input/output slots.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
import isp_config
CONFIG, BOARD = isp_config.load()
CAPTURE_USER = isp_config.identity()
ASSETS = Path(CONFIG['assets'])
CAM = CONFIG['cam']
CAMERA = BOARD['camera']
W, H, PAD = 3840, 2160, 2176
SIZE = W * H * 3 // 2
CONTROLS = dict(AeEnable=False, AwbEnable=False, ExposureTime=32878,
                AnalogueGain=1.750427, ColourGains=[1.884766, 1.903320])
FIRST_SEQUENCE = 12   # startup frames before this are never processed
FRESH_NS = 500_000_000


def limit_start_exposure(exposure, gain, slow_frame):
    """Fit a saved shutter to this session's budget, preserving sensitivity."""
    limited = min(exposure, int(CONTROLS['ExposureTime'] * slow_frame))
    return limited, min(16.0, gain * exposure / limited)


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE/file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15).strip()


def as_user(*args):
    return ['runuser', '-u', CAPTURE_USER.pw_name, '--', 'env', '-u', 'LIBCAMERA_CAM_LEASE_SOCKET', '-u', 'LIBCAMERA_CAMSS_RAW_STATS',
            '-u', 'LIBCAMERA_CAMSS_RAW_SAVE', f'XDG_RUNTIME_DIR=/run/user/{CAPTURE_USER.pw_uid}',
            f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{CAPTURE_USER.pw_uid}/bus', *map(str, args)]


def stop_process(process, camera=False):
    if process and process.poll() is None:
        if camera:
            # runuser translates group signals to SIGTERM. Send SIGINT to
            # its cam child so libcamera drains requests and closes RAW logs.
            children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
            for child in children:
                try:
                    if Path(f'/proc/{child}/exe').resolve() == Path(CAM).resolve():
                        os.kill(int(child),signal.SIGINT)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=4)
                return
            except subprocess.TimeoutExpired:
                pass
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def finish_publisher(process):
    """Let EOF drain the reader; retain final publisher failures in the report."""
    try:
        rc = process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        stop_process(process)
        raise TimeoutError('Publisher did not exit after its transport closed')
    if rc:
        raise RuntimeError(f'Publisher exited {rc} during cleanup')


def metadata_valid(fields):
    try:
        gains = [float(x) for x in fields['ColourGains'].strip('[] ').split(',')]
        return (fields['AeEnable'] == fields['AwbEnable'] == 'false' and
                int(fields['ExposureTime']) == CONTROLS['ExposureTime'] and
                abs(float(fields['AnalogueGain']) - CONTROLS['AnalogueGain']) < .00001 and
                float(fields['DigitalGain']) == 1 and len(gains) == 2 and
                max(abs(x-y) for x,y in zip(gains, CONTROLS['ColourGains'])) < .00001)
    except (KeyError, ValueError):
        return False


class Capture:
    def __init__(self, out, stop, adaptive=None, save_raw=False, transport="fifo"):
        self.stop = stop
        self.io_stop = threading.Event()
        self.adaptive = adaptive
        self.out = out
        self.condition = threading.Condition()
        self.raw = deque()
        self.latest = deque(maxlen=2)
        self.errors = []
        self.count = self.valid = 0
        self.leases = None
        self.lease_summary = None
        self.draining = False
        if transport == 'lease':
            # Unix socket paths are limited to 108 bytes; evidence directories
            # can be longer than that, so the lease socket lives in a short
            # runtime directory the camera user can traverse.
            runtime = Path('/run/qualcomm-isp/capture')
            runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
            self.leases = module('ipe_capture_lease', 'ipe-capture-lease.py').Receiver(runtime/f'capture-{os.getpid()}.sock', uid=CAPTURE_USER.pw_uid, gid=CAPTURE_USER.pw_gid)
        else:
            self.fifo = out/'frames.fifo'
            os.mkfifo(self.fifo, 0o600)
            os.chown(self.fifo, CAPTURE_USER.pw_uid, CAPTURE_USER.pw_gid)
            self.fd = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
        self.errlog = (out/'capture-stderr.log').open('w')
        raw_env = []
        if adaptive:
            raw = out/'paired-raw'
            raw.mkdir()
            os.chown(raw, CAPTURE_USER.pw_uid, CAPTURE_USER.pw_gid)
            raw_env = ['LIBCAMERA_CAMSS_RAW_STATS='+str(raw)]
            if save_raw:
                raw_env.append('LIBCAMERA_CAMSS_RAW_SAVE=1')
        if self.leases:
            raw_env.append('LIBCAMERA_CAM_LEASE_SOCKET='+str(self.leases.path))
        self.process = subprocess.Popen(as_user(*raw_env, 'LIBCAMERA_CAMSS_TUNING='+str(out/'ife.yaml'),
            'LD_LIBRARY_PATH='+CONFIG['cam_library_path'], CAM,
            '-c', CAMERA, '--stream', 'width=3840,height=2160,pixelformat=NV12',
            '--capture=0', '--metadata', '--script', out/'controls.yaml',
            *([] if self.leases else ['--file='+str(self.fifo)])),
            stdout=subprocess.PIPE, stderr=self.errlog, text=True, bufsize=1, start_new_session=True)
        readers = (self.read_leases, self.read_log) if self.leases else (self.read_frames, self.read_metadata)
        self.threads = [threading.Thread(target=f, daemon=True) for f in readers]
        for thread in self.threads:
            thread.start()

    def fail(self, error):
        with self.condition:
            self.errors.append(str(error))
            self.condition.notify_all()

    def read_log(self):
        with (self.out/'capture.log').open('w', buffering=1) as log:
            for line in self.process.stdout:
                log.write(line)

    def read_leases(self):
        try:
            while not self.io_stop.is_set() and self.leases.peer is None:
                try:
                    self.leases.accept()
                except socket.timeout:
                    if self.process.poll() is not None:
                        raise RuntimeError('Camera exited before connecting its lease transport')
            while not self.io_stop.is_set():
                try:
                    current = self.leases.receive()
                except socket.timeout:
                    continue
                if current is None:
                    if not self.stop.is_set():
                        raise RuntimeError('Camera ended before the live trial finished')
                    break
                if not current:
                    continue
                with self.condition:
                    self.count += 1
                    if self.draining:
                        continue
                    valid = self.adaptive.context(current['metadata']) is not None if self.adaptive else metadata_valid(current['metadata'])
                    if valid:
                        self.valid += 1
                        if len(self.latest) == 2:
                            self.leases.release(self.latest.popleft())
                        self.latest.append(current)
                    else:
                        self.leases.release(current)
                        while self.latest:
                            self.leases.release(self.latest.popleft())
                    self.condition.notify_all()
        except BaseException as exc:
            # Keep protocol/cache errors even if shutdown coincides with them.
            self.fail(exc)

    def read_frames(self):
        try:
            pending = bytearray()
            while not self.io_stop.is_set():
                if not select.select([self.fd], [], [], .2)[0]:
                    continue
                chunk = os.read(self.fd, min(1<<20, SIZE-len(pending)))
                if not chunk:
                    continue
                pending.extend(chunk)
                if len(pending) == SIZE:
                    with self.condition:
                        # Bound memory while preserving byte/metadata ordering.
                        while len(self.raw) >= 4 and not self.io_stop.is_set():
                            self.condition.wait(.2)
                        self.raw.append(bytes(pending))
                        self.condition.notify_all()
                    pending.clear()
        except BaseException as e:
            if not self.stop.is_set():
                self.fail(e)

    def read_metadata(self):
        current = None
        try:
            with (self.out/'capture.log').open('w', buffering=1) as log:
                for line in self.process.stdout:
                    log.write(line)
                    match = re.match(r'(\d+)\.(\d+) .* seq: (\d+) bytesused: (\d+)/(\d+)', line)
                    if match:
                        with self.condition:
                            if current is not None:
                                self.count += 1
                                valid = (self.adaptive.context(current['metadata']) is not None) if self.adaptive else metadata_valid(current['metadata'])
                                if valid:
                                    self.valid += 1
                                    self.latest.append(current)
                                    self.condition.notify_all()
                                else:
                                    self.latest.clear()
                            while not self.raw and not self.io_stop.is_set():
                                self.condition.wait(.2)
                            if self.io_stop.is_set():
                                return
                            frame = self.raw.popleft()
                            self.condition.notify_all()
                        seconds, fraction, sequence, y, uv = map(int, match.groups())
                        if (y, uv) != (W*H, W*H//2):
                            raise ValueError('Unexpected capture plane sizes')
                        current = dict(sequence=sequence, timestamp_ns=seconds*10**9+fraction*1000,
                                       data=frame, metadata={})
                    elif current is not None and line.startswith('\t') and ' = ' in line:
                        key, value = line.strip().split(' = ', 1)
                        current['metadata'][key] = value
            if not self.stop.is_set():
                self.fail('Camera ended before the live trial finished')
        except BaseException as e:
            if not self.stop.is_set():
                self.fail(e)

    def ready(self, frame, after):
        return (frame['sequence'] >= FIRST_SEQUENCE and frame['sequence'] > after and
                0 <= time.monotonic_ns()-frame['timestamp_ns'] < FRESH_NS)

    def wait_ready(self, timeout=8):
        """Block until a usable frame has been captured; take nothing."""
        deadline = time.monotonic()+timeout
        with self.condition:
            while not self.stop.is_set():
                if self.errors:
                    raise RuntimeError('; '.join(self.errors))
                if any(self.ready(f, -1) for f in self.latest):
                    return True
                if time.monotonic() > deadline:
                    raise TimeoutError('No fresh frame with verified controls')
                self.condition.wait(.1)
        return False

    def take(self, after, timeout=8):
        """Remove and return the newest fresh valid frame newer than `after`.

        With leases the returned frame stays leased until release(); the
        reader never releases a taken frame. Returns None when stopping.
        """
        deadline = time.monotonic()+timeout
        with self.condition:
            while not self.stop.is_set():
                if self.errors:
                    raise RuntimeError('; '.join(self.errors))
                if self.latest and self.ready(self.latest[-1], after):
                    frame = self.latest.pop()
                    # Older retained frames are superseded: release them now.
                    # The reader only retires entries while it holds two, so
                    # anything left here would otherwise stay leased.
                    while self.latest:
                        old = self.latest.popleft()
                        if self.leases:
                            self.leases.release(old)
                    return frame
                if time.monotonic() > deadline:
                    raise TimeoutError('No fresh frame with verified controls')
                self.condition.wait(.05)
        return None

    def release(self, frame):
        if self.leases:
            with self.condition:
                self.leases.release(frame)

    @contextmanager
    def planes(self, frame):
        """(Y, UV) views of a taken frame: in place for leases, owned bytes for the FIFO."""
        if self.leases:
            with self.leases.view(frame) as views:
                yield views
        else:
            view = memoryview(frame['data'])
            yield view[:W*H], view[W*H:]

    def verify_lease(self):
        """Hold one real buffer while other slots circulate, then compare all bytes."""
        if not self.leases:
            raise ValueError('Lease verification requires the lease capture transport')
        deadline = time.monotonic()+8
        with self.condition:
            while len(self.latest) != 2:
                if self.errors:
                    raise RuntimeError('; '.join(self.errors))
                if time.monotonic() > deadline or self.stop.is_set():
                    raise TimeoutError('No pair for held-buffer verification')
                self.condition.wait(.1)
            held = self.latest.pop()
            before = self.leases.copy(held)
            count = self.count
        began = time.monotonic()
        # The reader keeps recycling other slots; this one is absent from its
        # latest deque and cannot be acknowledged until the second read ends.
        self.stop.wait(.4)
        with self.condition:
            after = self.leases.copy(held)
            progressed = self.count-count
            if before != after or progressed < 4:
                raise RuntimeError('Held capture buffer changed or other slots did not progress')
            self.leases.release(held)
        return dict(sequence=held['sequence'], id=held['id'], bytes_compared=len(before),
                    unchanged=True, other_frames=progressed, held_ms=(time.monotonic()-began)*1000,
                    sha256=hashlib.sha256(before).hexdigest())

    def close(self):
        with self.condition:
            self.draining = True
        stop_process(self.process,camera=True)
        self.io_stop.set()
        with self.condition:
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(2)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Capture reader failed to stop; mapped buffers remain pinned until process exit")
        self.process.stdout.close()
        self.errlog.close()
        if self.leases:
            self.lease_summary = self.leases.summary()
            self.leases.close()
            if self.errors or self.lease_summary['footer'] is None:
                raise RuntimeError('Lease capture cleanup: '+ '; '.join(self.errors or ['missing footer']))
        else:
            os.close(self.fd)
            self.fifo.unlink()


class PipePublisher:
    """The earlier stdin transport, kept for comparison."""
    def __init__(self, process):
        self.process = process
        os.set_blocking(process.stdin.fileno(), False)

    def send(self, frame, sequence, timestamp, stop):
        view = memoryview(struct.pack('<QQ', sequence, timestamp)+frame)
        deadline = time.monotonic()+3
        while view:
            if stop.is_set():
                raise InterruptedError('Pipe handoff stopped')
            if self.process.poll() is not None:
                raise RuntimeError(f'Publisher exited {self.process.returncode}')
            if time.monotonic() > deadline:
                raise TimeoutError('Publisher stopped reading frames')
            if select.select([], [self.process.stdin], [], .1)[1]:
                try:
                    n = os.write(self.process.stdin.fileno(), view)
                    view = view[n:]
                except BlockingIOError:
                    pass
        return None

    def close(self):
        self.process.stdin.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--programs', type=Path, default=ASSETS/'anchor')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--frames', type=int, default=0, help='Stop after this many published frames; zero uses time limit')
    p.add_argument('--seconds', type=int, default=300)
    p.add_argument('--publish', action='store_true')
    p.add_argument('--handoff-fds', help='MEMFD,CONTROL descriptors from an already running publisher (daemon mode)')
    p.add_argument('--readback', choices=('read', 'mmap', 'verify'), default='mmap')
    p.add_argument('--verify-capture-lease', action='store_true')
    p.add_argument('--capture-transport', choices=('fifo', 'lease'), default='lease')
    p.add_argument('--transport', choices=('pipe', 'shared'), default='shared')
    p.add_argument('--system-pipewire', action='store_true', help='Use the installed bridge for comparison')
    p.add_argument('--adaptive', action='store_true', help='Use paired RAW AE/AWB, gain bank and bounded display-luma tone feedback')
    p.add_argument('--centre-priority', action='store_true', help='Protect central highlights while tolerating limited peripheral clipping')
    p.add_argument('--slow-frame', type=float, default=2.0,
                   help='Longest frame in default frame lengths once gain passes the knee (2.0 allows 1/15 s; 1.0 keeps the nominal 30 fps sensor budget)')
    p.add_argument('--reference-awb', action='store_true',
                   help='Use the earlier session-reference AWB instead of the Windows illuminant locus')
    p.add_argument('--awb-warmth', type=float, default=0.16, help='Red share of a warm light kept below 4500 K (locus AWB)')
    p.add_argument('--awb-warmth-blue', type=float, default=0.14, help='Blue share kept; smaller because the colour matrix amplifies blue changes')
    p.add_argument('--awb-warmth-table', default='2300:0.16:0.14,3250:0.12:0.47,4500:0.0:0.6',
                   help='cct:red:blue points, linearly interpolated, overriding the scalar warmth ramp (fitted to the Windows samples; empty = scalar)')
    p.add_argument('--colour-sets', type=Path, default=ASSETS/'colour-sets',
                   help='post-LTM colour programs per CCT and gain band (prepare-colour-sets.py); the AWB gains select the CCT')
    p.add_argument('--cct-sets', action='store_true',
                   help='Experimental: select the post-LTM colour program by estimated CCT (the 2300-2600 K sets oversaturated warm colours in the first comparison)')
    p.add_argument('--raw-target', type=float, default=110, help='RAW AE luma target (profile ae.target); lower exposes less and leaves more to the tone mapper')
    p.add_argument('--luma-target', type=float, default=145, help='Display luma target of the tone controller')
    p.add_argument('--slow-frame-gain', type=float, default=8.0, help='Analogue gain reached at one frame length before the frame grows to slow_frame')
    p.add_argument('--highlight-peak', type=float, default=0.95, help='RAW highlight ceiling: 99.5th percentile target as a fraction of full scale')
    p.add_argument('--peripheral-clipping', type=float, default=0.05, help='Whole-frame RAW clipping budget tolerated with centre priority')
    p.add_argument('--centre-clipping', type=float, default=0.02, help='Central RAW clipping budget tolerated with centre priority')
    p.add_argument('--centre-percentile', type=float, default=0.95, help='Central percentile the highlight peak target applies to (lower lets the brightest few percent approach clipping)')
    p.add_argument('--initial-drc', type=float, help='Start the tone controller at this DRC gain instead of 1.5 (manual-exposure trials hold it there)')
    p.add_argument('--bank-gain', choices=('analogue', 'total', 'geometric'), default='total',
                   help='Select denoise programs on analogue x digital gain (default; measured 2026-10-17 in a 1.7 lux room), on analogue gain only (keeps texture under digital gain, noisier), or on analogue x sqrt(digital)')
    p.add_argument('--sensor-digital-max', type=float, default=3.0,
                   help='Sensor digital gain the RAW AE may add after analogue gain is exhausted (1.0 = none; 3 balances brightness against chroma noise)')
    p.add_argument('--save-raw', action='store_true', help='Retain RAW requests 58/59 for statistics validation')
    p.add_argument('--save-every', type=int, default=0, help='Also save every Nth published output for inspection')
    p.add_argument('--save-switches', type=int, default=0, help='Save the first output after each of the first N program switches')
    p.add_argument('--reseed-on-program-switch', action='store_true',
                   help='Run a seed pass after every program switch (the earlier behaviour) instead of keeping temporal history')
    p.add_argument('--bank', type=Path, default=ASSETS/BOARD['program_bank'],
                   help='Program bank (default: DRC to 8, ASF 0.75, ANR one band lighter from gain row 12; bank-drc8-asf075 is the previous default, bank-drc8 ASF 0.5)')
    p.add_argument('--control-script', type=Path, help='Explicit cam control script for transition validation')
    p.add_argument('--stop-file', type=Path, help='Stop cleanly when the unprivileged launcher creates this file')
    p.add_argument('--state-file', type=Path,
                   help='Start from the exposure/gain/white balance this file recorded at the end of the last session (daemon warm start)')
    p.add_argument('--retain-frames', type=int, default=0,
                   help='Keep only the newest N per-frame records in memory/report (0 = all; daemon sessions use a bound)')
    p.add_argument('--quiet', action='store_true', help='Log every 300th frame and no per-command HFI lines (daemon sessions)')
    a = p.parse_args()
    if not 0 <= a.seconds <= 7200 or not 0 <= a.frames <= 300000 or a.save_every < 0 or a.save_switches < 0:
        raise ValueError('Trial duration must be 0..7200 seconds (0 = until stopped) and frame limit 0..300000')
    external = None
    if a.handoff_fds:
        try:
            external = tuple(int(x) for x in a.handoff_fds.split(','))
            assert len(external) == 2 and all(x > 2 for x in external)
        except (ValueError, AssertionError):
            raise ValueError('--handoff-fds needs two descriptor numbers') from None
        if not a.publish or a.transport != 'shared':
            raise ValueError('--handoff-fds requires --publish and the shared transport')
    if a.verify_capture_lease and a.capture_transport != 'lease':
        raise ValueError('Lease verification requires --capture-transport lease')
    if a.stop_file and a.stop_file.exists():
        raise ValueError('Stop file already exists')
    if (a.centre_priority or a.save_raw) and not a.adaptive:
        raise ValueError('Centre priority and RAW validation require adaptive capture')
    adaptive = module('ipe_adaptive', 'ipe-adaptive.py') if a.adaptive else None
    bank = adaptive.Bank(a.bank) if adaptive else None
    tone = adaptive.Tone(target=a.luma_target, max_gain=bank.max_drc) if adaptive else None
    if a.initial_drc is not None:
        if not adaptive or not 1 <= a.initial_drc <= bank.max_drc:
            raise ValueError('--initial-drc needs --adaptive and 1..bank DRC limit')
        tone.gain = a.initial_drc
    colour = locus = None
    if adaptive and a.cct_sets and (a.colour_sets/'colour-sets.json').exists():
        colour = adaptive.ColourSets(a.colour_sets)
        locus = adaptive.Locus(json.loads((ASSETS/BOARD['awb_reference']).read_text())['references'])
    striping_capacity = max(e['files'][n]['size'] for e in bank.entries.values()
                            for n in ('striping.bin','seed-striping.bin')) if bank else 0
    if bank:
        a.programs,_ = bank.select(CONTROLS['AnalogueGain'],tone.gain)
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    profile = (ASSETS/BOARD['ife_tuning']).read_text()
    if a.centre_priority:
        profile = profile.replace('  raw_highlights: true','  raw_highlights: true\n  raw_centre_priority: true')
    warmth_table = []
    for item in filter(None, a.awb_warmth_table.split(',')):
        cct, red, blue = (float(x) for x in item.split(':'))
        if not 1000 <= cct <= 20000 or not 0 <= red <= 1 or not 0 <= blue <= 1:
            raise ValueError('Invalid warmth table point')
        warmth_table += [cct, red, blue]
    if not 1.0 <= a.slow_frame <= 2.0 or not 0 <= a.awb_warmth <= 1 or not 0 <= a.awb_warmth_blue <= 1:
        raise ValueError('--slow-frame must be 1.0..2.0 and --awb-warmth 0..1')
    # Exposure order: shutter to one frame, gain to the configured knee,
    # then optional frame extension before gain climbs further.
    if not 40 <= a.raw_target <= 200 or not 60 <= a.luma_target <= 220 or not 1 <= a.slow_frame_gain <= 16:
        raise ValueError('Targets or slow-frame gain out of range')
    profile = profile.replace('  slow_frame: 2.0', f'  slow_frame: {a.slow_frame}\n  slow_frame_gain: {a.slow_frame_gain}')
    profile = profile.replace('  target: 110\n', f'  target: {a.raw_target}\n', 1)
    if not 0.5 <= a.highlight_peak <= 0.98 or not 0.001 <= a.peripheral_clipping <= 0.05 or not 0.0005 <= a.centre_clipping <= 0.02 or not 0.9 <= a.centre_percentile <= 0.999:
        raise ValueError('Highlight parameters out of range')
    profile = profile.replace('  raw_highlights: true', f'  highlight_peak: {a.highlight_peak}\n  peripheral_clipping: {a.peripheral_clipping}\n  centre_clipping: {a.centre_clipping}\n  centre_percentile: {a.centre_percentile}\n  raw_highlights: true', 1)
    if not 1.0 <= a.sensor_digital_max <= 8.0:
        raise ValueError('--sensor-digital-max must be 1.0..8.0')
    profile = profile.replace('  sensor_digital_max: 1.0', f'  sensor_digital_max: {a.sensor_digital_max}')
    if adaptive and not a.reference_awb:
        # Windows AWB neutral references for this sensor (parity/awb-reference),
        # anchored to the measured paper white point at 5000 K.
        refs = json.loads((ASSETS/BOARD['awb_reference']).read_text())['references']
        # Horizon (2300 K) to D75 (7500 K): the 1500 K and 12000 K extremes
        # add long segments that sweep across ordinary object colours.
        flat = ', '.join(f"{r['nominal_cct']}, {r['rg']:.6f}, {r['bg']:.6f}" for r in refs if 2300 <= r['nominal_cct'] <= 7500)
        profile = profile.replace('  raw_reference_gains:', f'  locus: [{flat}]\n  locus_width: 0.12\n  locus_anchor_cct: 5000\n'
                                  f'  locus_warmth: {a.awb_warmth}\n  locus_warmth_blue: {a.awb_warmth_blue}\n  locus_warm_cct: 4500\n  min_green: 16\n'
                                  + (f'  locus_warmth_table: [{", ".join(str(x) for x in warmth_table)}]\n' if warmth_table else '')
                                  + '  raw_reference_gains:')
    if not adaptive:
        profile = profile.replace('  raw_highlights: true', '  raw_highlights: false')
        profile = re.sub(r'^  raw_reference_gains:.*\n', '', profile, flags=re.M)
    (out/'ife.yaml').write_text(profile)
    initial_controls = dict(CONTROLS, AeEnable=bool(adaptive),AwbEnable=bool(adaptive))
    warm_start = None
    if a.state_file and adaptive and a.state_file.exists():
        # Same room, same light most of the time: begin where the last
        # session converged instead of ramping from the daylight defaults.
        try:
            saved = json.loads(a.state_file.read_text())
            age_h = (time.time() - saved['saved_at']) / 3600
            exposure, gain, wb = int(saved['ExposureTime']), float(saved['AnalogueGain']), [float(x) for x in saved['ColourGains']]
            if age_h < 24 and 200 <= exposure <= 67000 and 1 <= gain <= 16 and all(0.25 <= x <= 4 for x in wb):
                exposure, gain = limit_start_exposure(exposure, gain, a.slow_frame)
                initial_controls.update(ExposureTime=exposure, AnalogueGain=gain, ColourGains=wb)
                warm_start = dict(saved, age_hours=age_h)
                drc = float(saved.get('drc', tone.gain))
                if a.initial_drc is None and math.isfinite(drc) and 1 <= drc <= bank.max_drc:
                    tone.gain = drc   # the display luma otherwise ramps from DRC 1.5 every session
                bank.gain_index = bank.tone_index = None   # fresh selection, no dwell against the default seed
                a.programs,_ = bank.select(initial_controls['AnalogueGain'], tone.gain)   # seed with the restored programs
        except (ValueError, KeyError, OSError):
            warm_start = None
    (out/'controls.yaml').write_text(a.control_script.read_text() if a.control_script else
                                   json.dumps({'frames': [{0: initial_controls}]}, indent=2)+'\n')
    media_device, sensor = isp_config.sensor(BOARD)
    controls = run('v4l2-ctl', '-d', sensor, '--get-ctrl=exposure,vertical_blanking,analogue_gain,digital_gain')
    initial = {k:int(v) for k,v in re.findall(r'(\w+): (\d+)', controls)}
    stream = module('ipe_stream', 'ipe-stream.py')
    if a.quiet:
        stream.icplab.VERBOSE = False
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    session = capture = publisher = publish_log = handoff = transport = None
    prep_pool = read_pool = None
    report = dict(capture_transport=a.capture_transport, readback=a.readback, transport=a.transport, system_pipewire=a.system_pipewire,
                  initial_controls=initial_controls, frames=deque(maxlen=a.retain_frames or None), seeds=[], program_switches=[],
                  published_total=0, retain_frames=a.retain_frames, completed=False, adaptive=bool(adaptive),
                  centre_priority=a.centre_priority, history='continuous', pipeline='prepare/firmware/readback overlapped',
                  reseed_on_program_switch=a.reseed_on_program_switch, slow_frame=a.slow_frame, sensor_digital_max=a.sensor_digital_max,
                  warm_start=warm_start, bank_gain=a.bank_gain,
                  colour_sets=str(a.colour_sets.resolve()) if colour else None,
                  raw_target=a.raw_target, luma_target=a.luma_target, slow_frame_gain=a.slow_frame_gain,
                  highlight_peak=a.highlight_peak, peripheral_clipping=a.peripheral_clipping, centre_clipping=a.centre_clipping, centre_percentile=a.centre_percentile,
                  awb=('session-reference' if a.reference_awb or not adaptive else f'illuminant-locus warmth table {a.awb_warmth_table or f"{a.awb_warmth}/{a.awb_warmth_blue}"}'),
                  policy=('Paired RAW AE/AWB; quantized gain bank and bounded display-luma tone feedback' if adaptive else
                          'Fixed validated controls') + '; continuous temporal history, seed at start' +
                         (' and after each program switch' if a.reseed_on_program_switch else '; program switches keep history'),
                  bank=str(a.bank.resolve()) if bank else None)
    report_lock = threading.Lock()
    published = []   # sequences in publication order

    try:
        session = stream.Session(a.programs,striping_capacity=striping_capacity,readback=a.readback,retain_times=a.retain_frames)
        capture = Capture(out, stop, adaptive, a.save_raw, a.capture_transport)
        if a.verify_capture_lease:
            report['held_capture_buffer'] = capture.verify_lease()
        if a.publish and external:
            # Daemon mode: the publisher already owns the PipeWire node and
            # created the shared frame; adopt, seal and announce readiness.
            if not capture.wait_ready():
                raise RuntimeError('Trial stopped before camera startup completed')
            handoff = transport = module('ipe_handoff', 'ipe-handoff.py').Producer.from_fds(*external)
            print('PUBLISHER READY', flush=True)   # the daemon reports started on this line
        elif a.publish:
            # The physical camera is already owned before desktop discovery resumes.
            if not capture.wait_ready():
                raise RuntimeError('Trial stopped before camera startup completed')
            subprocess.run(as_user('systemctl', '--user', 'start', 'wireplumber'), check=True, timeout=15)
            publish_log = (out/'publisher.log').open('w')
            if a.transport == 'shared':
                handoff = module('ipe_handoff', 'ipe-handoff.py').Producer()
            publisher = subprocess.Popen(as_user('python3', '-I', HERE/'ipe-pipewire.py',
                *(handoff.child_args() if handoff else []),
                *(['--system-pipewire'] if a.system_pipewire else [])),
                pass_fds=handoff.child_fds() if handoff else (),
                stdin=subprocess.DEVNULL if handoff else subprocess.PIPE,
                stdout=publish_log, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True)
            if handoff:
                handoff.child_started()
                transport = handoff
            else:
                transport = PipePublisher(publisher)
        started = time.monotonic()
        prep_pool = ThreadPoolExecutor(1, 'prepare')
        read_pool = ThreadPoolExecutor(1, 'readback')

        def prepare_next(slot, after):
            """Stage 1: take the newest capture frame and write it into a free slot."""
            frame = capture.take(after)
            if frame is None:
                return None
            began = time.monotonic()
            try:
                with capture.planes(frame) as (y, uv):
                    prepared = session.prepare(slot, planes=(y, uv))
                    input_luma = adaptive.luma(y) if adaptive else None
            finally:
                capture.release(frame)
            frame = {k: v for k, v in frame.items() if k != 'data'}
            return dict(slot=slot, frame=frame, prepared=prepared, input_luma=input_luma,
                        taken_ns=time.monotonic_ns(), stage_ms=(time.monotonic()-began)*1000)

        def finish(token, item, selection, reference):
            """Stage 3: read the output, publish it, record the frame."""
            frame_meta = item['frame']
            began = time.monotonic()
            data = session.readback(token, height=H)
            read_ms = (time.monotonic()-began)*1000
            count = report['published_total']+1
            if count == 1:
                (out/'first-output.nv12').write_bytes(data)
            if a.save_every and count % a.save_every == 0:
                (out/f'output-{count:05d}-seq{frame_meta["sequence"]}.nv12').write_bytes(data)
            if item.get('save_switch'):
                (out/f'switch-{item["save_switch"]:02d}-seq{frame_meta["sequence"]}.nv12').write_bytes(data)
            publish_started = time.monotonic_ns()
            accepted_ns = None
            if transport:
                try:
                    if external and getattr(transport, 'failed', False):
                        raise EOFError('transport already closed')
                    accepted_ns = transport.send(data, frame_meta['sequence'], frame_meta['timestamp_ns'], stop)
                except InterruptedError:
                    # Stop requested while waiting for the publisher's ACK: not an error.
                    if stop.is_set():
                        session.complete(token)
                        return None
                    raise
                except (EOFError, BrokenPipeError, ConnectionError, TimeoutError, RuntimeError) as exc:
                    if external:
                        # The persistent publisher went away or stopped reading:
                        # end this session cleanly instead of failing it.
                        report['stop_reason'] = f'publisher: {exc!r}'
                        stop.set()
                        session.complete(token)
                        return None
                    raise
            now = time.monotonic_ns()
            age_ms = (now-frame_meta['timestamp_ns'])/1e6
            publish_ms = (now-publish_started)/1e6
            stats = {}
            if adaptive:
                ctx = adaptive.context(frame_meta['metadata'])
                output_luma = adaptive.luma(data)
                reason = tone.observe(ctx, output_luma['weighted_mean'], age_ms)
                stats = dict(program=selection, input_luma=item['input_luma'], output_luma=output_luma,
                             tone_reason=reason, next_drc=tone.gain)
            session.complete(token)
            times = token['times']
            with report_lock:
                published.append(frame_meta['sequence'])
                if a.retain_frames and len(published) > a.retain_frames:
                    del published[:-a.retain_frames]
                report['published_total'] += 1
                report['frames'].append(dict(sequence=frame_meta['sequence'], reference_sequence=reference,
                    reseeded=False, metadata=frame_meta['metadata'],
                    elapsed_ms=(now-item['taken_ns'])/1e6 + item['stage_ms'],
                    stage_ms=dict(prepare=times.get('prepare_ms'), firmware=times.get('firmware_ms'),
                                  readback=read_ms, publish=publish_ms),
                    age_ms=age_ms, publish_ms=publish_ms,
                    accepted_age_ms=(accepted_ns-frame_meta['timestamp_ns'])/1e6 if accepted_ns else None, **stats))
            if not a.quiet or count == 1 or count % 300 == 0:
                print(f'LIVE frame={count} sensor={frame_meta["sequence"]} reference={reference} age_ms={age_ms:.1f}', flush=True)
            return data

        def drain(reads):
            while reads:
                reads.popleft().result()

        reads = deque()
        last_output = -1          # sequence of the frame whose output is the current TF reference
        previous = -1             # newest capture sequence handed to the pipeline
        last_data = None
        pending = prep_pool.submit(prepare_next, 0, previous)
        while not stop.is_set() and (not a.seconds or time.monotonic()-started < a.seconds):
            if a.stop_file and a.stop_file.exists():
                break
            item = pending.result()
            if item is None:
                break
            slot, meta = item['slot'], item['frame']
            selection = None
            if adaptive:
                ctx = adaptive.context(meta['metadata'])
                bank_gain = {'analogue': ctx['analogue_gain'], 'total': ctx['gain'],
                             'geometric': ctx['analogue_gain'] * math.sqrt(ctx['digital_gain'])}[a.bank_gain]
                programs, selection = bank.select(bank_gain, tone.gain)
                overrides = None
                if colour is not None:
                    cct, distance = locus.cct_from_gains(ctx['wb'])
                    post, colour_selection = colour.select(cct, bank.last_gain_index)
                    colour_selection['locus_distance'] = distance
                    selection = dict(selection, colour=colour_selection)
                    overrides = {'post.bin': post}
                if programs != session.p or {k: Path(v).resolve() for k, v in (overrides or {}).items()} != session.overrides:
                    drain(reads)
                    was_full = session.full
                    session.select_programs(programs, keep_history=not a.reseed_on_program_switch, overrides=overrides)
                    report['program_switches'].append(dict(sequence=meta['sequence'], program=selection,
                                                           kept_history=bool(session.full and was_full)))
                    if a.save_switches and len(report['program_switches']) <= a.save_switches and session.full:
                        item['save_switch'] = len(report['program_switches'])
            previous = meta['sequence']
            # Stage 1 for the other slot overlaps this frame's firmware pass.
            pending = prep_pool.submit(prepare_next, 1-slot, previous)
            token = session.submit(slot, item['prepared'])
            if token['seed']:
                # Not published: this pass only creates the temporal reference.
                drain(reads)
                session.complete(token)
                report['seeds'].append(dict(sequence=meta['sequence'], ipe_sequence=token['sequence'],
                                            program=selection, reason='start' if last_output < 0 else 'program-switch'))
                last_output = meta['sequence']
                continue
            reads.append(read_pool.submit(finish, token, item, selection, last_output))
            last_output = meta['sequence']
            if len(reads) > 1:
                last_data = reads.popleft().result()
            if a.frames and report['published_total'] + len(reads) >= a.frames:
                break
        stop_prepare = pending
        # Finish outstanding stages in order before anything is torn down.
        while reads:
            last_data = reads.popleft().result()
        try:
            stop_item = stop_prepare.result(timeout=1)
        except BaseException:
            stop_item = None
        if last_data is not None:
            (out/'last-output.nv12').write_bytes(last_data)
        if a.state_file and adaptive and len(report['frames']) >= 60:
            recent = list(report['frames'])[-30:]
            if all(f['tone_reason'] in ('within-luma-deadband', 'tone-limit', 'bounded-display-luma-feedback') for f in recent[-5:]):
                m = recent[-1]['metadata']
                try:
                    a.state_file.parent.mkdir(parents=True, exist_ok=True)
                    a.state_file.write_text(json.dumps(dict(saved_at=time.time(), ExposureTime=int(m['ExposureTime']),
                        AnalogueGain=float(m['AnalogueGain']), DigitalGain=float(m['DigitalGain']),
                        ColourGains=[float(x) for x in m['ColourGains'].strip('[] ').split(',')],
                        drc=recent[-1]['next_drc'], program=recent[-1]['program']['id'])) + '\n')
                    report['state_saved'] = str(a.state_file)
                except OSError as exc:
                    report['state_saved'] = f'failed: {exc!r}'
        report.update(completed=True, elapsed_s=time.monotonic()-started, capture_frames=capture.count,
                      valid_capture_frames=capture.valid, ipe_frames=session.sequence,
                      configure_calls=session.config_count, allocations=len(session.buffers), timings=list(session.times),
                      discarded_prepared_frame=stop_item['frame']['sequence'] if stop_item else None)
        report['frames'] = list(report['frames'])
        report['output_fps'] = report['published_total']/report['elapsed_s']
        if a.retain_frames:
            report['timings'] = report['timings'][-a.retain_frames:]
        if not report['frames']:
            raise RuntimeError('No live IPE frames were produced')
    except BaseException as e:
        report.update(completed=False, error=repr(e))
        raise
    finally:
        stop.set()
        cleanup_errors = []
        actions = []
        for pool in (prep_pool, read_pool):
            if pool:
                actions.append(lambda pool=pool: pool.shutdown(wait=True, cancel_futures=True))
        if capture:
            actions.append(capture.close)
        if publisher:
            actions.extend([transport.close, lambda: finish_publisher(publisher)])
        elif handoff:
            actions.append(handoff.close)
        if external:
            report['daemon_mode'] = True
        if publish_log:
            actions.append(publish_log.close)
        if session:
            actions.append(session.close)
        for action in actions:
            try:
                action()
            except BaseException as e:
                cleanup_errors.append(repr(e))
        try:
            run('media-ctl', '-d', media_device, '-r')
            run('v4l2-ctl', '-d', sensor, '--set-ctrl=exposure=8')
            run('v4l2-ctl', '-d', sensor, '--set-ctrl=vertical_blanking='+str(initial['vertical_blanking']))
            run('v4l2-ctl', '-d', sensor, '--set-ctrl='+','.join(f'{k}={initial[k]}' for k in ('exposure','analogue_gain','digital_gain')))
            restored = run('v4l2-ctl', '-d', sensor, '--get-ctrl=exposure,vertical_blanking,analogue_gain,digital_gain')
            report['sensor_controls_restored'] = initial == {k:int(v) for k,v in re.findall(r'(\w+): (\d+)', restored)}
            if not report['sensor_controls_restored']:
                raise RuntimeError('Sensor control restoration did not match')
        except BaseException as e:
            cleanup_errors.append(repr(e))
        if capture and capture.lease_summary:
            report['capture_lease'] = capture.lease_summary
        report['cleanup_errors'] = cleanup_errors
        if publisher:
            report['publisher_exit'] = publisher.poll()
        if cleanup_errors:
            report['completed'] = False
        # Retained frames are a deque; an early failure reaches here before the success path lists them.
        (out/'report.json').write_text(json.dumps(report, indent=2, default=list)+'\n')
        if cleanup_errors:
            raise RuntimeError('Live cleanup failed: '+'; '.join(cleanup_errors))


if __name__ == '__main__':
    main()
