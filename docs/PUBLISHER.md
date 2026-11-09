# Publisher formats and recovery

The ISP still captures and processes native 3840x2160 NV12. The desktop
publisher offers RGBA at 3840x2160, 1920x1080, 1280x720 and 640x360, with a
nominal 30 fps in every mode. Native 4K is the default preference. Smaller
outputs are scaled by GStreamer; this does not change the sensor or IPE mode.
Full-range BT.601 input interpretation and opaque RGBA output are preserved.

The private PipeWire bridge advertises the complete list. When a consumer
selects a size, the sink requests upstream renegotiation and wraps transport
buffers for that size. Frames still carrying the previous size are dropped
until the scaler follows. The transport pool remains bounded to eight buffers.
The scaler connects directly to the sink: an intervening caps filter can fail
renegotiation on an old-size frame during a rapid reconnect.

A proxy error such as `no more input formats` does not make a healthy provider
stream permanently fail. An incompatible request against an idle node fails
without replacing the publisher process or node. Actual stream errors still
reach the GStreamer bus. In standby mode the publisher rebuilds its pipeline
in the same process, preserving the worker session. Rebuilding replaces the
PipeWire node, so clients must reconnect. Three rebuilds are allowed in a
rolling 60-second window; another error exits for systemd recovery.

## Desktop camera routing

Runtime `0.1-12` installs `wireplumber/prefer-isp.lua` and its component config.
Each publisher advertises `qualcomm.isp.raw-node` from its board profile.
While that publisher exists, the policy suppresses only that matching libcamera
node. It retains the device and creation properties, then runs the normal
libcamera creation hooks to restore fallback when the last matching ISP
publisher disappears. Existing user rules still apply to restored nodes.
Presence means the published PipeWire source exists; this is not a worker
health probe, and a transient capture failure still uses publisher recovery.

Both startup orders and WirePlumber restarts are supported. Front and rear
operate independently. No sensor driver, device permission, raw `/dev/video*`
endpoint or IR authentication path is disabled. Applications using PipeWire
see only the ISP entry for an available publisher; applications that enumerate
V4L2/libcamera directly can still discover those lower-level routes. Gaze
already filters media-controller pipeline fragments.

Close camera/audio applications before running the desktop lifecycle check:

```sh
python tools/test-camera-routing.py
```

It restarts WirePlumber and both publishers, verifies fallback in each direction,
and restores the initial publisher running states. It does not capture frames.
Removing an ISP publisher restores fallback without restarting WirePlumber.
Removing the policy package requires restarting WirePlumber to unload its hooks.

## Shared camera hardware and switching

The front and rear publishers share an exclusive CAMSS/ICP lock. Runtime
`0.1-13` preserves the five-second debounce for same-camera close/reopen, but
an unused camera releases immediately on the next graph poll when another ISP
publisher has a linked consumer. A failed graph query is unknown demand and
does not stop capture. The existing owner is never preempted while it still
has consumers.

Startup completion no longer grants a future ten-second consumer-presence
timestamp. That extra hold, followed by the ordinary five-second idle grace,
blocked the newly selected camera when users switched back during startup.
The frame-stall watchdog still has a startup grace; hardware ownership does not.
Session exit `EX_TEMPFAIL` reports the hardware lock as `busy`, so the publisher
rechecks selection and retries after one second. Other startup failures retain
the five-second retry delay.

With both lenses uncovered and other camera apps closed, run
`python tools/test-camera-switching.py` to test switching and cancelled startup
in one process with clock-synchronised rendering, increasing timestamps and
non-black frames. Actual Gaze picker checks remain necessary for GUI acceptance.

## Preview startup timing

Before starting an on-demand worker, the publisher sends a black frame with
a current monotonic timestamp to the linked consumer. The idle black frame
may have been discarded before that consumer connected. Without a fresh
startup frame, a GStreamer consumer can establish its pseudo-live timestamp
offset from the first camera frame, which is delayed by IPE reference seeding.
That startup delay then persists even after processing reaches steady state.
Real frames retain their original sensor timestamps and sequence numbers.

On the SP11 rear camera, a clock-synchronised 4K consumer measured a median
sensor-timestamp-to-consumer age of 283 ms before this change and 78 ms in a
separate candidate trial. This measures software delivery, not physical
screen presentation. Color, exposure and denoising are unchanged.

## One active format per node

Concurrent consumers must accept the same active format. This publisher does
not provide independent per-client scaling.

There is also an unresolved system PipeWire/GStreamer interaction on the tested
desktop: starting a 4K-only consumer while 1080p is running can stall both
consumers. PipeWire's link negotiation may give a new consumer the already
active format after its format filter has no match. The system `pipewiresrc`
then hits `handle_format_change: assertion 'gst_caps_is_fixed (pwsrc->caps)'
on the empty intersection instead of completing or rejecting the link.

This was also reproduced with the unchanged, single-format 0.1-10 publisher:
native 4K streaming followed by a 1080p-only peer stalls both. Restricting
the provider's advertised list while streaming did not solve it. This change
does not claim to fix that consumer/server path. Close the conflicting
consumers, then reopen at a common size. Gaze should filter media-controller
pipeline fragments and choose a size the camera actually offers.

## Reproduce without camera hardware

Build the private bridge and run the synthetic integration suite in a desktop
session with PipeWire and GStreamer installed:

```sh
python tools/build-pipewire.py vendor/pipewire-gst /tmp/isp-test-bridge
python tools/test-publisher.py --bridge /tmp/isp-test-bridge/libgstpipewire.so \
  --output /tmp/isp-publisher-results
```

The output directory must not already exist. The test uses a uniquely named
node and a synthetic configuration; it does not open the real camera or worker.
Consumer fallback is disabled so a missing test node cannot silently select a
real camera. Checks cover advertised sizes, a 1080p ceiling, rejected idle
negotiation, rapid size changes, compatible simultaneous consumers, default 4K,
full-range gray/alpha pixels, stdin shutdown, pipeline rebuilding and its rate
limit. The conflicting-client limitation above is excluded from passing checks.
