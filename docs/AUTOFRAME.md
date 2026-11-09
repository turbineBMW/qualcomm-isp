# Automatic framing for the front camera

`qualcomm-isp-autoframe` gives the ISP front camera the *Automatic framing* of
Windows Studio Effects: a GStreamer element (`ispautoframe`) in the camera
publisher finds the face(s) with a YuNet detector and crops a 16:9 window out
of the ISP's 3840x2160 output that follows them, scaled to 1080p. The wide
IMX681 lens is the headroom for that crop. Ported 2026-11-09 from the Surface
Pro 12 work (`surface-pro-12-linux`, `autoframe/`), which has the behaviour
study and the Intel-side history; the controller is the same code.

```
IMX681 ─► CAMSS IFE/IPE ─NV12 3840x2160─► appsrc ─► queue ─► ispautoframe ─NV12 1920x1080─► videoconvertscale ─► pipewiresink
                                                               │  detector: NV12 → 320x192 BGR → YuNet (OpenCV DNN, CPU), every 2nd frame
                                                               │  controller: faces → target box → damped window (dead zone, hysteresis, hold)
                                                               └  cropper: OpenCV crop + scale of the two NV12 planes
```

## Pieces

- `autoframe/src/gstispautoframe.cpp`: the element (`GstVideoFilter`, NV12 in
  at any size, NV12 out at the negotiated size). Properties: `enable`, `model`,
  `det-width`/`det-height` (320x192), `threads` (2), `max-zoom` (2), `face-frac`
  (0.30), `headroom` (0.12), `speed` (4 rad/s), `hold-time` (3 s), `group`,
  `detect-interval` (2), `control-file`, `draw-boxes`.
- `autoframe/src/framing.h`: the controller (target box, dead zone, critically
  damped spring, presence hysteresis, hold, group framing). Shared with the
  Surface Pro 12 tree; keep them in step.
- `autoframe/src/yunet.{h,cpp}`: YuNet through `cv::FaceDetectorYN` (OpenCV's
  own runner: input scaling, decode and NMS). OpenCV 5 prints one
  `setPreferableTarget ... not supported by the new graph engine` warning at
  start; harmless.
- `runtime/publisher_filters.py` and the `publisher.d` hook in
  `runtime/ipe-pipewire.py` (runtime 0.1-20): the package drops
  `/etc/qualcomm-isp/publisher.d/50-autoframe.json` and the front publisher
  inserts the element between its frame queue and its scaler, offering
  1920x1080, 1280x720 and 640x360 to peers instead of 4K first. See
  [PUBLISHER.md](PUBLISHER.md).
- `autoframe/bin/isp-autoframe`: `auto|off|toggle|status`, `--face`, `--zoom`,
  `--headroom`, `--speed`, `--hold`, `--group`, `--boxes`; `plugin
  install|remove` for the bar widget. It writes
  `~/.config/qualcomm-isp/autoframe.conf`; the publisher runs in the user's
  session, so the element reads that same file (mtime with nanoseconds, or
  size, polled once a second) and nothing has to be restored at login. This is
  simpler than the Surface Pro 12 version, whose relay runs as root and needs a
  world-writable `/run` directory plus a restore unit.
- `autoframe/omarchy/plugins/isp.autoframe`: Omarchy bar widget (Quickshell
  QML): camera icon (right-click switches), popup with Follow me / Full view,
  Loose / Normal / Tight presets, sliders for face size, zoom limit, headroom,
  speed and hold, the detection-box overlay and a reset. It watches the
  settings file and drives everything through the tool.
- `autoframe/tools/make-clip.py`: NV12 test clip from recorded full-sensor
  frames, with an optional pan so a still face moves.
- `packaging/autoframe/PKGBUILD`: the reference recipe (plugin in
  `/usr/lib/gstreamer-1.0`, model in `/usr/share/qualcomm-isp`, tool, drop-in,
  widget). The published recipe lives in `surface-pro-11-omarchy`.

## Measurements (2026-11-09, Surface Pro 11, Oryon, kernel 7.3.0-25.2)

Offline, 630 frames of a 3840x2160 clip (a recorded IMX681 frame with a face,
panned), output 1920x1080, `gst-launch` with `fakesink`. CPU is `user` time
with the parse-only baseline (0.43 s) subtracted.

| Configuration | detect per frame (avg, every 2nd frame) | crop+scale | CPU per frame |
|---|---|---|---|
| 1 thread | 2.53 ms | 2.48 ms (full view, 2x area downscale) | 5.6 ms (17 % of a core at 30 fps) |
| 2 threads (default) | 1.95 ms | 0.36 ms (1.44x window) | 4.1 ms (12 %) |
| 4 threads | 1.70 ms | 0.23 ms | 5.0 ms (15 %) |

A single YuNet pass at 320x192 is therefore about 4 ms on one Oryon core, ten
times the Surface Pro 12's NPU (0.42 ms) but still a small share of a frame.

What the publisher pays for a 1080p consumer, same clip and frame count,
`videoconvertscale n-threads=4` to RGBA as in the publisher:

| Pipeline | CPU per frame |
|---|---|
| Today: 4K NV12 → videoconvertscale → RGBA 1080p | 20.8 ms |
| With framing: 4K NV12 → ispautoframe (2 threads) → 1080p NV12 → videoconvertscale → RGBA 1080p | 10.3 ms |

The element pays for itself: converting 1080p instead of 4K saves more than
the detector and crop cost, so the publisher uses about half the CPU it does
now for the common 1080p peer. (A peer that insisted on 4K no longer gets it;
the drop-in's `formats` list decides, and editing it to put 3840x2160 first
brings 4K back as a scaled-up framed picture.)

Behaviour on the clip: the face box is on the face in every frame; with the
default 30 % face size the lab frame (face already 30 % of the height) stays
at full view, with `--face 0.45` the window settles at 1.44x and follows the
pan.

In place (runtime 0.1-20 + package 0.1-1 installed, a headless 1080p
`pipewiresrc` consumer for 10 s): the worker started, 303 frames reached the
sink at 30 fps with no queue overruns, publisher sink latency median 63-67 ms
(p95 76-82 ms) against a 68 ms median for the plain pipeline's last session,
no pipeline errors. What remains is the visual check by the user in front of
the camera (Chromium or Snapshot at 1080p): framing look, follow speed, the
hold when leaving.

## Install and use

```sh
sudo pacman -U qualcomm-isp-runtime-0.1-20-*.pkg.tar.xz qualcomm-isp-autoframe-0.1-1-*.pkg.tar.zst
systemctl --user restart qualcomm-isp-publisher.service   # if the camera was open
isp-autoframe status
isp-autoframe --face 0.40            # tighter
isp-autoframe off                    # full field of view (scaled to 1080p)
isp-autoframe plugin install         # Omarchy bar widget
```

`journalctl --user -u qualcomm-isp-publisher` shows `Publisher filter from
/etc/qualcomm-isp/publisher.d/50-autoframe.json: ...` and `Publisher formats:
1920x1080, 1280x720, 640x360` at start. `GST_DEBUG=ispautoframe:5` on the
publisher logs the per-frame cost every 300 frames.

## Next steps

1. **Detector on the NPU (Hexagon).** The X1E's cDSP is reachable from Linux:
   `/mnt/data/sp11/npu` holds a working llama.cpp Hexagon (HTP, v73) build
   through `libcdsprpc.so`/fastrpc with the `fastrpc_shell_3` and
   `libggml-htp-v73.so` DSP images. Nothing on this machine runs a
   convolutional ONNX model there yet. Options, roughly in order of effort:
   OpenCV's OpenCL target on the Adreno (rusticl or the Qualcomm ICD; a
   one-line backend change in `yunet.cpp`, worth measuring first because the
   GPU is idle during a call); Qualcomm's QNN/AI Engine Direct with the
   Hexagon SDK (closed, needs the model converted and a skel library for the
   cDSP); a hand-written HTP op library the way ggml's Hexagon backend does it
   (open, large). The CPU cost today is 4 ms per detection, so this is a
   power item, not a latency one: measure the publisher's package power with
   and without the detector before choosing.
2. **Crop in the ISP, as Windows does.** The IPE scales to the 3840x2160
   output in its program; a per-frame crop/zoom window applied there would
   hand the publisher a framed 1080p stream and remove the 4K frame copy, the
   detector's downscale from 4K and the CPU crop. The runtime's IPE
   programming (`runtime/ipe-live.py`, the program bank) is where the output
   crop would be set; the controller already has `extend_aspect` for a crop
   window expressed in sensor coordinates and the element only needs the
   Surface Pro 12's `mode=isp` path back (detect on the output, map through
   the window sent N frames earlier, send the window upstream). Two findings
   from the Intel side to check here before committing: the ISP's temporal
   noise reduction may be weaker in a downscaled configuration (it was, on
   the Intel PSYS, which is why that port defaults to the CPU crop), and the
   zoom-region update latency must be measured so detections map to the
   right frame.
3. **Face-based exposure.** The Surface Pro 12 sends the faces to the camera
   HAL's 3A as `face-regions`. The runtime's RAW-driven AE could weight the
   face box the same way (`ipe-adaptive.py`), with framing on or off.
4. **Rear camera.** The same element works on the rear publisher's node by
   adding it to the drop-in's `nodes`; the rear's 4K output set is the same.
   Not enabled: the rear is a document/scene camera.
