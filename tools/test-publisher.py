#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Exercise the publisher on the current PipeWire server using synthetic NV12.

No camera device or privileged worker is opened. Requires GStreamer/PyGObject,
pw-dump, gst-launch-1.0 and a freshly built private bridge. Writes diagnostic
logs to --output. This is an explicit desktop integration check, not a makepkg
check(): a package builder need not have a running PipeWire session.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
RGBA = 'video/x-raw,format=RGBA'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    bridge = args.bridge.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    profile = output / 'profile.json'
    profile.write_text(json.dumps(dict(schema=1, width=3840, height=2160,
                                      padded_height=2176, description='ISP synthetic test',
                                      node_name='isp-test', nickname='ISP synthetic test')))
    config = output / 'config.json'
    config.write_text(json.dumps(dict(profile=str(profile), assets=str(output),
                                     cam='/nonexistent', cam_library_path=str(output), bridge=str(bridge))))
    env = dict(os.environ, QUALCOMM_ISP_CONFIG=str(config),
               GST_DEBUG=os.environ.get('GST_DEBUG', 'pipewiresink:4,pipewirepool:3'), GST_DEBUG_NO_COLOR='1')
    node = f'isp-test-{os.getpid()}'
    command = [sys.executable, '-I', str(ROOT / 'runtime/ipe-pipewire.py'), '--node-name', node]

    def snapshot():
        objects = json.loads(subprocess.check_output(['pw-dump'], timeout=5))
        return [o for o in objects if o.get('info', {}).get('props', {}).get('node.name') == node]

    def until(condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.1)
        raise AssertionError('Timed out waiting for PipeWire node state')

    with (output / 'publisher.log').open('w') as log:
        publisher = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env)
    stop = threading.Event()
    children = []
    producer_errors = []

    def generate():
        frame = bytes([96]) * (3840 * 2160) + bytes([128]) * (3840 * 2160 // 2)
        seq = 0
        try:
            while not stop.is_set():
                seq += 1
                publisher.stdin.write(struct.pack('<QQ', seq, time.monotonic_ns()))
                publisher.stdin.write(frame)
                publisher.stdin.flush()
                stop.wait(1 / 30)
        except BrokenPipeError:
            if not stop.is_set():
                producer_errors.append('Publisher closed its input early')

    producer = threading.Thread(target=generate, daemon=True)
    producer.start()

    def start(name, caps, count=45, dest=None):
        assert publisher.poll() is None, 'Publisher exited before ' + name
        # Explicitly disable fallback so a vanished test node cannot cause a
        # false PASS by opening the user's real default camera instead.
        cmd = ['gst-launch-1.0', '-v', 'pipewiresrc', 'target-object=' + node,
               'stream-properties=props,node.dont-fallback=true', 'num-buffers=' + str(count), '!', caps, '!']
        cmd += ['filesink', 'location=' + str(dest)] if dest else ['fakesink', 'sync=false']
        with (output / (name + '.log')).open('w') as log:
            p = subprocess.Popen(cmd, stdout=log, stderr=log)
        children.append(p)
        return p

    def finish(name, proc, size=None, reject=False):
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            raise AssertionError(name + ' stalled')
        assert publisher.poll() is None, 'Publisher exited during ' + name
        text = (output / (name + '.log')).read_text()
        if reject:
            assert rc != 0 and 'no more input formats' in text, (name, rc, text)
        else:
            assert rc == 0, (name, rc, text)
            assert 'CRITICAL' not in text and 'ERROR' not in text, text
            if size:
                assert f'width=(int){size[0]}, height=(int){size[1]}' in text, text
        assert len(snapshot()) == 1, 'Test node disappeared'
        print('PASS', name, flush=True)

    def idle():
        until(lambda: bool(snapshot()) and not snapshot()[0]['info']['params'].get('Format'))

    try:
        until(lambda: bool(snapshot()))
        serial = snapshot()[0]['info']['props']['object.serial']
        formats = snapshot()[0]['info']['params']['EnumFormat']
        assert [(f['size']['width'], f['size']['height']) for f in formats] == [
            (3840, 2160), (1920, 1080), (1280, 720), (640, 360)]
        finish('unsupported-idle', start('unsupported-idle', RGBA + ',width=1024,height=768'), reject=True)
        finish('1080-capped', start('1080-capped', RGBA + ',width=[320,1920],height=[240,1080]'), (1920, 1080))
        idle()
        # Two clients that accept the same size must both finish.
        a = start('concurrent-a', RGBA + ',width=1920,height=1080', 120)
        time.sleep(1)
        b = start('concurrent-b', RGBA + ',width=1920,height=1080')
        finish('concurrent-b', b, (1920, 1080))
        finish('concurrent-a', a, (1920, 1080))
        idle()
        for w, h in [(3840, 2160), (1280, 720), (640, 360), (1920, 1080), (3840, 2160)]:
            name = f'size-{w}-{h}'
            finish(name, start(name, RGBA + f',width={w},height={h}'), (w, h))
            idle()
        finish('native-default', start('native-default', RGBA), (3840, 2160))
        idle()
        dest = output / 'gray.rgba'
        finish('pixels', start('pixels', RGBA + ',width=640,height=360', 1, dest), (640, 360))
        pixels = dest.read_bytes()
        assert len(pixels) == 640 * 360 * 4, len(pixels)
        # Full-range gray stays gray, including alpha, after scaling/conversion.
        assert all(94 <= x <= 98 for x in pixels[0:3]), pixels[:4]
        assert pixels[3::4] == bytes([255]) * (640 * 360)
        assert snapshot()[0]['info']['props']['object.serial'] == serial, 'Node was replaced after a peer error'
        assert not producer_errors, producer_errors
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        stop.set()
        producer.join(timeout=2)
        if producer.is_alive():
            publisher.terminate()
        else:
            publisher.stdin.close()
        try:
            publisher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait()
    assert publisher.returncode == 0, publisher.returncode

    # A signal while stdin is open but idle must not leave a Python buffered
    # reader alive at interpreter shutdown (which would abort with SIGABRT).
    with (output / 'idle-stop.log').open('w') as log:
        p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env)
        try:
            time.sleep(1)
            p.terminate()
            assert p.wait(timeout=5) == 0, 'Idle stdin shutdown failed'
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdin.close()
    print('PASS idle stdin shutdown', flush=True)

    # Inject real Gst bus errors into a standby publisher. No peers link to
    # this node, so standby never contacts a worker or opens camera hardware.
    wrapper = output / 'inject.py'
    wrapper.write_text('''import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('publisher', sys.argv.pop(1))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
remaining = int(sys.argv.pop(1))
build = p.build_pipeline
def wrapped(name):
    global remaining
    parts = build(name)
    print('TEST build', flush=True)
    if remaining:
        remaining -= 1
        def inject():
            error = p.GLib.Error.new_literal(p.Gst.ResourceError.quark(), 'synthetic stream failure', int(p.Gst.ResourceError.FAILED))
            parts[0].post_message(p.Gst.Message.new_error(parts[0], error, 'test-publisher injection'))
            return False
        p.GLib.timeout_add(500, inject)
    else:
        def stop():
            os.kill(os.getpid(), signal.SIGTERM)
            return False
        p.GLib.timeout_add(500, stop)
    return parts
p.build_pipeline = wrapped
p.main()
''')
    for errors, rc, builds in [(1, 0, 2), (4, 1, 4)]:
        r = subprocess.run([sys.executable, '-I', str(wrapper), str(ROOT / 'runtime/ipe-pipewire.py'),
                            str(errors), '--standby', '--poll-seconds', '.1', '--node-name', node,
                            '--worker-socket', str(output / 'nonexistent-worker.sock')],
                           env=env, capture_output=True, text=True, timeout=15)
        text = r.stdout + r.stderr
        (output / f'rebuild-{errors}.log').write_text(text)
        assert r.returncode == rc, text
        assert text.count('TEST build') == builds, text
        assert text.count('error, rebuilding') == min(errors, 3), text
        assert 'starting worker' not in text, text
        print('PASS', 'single-error rebuild' if errors == 1 else 'rebuild rate limit', flush=True)
    print('All publisher integration checks passed; logs:', output, flush=True)


if __name__ == '__main__':
    main()
