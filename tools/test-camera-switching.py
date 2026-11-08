#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Exercise real ISP camera switching in one clock-synchronised Gst process.

Close other camera apps first and uncover both lenses. Requires the installed
front/rear publishers. Captures frames in memory; does not save camera images.
"""
import time
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)
NODES = {'front': 'sp11-ipe-preview', 'rear': 'sp11-ipe-rear'}


def open_camera(camera):
    pipeline = Gst.parse_launch(
        f'pipewiresrc target-object={NODES[camera]} '
        'stream-properties="props,node.dont-fallback=true" ! '
        'video/x-raw,format=RGBA,width=1280,height=720 ! '
        'appsink name=preview sync=true max-buffers=2 drop=true')
    assert pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
    return pipeline


def frames(pipeline, label, started):
    sink = pipeline.get_by_name('preview')
    count, first, last_pts = 0, None, None
    deadline = started + 8
    while time.monotonic() < deadline and count < 10:
        error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if error:
            raise AssertionError(error.parse_error())
        sample = sink.emit('try-pull-sample', Gst.SECOND // 5)
        if sample is None:
            continue
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        # Skip the publisher's standby black frame (alpha remains opaque).
        if max(data[0::64], default=0) <= 2:
            continue
        if first is None:
            first = time.monotonic() - started
        if last_pts is not None:
            assert buf.pts > last_pts, (label, 'non-increasing PTS')
        last_pts = buf.pts
        count += 1
    assert count == 10, (label, 'frames', count, 'first', first)
    print(f'PASS {label}: first image {first:.2f}s, ten frames with increasing PTS', flush=True)


def main():
    current = None
    try:
        for camera in ('front', 'rear', 'front'):
            if current:
                current.set_state(Gst.State.NULL)
            start = time.monotonic()
            current = open_camera(camera)
            frames(current, camera, start)
        current.set_state(Gst.State.NULL)
        current = open_camera('rear')
        # Change back while the rear worker may be waiting/starting. This must
        # not leave an abandoned session holding the hardware for 15 seconds.
        time.sleep(1.2)
        current.set_state(Gst.State.NULL)
        start = time.monotonic()
        current = open_camera('front')
        frames(current, 'front after cancelled rear startup', start)
    finally:
        if current:
            current.set_state(Gst.State.NULL)


if __name__ == '__main__':
    main()
