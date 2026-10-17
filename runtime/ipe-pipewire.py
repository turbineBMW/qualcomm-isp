#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Publish NV12 from shared memory or stdin as an opt-in PipeWire camera.

Stdin format: <sequence:u64, capture_monotonic_ns:u64> then 3840x2160 NV12.
Upstream queues are bounded and discard old frames. The private PipeWire bridge
re-triggers starved video drivers; producer progress alone is not display proof.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst

W, H = 3840, 2160
SIZE = W * H * 3 // 2
NOMINAL_FPS = 30   # the pipelined worker publishes at the sensor cadence
TRANSPORT_BUFFERS = 8
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isp_config
CONFIG, BOARD = isp_config.load()
PRIVATE_BRIDGE = Path(CONFIG['bridge'])


def configure_transport_pool(sink):
    # 1.6.9 exposes its pool through an allocation query and preserves its
    # configured bounds in setcaps/render. Eight 4K RGBA buffers use ~253 MiB;
    # the implicit default of sixteen uses ~506 MiB. Keep headroom for
    # Snapshot's live preview and photo branches. Keep upstream allocation
    # separate so a missing/slow consumer cannot stop the stdin reader.
    caps = Gst.Caps.from_string(
        f'video/x-raw,format=RGBA,width={W},height={H},framerate={NOMINAL_FPS}/1')
    sink.set_property('use-bufferpool', True)
    try:
        query = Gst.Query.new_allocation(caps, True)
        if not sink.get_static_pad('sink').query(query) or query.get_n_allocation_pools() != 1:
            raise RuntimeError('PipeWire did not expose its transport pool')
        pool, _, _, _ = query.parse_nth_allocation_pool(0)
        config = pool.get_config()
        Gst.BufferPool.config_set_params(config, caps, W * H * 4, 2, TRANSPORT_BUFFERS)
        if not pool.set_config(config):
            raise RuntimeError('Could not bound the PipeWire transport pool')
        ok, _, size, minimum, maximum = Gst.BufferPool.config_get_params(pool.get_config())
        if not ok or (size, minimum, maximum) != (W * H * 4, 2, TRANSPORT_BUFFERS):
            raise RuntimeError('Unexpected PipeWire pool configuration')
        print(f'PipeWire transport pool: 2..{TRANSPORT_BUFFERS} native-4K RGBA buffers', flush=True)
    finally:
        sink.set_property('use-bufferpool', False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-name', default=BOARD['node_name'])
    parser.add_argument('--shared-fd', type=int)
    parser.add_argument('--control-fd', type=int)
    bridge = parser.add_mutually_exclusive_group()
    bridge.add_argument('--pipewire-plugin', type=Path, default=PRIVATE_BRIDGE)
    bridge.add_argument('--system-pipewire', action='store_true', help='Use the installed bridge for comparison')
    parser.add_argument('--check-bridge', action='store_true', help='Validate plugin loading without streaming')
    parser.add_argument('--standby', action='store_true',
                        help='Keep the camera node published; start the privileged worker through --worker-socket only while a consumer is linked')
    parser.add_argument('--worker-socket', type=Path, default=Path('/run/qualcomm-isp/control'))
    parser.add_argument('--idle-seconds', type=float, default=5, help='Stop the worker this long after the last consumer leaves')
    parser.add_argument('--poll-seconds', type=float, default=1)
    args = parser.parse_args()
    if args.standby and args.shared_fd is not None:
        parser.error('--standby owns its shared frame; do not pass descriptors')
    if (args.shared_fd is None) != (args.control_fd is None):
        parser.error('Shared memory requires both descriptors')
    shared = None
    spec = importlib.util.spec_from_file_location('ipe_handoff', Path(__file__).with_name('ipe-handoff.py'))
    handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handoff)
    if args.shared_fd is not None:
        shared = handoff.Consumer(args.shared_fd, args.control_fd)
    Gst.init(None)
    if not args.system_pipewire:
        loaded = Gst.Plugin.load_file(str(args.pipewire_plugin.resolve(strict=True)))
        if loaded is None or loaded.get_name() != 'pipewire':
            raise RuntimeError('Could not load the private PipeWire bridge')
    pipeline = Gst.Pipeline.new('sp11-ipe-preview')
    source = Gst.ElementFactory.make('appsrc', 'frames')
    queue = Gst.ElementFactory.make('queue', 'latest')
    convert = Gst.ElementFactory.make('videoconvert', 'full-range-rgb')
    rgb = Gst.ElementFactory.make('capsfilter', 'unambiguous-colour')
    sink = Gst.ElementFactory.make('pipewiresink', 'camera')
    if not all((pipeline, source, queue, convert, rgb, sink)):
        raise RuntimeError('Missing GStreamer appsrc/queue/pipewiresink')
    plugin = sink.get_factory().get_plugin()
    plugin_path = Path(plugin.get_filename()).resolve()
    if not args.system_pipewire and plugin_path != args.pipewire_plugin.resolve():
        raise RuntimeError(f'Unexpected PipeWire bridge: {plugin_path}')
    print(f'PipeWire bridge: {plugin_path} ({plugin.get_version()})', flush=True)
    if args.check_bridge:
        if shared:
            shared.close()
        return
    # Full-range BT.601, matching the verified IFE/IPE NV12 converter.
    source.set_property('caps', Gst.Caps.from_string(
        f'video/x-raw,format=NV12,width={W},height={H},framerate={NOMINAL_FPS}/1,colorimetry=1:4:5:4,pixel-aspect-ratio=1/1'))
    # The installed PipeWire GStreamer bridge omits YUV colourimetry on the
    # receiving caps. Convert while the full-range BT.601 contract is known;
    # RGBA preserves the display RGB values and sets opaque alpha. Chromium
    # WebRTC accepts RGBA but rejects RGBx when enumerating camera formats.
    rgb.set_property('caps', Gst.Caps.from_string('video/x-raw,format=RGBA'))
    convert.set_property('n-threads', 4)
    for key, value in [('is-live', True), ('format', Gst.Format.TIME), ('block', False),
                       ('max-buffers', 2), ('max-bytes', SIZE * 2), ('leaky-type', 2)]:
        source.set_property(key, value)
    for key, value in [('max-size-buffers', 2), ('max-size-bytes', 0), ('max-size-time', 0), ('leaky', 2)]:
        queue.set_property(key, value)
    properties = Gst.Structure.new_empty('props')
    for key, value in [('media.class', 'Video/Source'), ('media.role', 'Camera'),
                       ('node.name', args.node_name), ('node.description', BOARD['description']),
                       ('node.nick', 'Front Camera (ISP)'), ('priority.session', 800),
                       ('node.virtual', True)]:
        properties.set_value(key, value)
    sink.set_property('stream-properties', properties)
    sink.set_property('mode', 2)
    sink.set_property('sync', False)
    sink.set_property('enable-last-sample', False)
    configure_transport_pool(sink)
    for item in (source, queue, convert, rgb, sink):
        pipeline.add(item)
    if not all((source.link(queue), queue.link(convert), convert.link(rgb), rgb.link(sink))):
        raise RuntimeError('Could not link publisher')
    origin = time.monotonic_ns()
    loop = GLib.MainLoop()
    failed = []
    stopping = threading.Event()
    # Display-side evidence: what actually reaches the PipeWire sink, when.
    stats = dict(pushed=0, sink=0, queue_overruns=0, latency_ms=[], last_report=time.monotonic())
    stats_lock = threading.Lock()

    def sink_probe(pad, info):
        buffer = info.get_buffer()
        with stats_lock:
            stats['sink'] += 1
            if buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
                stats['latency_ms'].append((time.monotonic_ns() - (buffer.pts + origin)) / 1e6)
        return Gst.PadProbeReturn.OK

    def report_stats(final=False):
        with stats_lock:
            latencies = sorted(stats['latency_ms'])
            summary = dict(pushed=stats['pushed'], sink=stats['sink'], queue_overruns=stats['queue_overruns'],
                           interval_frames=len(latencies), final=final)
            if latencies:
                summary.update(latency_median_ms=latencies[len(latencies)//2], latency_p95_ms=latencies[int(len(latencies)*.95)],
                               latency_max_ms=latencies[-1])
            stats['latency_ms'] = []
            stats['last_report'] = time.monotonic()
        print('STATS ' + json.dumps(summary), flush=True)

    sink.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, sink_probe)
    queue.connect('overrun', lambda q: stats.__setitem__('queue_overruns', stats['queue_overruns'] + 1))

    def message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            failed.append(str(msg.parse_error()))
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect('message', message)

    def exact(size):
        result = bytearray(size)
        view = memoryview(result)
        while view:
            n = sys.stdin.buffer.readinto(view)
            if not n:
                if len(view) != size:
                    raise EOFError('Truncated frame')
                return None
            view = view[n:]
        return result

    def push(frame, seq, timestamp):
        buffer = Gst.Buffer.new_allocate(None, SIZE, None)
        if buffer.fill(0, frame) != SIZE:
            raise RuntimeError('Incomplete GStreamer frame copy')
        buffer.pts = max(0, timestamp - origin)
        buffer.dts = Gst.CLOCK_TIME_NONE
        buffer.duration = Gst.SECOND // NOMINAL_FPS
        buffer.offset = seq
        result = source.emit('push-buffer', buffer)
        if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
            raise RuntimeError(f'Publisher flow error: {result}')
        with stats_lock:
            stats['pushed'] += 1
            due = time.monotonic() - stats['last_report'] >= 2
        if due:
            report_stats()
        return result

    def feed(consumer):
        """Copy frames from one shared-frame consumer into the pipeline until EOF."""
        first = True
        while not stopping.is_set():
            try:
                seq, timestamp, frame = consumer.receive()
            except EOFError:
                return
            if first:
                print(f'STANDBY {time.monotonic():.3f} first frame of this session (sensor {seq}, age {(time.monotonic_ns()-timestamp)/1e6:.0f} ms)', flush=True)
                first = False
            last_frame[0] = time.monotonic()
            if push(frame, seq, timestamp) != Gst.FlowReturn.OK:
                raise RuntimeError('Publisher flushed before accepting shared frame')
            consumer.acknowledge()

    def reader():
        try:
            if shared:
                feed(shared)
            else:
                while not stopping.is_set():
                    header = exact(16)
                    if header is None:
                        break
                    seq, timestamp = struct.unpack('<QQ', header)
                    frame = exact(SIZE)
                    if frame is None:
                        raise EOFError('Missing frame')
                    push(frame, seq, timestamp)
        except BaseException as e:
            if not stopping.is_set():
                failed.append(str(e))
        finally:
            # No consumer may be linked, so EOS need not reach the sink.
            GLib.idle_add(loop.quit)

    BLACK = bytes(W * H) + b'\x80' * (W * H // 2)
    last_frame = [0.0]
    STALL_SECONDS = 8

    def consumer_count():
        """Distinct PipeWire nodes linked to our node, from pw-dump."""
        try:
            objects = json.loads(subprocess.check_output(['pw-dump'], timeout=10))
        except (subprocess.SubprocessError, ValueError) as exc:
            print(f'STANDBY {time.monotonic():.3f} pw-dump failed: {exc}', flush=True)
            return None
        ours = {o['id'] for o in objects if o.get('type') == 'PipeWire:Interface:Node'
                and o.get('info', {}).get('props', {}).get('node.name') == args.node_name}
        peers = {o['info']['input-node-id'] for o in objects if o.get('type') == 'PipeWire:Interface:Link'
                 and o.get('info', {}).get('output-node-id') in ours}
        return len(peers)

    class Worker:
        """One privileged session requested from the worker daemon."""
        def __init__(self):
            self.control = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.control.settimeout(90)
            memfd = None
            mine = theirs = None
            try:
                self.control.connect(str(args.worker_socket))
                memfd = os.memfd_create('ipe-publisher-frame', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
                os.ftruncate(memfd, SIZE)
                mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                import array
                self.control.sendmsg([json.dumps(dict(type='start')).encode()],
                                     [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [memfd, theirs.fileno()]))])
                reply = json.loads(self.control.recv(65536) or b'{}')
                if reply.get('type') != 'started':
                    raise RuntimeError(f'Worker did not start: {reply}')
                self.session = reply.get('session')
                # The worker has sealed the frame and sent READY; Consumer verifies both.
                owned_fd, memfd = memfd, None  # Consumer closes this even on failure.
                self.consumer = handoff.Consumer(owned_fd, mine.detach(), wait_ready=True, timeout=60)
            except BaseException:
                self.control.close()
                raise
            finally:
                if memfd is not None:
                    os.close(memfd)
                if mine is not None:
                    mine.close()
                if theirs is not None:
                    theirs.close()

        def stop(self):
            try:
                self.control.settimeout(60)
                self.control.send(json.dumps(dict(type='stop')).encode())
                reply = json.loads(self.control.recv(65536) or b'{}')
                print(f'STANDBY {time.monotonic():.3f} worker stopped: {reply}', flush=True)
            except OSError as exc:
                print(f'STANDBY {time.monotonic():.3f} worker stop failed: {exc!r}', flush=True)
            finally:
                self.control.close()

    def feed_worker(consumer):
        try:
            feed(consumer)
        finally:
            consumer.close()

    def standby():
        """Publish black until a consumer links, then run the worker while one is linked."""
        worker = None
        feeder = None
        last_seen = time.monotonic()
        try:
            push(BLACK, 0, time.monotonic_ns())
            while not stopping.is_set():
                count = consumer_count()
                if count:
                    last_seen = time.monotonic()
                if feeder is not None and not feeder.is_alive():
                    # The session ended (worker stopped, failed or publisher EOF).
                    feeder = None
                    if worker is not None:
                        worker.stop()
                        worker = None
                    push(BLACK, 0, time.monotonic_ns())
                    if count:
                        print(f'STANDBY {time.monotonic():.3f} session ended with a consumer still linked; retrying in 3 s', flush=True)
                        stopping.wait(3)
                if count and worker is None:
                    print(f'STANDBY {time.monotonic():.3f} {count} consumer(s) linked: starting worker', flush=True)
                    try:
                        worker = Worker()
                    except BaseException as exc:
                        print(f'STANDBY {time.monotonic():.3f} worker start failed: {exc!r}; retrying in 5 s', flush=True)
                        worker = None
                        stopping.wait(5)
                        continue
                    print(f'STANDBY {time.monotonic():.3f} worker session {worker.session}', flush=True)
                    # A module reload during start can drop PipeWire links for a
                    # few seconds; do not read that as the consumer leaving.
                    last_seen = time.monotonic() + 10
                    last_frame[0] = time.monotonic() + 10   # start grace
                    feeder = threading.Thread(target=feed_worker, args=(worker.consumer,), daemon=True)
                    feeder.start()
                elif (worker is not None and feeder is not None and feeder.is_alive() and last_frame[0]
                      and time.monotonic() - last_frame[0] > STALL_SECONDS and time.monotonic() - last_seen < args.idle_seconds):
                    # Frames stopped although a consumer is linked (for example
                    # after suspend/resume): restart the worker.
                    print(f'STANDBY {time.monotonic():.3f} no frame for {time.monotonic()-last_frame[0]:.0f} s: restarting worker', flush=True)
                    worker.stop()
                    worker = None
                    feeder.join(30)
                    feeder = None
                    last_frame[0] = 0.0
                    push(BLACK, 0, time.monotonic_ns())
                elif not count and worker is not None and time.monotonic() - last_seen >= args.idle_seconds:
                    print(f'STANDBY {time.monotonic():.3f} no consumer: stopping worker', flush=True)
                    worker.stop()
                    worker = None
                    if feeder is not None:
                        feeder.join(30)
                        feeder = None
                    push(BLACK, 0, time.monotonic_ns())
                stopping.wait(args.poll_seconds)
        except BaseException as exc:
            if not stopping.is_set():
                failed.append(repr(exc))
        finally:
            if worker is not None:
                worker.stop()
            GLib.idle_add(loop.quit)

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
    pipeline.set_state(Gst.State.PLAYING)
    thread = threading.Thread(target=standby if args.standby else reader, daemon=True)
    thread.start()
    try:
        loop.run()
    finally:
        stopping.set()
        if args.standby:
            thread.join(60)
        if shared:
            shared.socket.shutdown(socket.SHUT_RDWR)
            thread.join(3)
            if thread.is_alive():
                failed.append('Shared-frame reader did not stop')
            else:
                shared.close()
        pipeline.set_state(Gst.State.NULL)
        report_stats(final=True)
    if failed:
        raise RuntimeError('; '.join(failed))


if __name__ == '__main__':
    main()
