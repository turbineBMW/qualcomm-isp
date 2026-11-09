# Automatic framing (ispautoframe)

Face-following crop for the ISP front camera, the way Windows Studio Effects'
*Automatic framing* does it: a GStreamer element the camera publisher loads
through a `publisher.d` drop-in. It runs a YuNet face detector (OpenCV DNN on
the CPU) on the ISP's 3840x2160 NV12 output and crops a 16:9 window that
follows the face(s), scaled to 1080p. Design, measurements and the open
work (NPU detector, crop in the ISP): [`../docs/AUTOFRAME.md`](../docs/AUTOFRAME.md).

```
appsrc (NV12 3840x2160) ! queue ! ispautoframe control-file=~/.config/qualcomm-isp/autoframe.conf ! capsfilter caps=video/x-raw,width=1920,height=1080 ! videoconvertscale ! pipewiresink
```

- `src/gstispautoframe.cpp`: the element. `src/framing.h`: the controller
  (shared with surface-pro-12-linux). `src/yunet.{h,cpp}`: the detector.
- `bin/isp-autoframe`: the user's tool (`auto|off|toggle|status`, `--face`,
  `--zoom`, `--headroom`, `--speed`, `--hold`, `--group`, `--boxes`,
  `plugin install|remove`). It writes the settings file the element watches.
- `omarchy/plugins/isp.autoframe`: Omarchy bar widget (Quickshell QML).
- `publisher.d/50-autoframe.json`: the drop-in for the front publisher.
- `tools/make-clip.py`: NV12 test clip from recorded frames, optional pan.
- `../packaging/autoframe`: the reference package recipe (`qualcomm-isp-autoframe`).

## Build and offline test

```sh
meson setup build && ninja -C build
python tools/make-clip.py /tmp/clip.nv12 --frames 90 --pan frame-3840x2640.nv12
GST_PLUGIN_PATH=build GST_DEBUG=ispautoframe:5 gst-launch-1.0 filesrc location=/tmp/clip.nv12 ! \
  rawvideoparse format=nv12 width=3840 height=2160 framerate=30/1 ! \
  ispautoframe model=/usr/share/qualcomm-isp/face_detection_yunet_2023mar.onnx draw-boxes=true face-frac=0.45 ! \
  video/x-raw,width=1920,height=1080 ! videoconvert ! pngenc ! multifilesink location=/tmp/out-%03d.png
```

The model is YuNet `face_detection_yunet_2023mar.onnx` (OpenCV Zoo, Apache-2.0),
fetched by the package recipe at a pinned commit.
