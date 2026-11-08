# OV13858 rear-camera ISP experiment

First hardware proof on Surface Pro 11 Denali OLED, 2026-10-17: the rear camera
produces 3840x2160 NV12 through IFE, and those frames pass
through IPE with locally generated rear-sensor programs. The packaged front
camera remains the qualified desktop path. The updated packages include an optional rear PipeWire publisher;
[installation](../../docs/INSTALL.md#experimental-ov13858-rear-isp) is explicit.
The bounded experiment scripts do not install packages or replace the kernel.

## Findings

- `mode-controls.patch` refreshes vertical blanking limits/defaults after sensor
  mode configuration. Hardware regression on 2026-11-08 deliberately seeded
  1056x784 (default blanking 2430), then opened the full-resolution ISP route.
  With private libcamera `0.7.2-5`, 180 desktop frames completed without a worker
  restart; 247 processed frames including idle grace stayed at 66,697 us exposure
  and cleanup reported no errors. Before the fix exposure reached 115,566 us
  and exceeded the adaptive runtime's metadata limit. Rear colour and wider
  lighting qualification remain separate from this lifecycle/control check.

- The sensor is OV13858, ACPI ID **MSHW0491**, with native GRBG RAW10. Its full
  mode is 4224x3136. IFE scales to 3840x2850 and crops at `(0,344)` to 3840x2160.
- CSID 680 `CAMIF_FRAME_CFG`, register `0x330`, bits 26:24, selects the Bayer
  pattern forwarded to IFE: RGGB=0, GRBG=1, BGGR=2, GBRG=3. The original bar
  interpretation incorrectly selected 3 for GRBG. On 2026-11-08 paired real
  GRBG RAW and pre-IPE NV12 proved that 3 reverses red and blue before white
  balance; 1 restores their correspondence. Red-only and blue-only gain trials
  independently verified the gain controls. Sensor bars alone did not qualify
  real-scene channel identity.
  Horizontal cropping is unsuitable because it scrambles packed RAW10 groups.
- `csid-native-bayer.patch` derives the field from the effective source format.
  It retains the existing GBRG row crop for front-path compatibility. RAW10 was
  measured; the analogous other bit-depth mappings have not been hardware tested.
  The object compiles and strict checkpatch passes. The patched kernel has not
  been installed: hardware trials use the existing lab `ipp_pokes` parameter to
  apply the same register value temporarily.
- `libcamera.patch` adds an opt-in OV13858 pixel path, linear analogue gain
  (`code / 128`) and sensor digital gain with unity at 1024. IMX681 retains its
  reciprocal gain model and unity digital code 256. The default slow-frame gain
  remains 4x for both. Enable rear experiments with
  `LIBCAMERA_CAMSS_OV13858_ISP=1` only alongside the matching CSID setting.
- Rear tuning differs structurally from the front: TF reserve selector 4,
  DSX module occurrence 1, one TMC region, three IPE gamma rows, two CCM lux
  rows and two CV lux rows. `preparation/sensor_layout.py` selects these shapes by exact tuning hash.
  `prepare.py --sensor ov13858` generates a complete rear adaptive bank; the
  default remains IMX681. Three front grid entries (18 hardware files each)
  and the IFE profile still match the accepted front assets.

The register investigation used local driver disassembly plus hardware tests.
Disassembly, Windows files, extracted programs and captured images stay outside
this source tree. The patches contain source changes only.

## Inputs and reproduction

Start from this project's source baseline `06c556b`, with its qualified front
runtime and firmware already installed. Use the same aarch64 build dependencies
as [the installation guide](../../docs/INSTALL.md) and
[preparation guide](../../docs/PREPARE-BOARD-DATA.md).

The rear `MSHW0491` extension selects `com.surface.tuned.rfc_ov13858.bin`.
The `_MSHW0561` variant is different and is not accepted here.

| Owner-supplied input | SHA-256 |
| --- | --- |
| `QcDeviceMFT8380.dll` | `c241b7fbb2ec54e439752a1ea7ad25da10ca740012a54bd0e7a87ea94a141c35` |
| `com.surface.tuned.rfc_ov13858.bin` | `4858ccb297eeecbc8e9b6d673f7ab4b0ead559adf16e3fe717eea9e40ccef635` |

Set absolute paths. The work directory must be new and outside the repository.
Preparation runs without root, copies and checks the supplied inputs, and invokes
the existing hash-guarded native DLL helper offline. It generates one gain=2x,
DRC=1.5 anchor and a fixed 30 ms exposure script; this is not an adaptive bank.
White-balance starting gains are extracted from the rear tuning. The IFE profile
disables front-specific paired RAW statistics and software filtering.

```sh
REPO=/absolute/path/to/qualcomm-isp
WORK=/absolute/path/to/private-rear-work
python -I "$REPO/experiments/ov13858/prepare-anchor.py" \
  --dll /absolute/path/to/QcDeviceMFT8380.dll \
  --tuning /absolute/path/to/com.surface.tuned.rfc_ov13858.bin \
  --output "$WORK"
```

Prepare the pinned baseline libcamera with `makepkg --nobuild` in
`packaging/libcamera` if it has not already been prepared. The recipe now applies the incremental rear patch too. Copy that prepared
source into the private work directory.
The baseline archive is commit `aeae1f0c7155ae2a175f27b571496f87943197f9` plus
`vendor/libcamera-working.patch`; do not apply the incremental patch directly
to upstream libcamera.

```sh
cp -a "$REPO/packaging/libcamera/src/libcamera" "$WORK/libcamera"
meson setup "$WORK/build" "$WORK/libcamera" --buildtype=release \
  -Dpipelines=camss,simple,uvcvideo -Dipas=soft -Dv4l2=disabled \
  -Dgstreamer=disabled -Dcam=enabled -Dqcam=disabled -Dlc-compliance=disabled \
  -Dpycamera=disabled -Ddocumentation=disabled -Dtest=false -Dtracing=disabled \
  -Dcam-output-kms=disabled -Dcam-output-sdl2=disabled -Dcam-jpeg=disabled \
  -Dwerror=false
ninja -C "$WORK/build" -j4
```

Close camera applications before the bounded live test. The script checks the
installed transport identity, pauses the publisher and WirePlumber, takes the
camera lock, applies the GRBG field, loads ICP and runs capture as the original
non-root user. Fresh frames pass over a FIFO to the serialized IPE session.
Cleanup restores the register override, media graph and services and checks
that ICP unloads and its runtime usage returns to zero. Existing output paths
are refused. The script is specific to the already qualified Denali lab kernel.

```sh
ASKPASS_REASON="Test ninety live rear-camera ISP frames and restore the front-camera services" \
  sudo -A python -I "$REPO/experiments/ov13858/live.py" \
  --work "$WORK" --output "$WORK/live-90" --frames 90
ffmpeg -f rawvideo -pixel_format nv12 -video_size 3840x2160 \
  -i "$WORK/live-90/ipe.nv12" -frames:v 1 "$WORK/live-90/ipe.png"
```

The last IFE/IPE frames, capture metadata and timing/cleanup report remain in
that private output directory. Review the images locally; do not add them or
generated binary assets to this repository. For an adaptive bank, run the main `preparation/prepare.py` with
`--sensor ov13858` and the same owner-supplied inputs in a separate output directory.

## Measured acceptance

On kernel `7.2.8-13.1-omarchy-isp+`:

- Three changing 4224x3136 packed RAW10 frames, both before and after ISP trials.
- Rear colour bars and a real scene confirm an image through
  IFE and IPE. Fixed controls are 30 ms, 2x analogue gain and 1x sensor digital.
  These initial checks did not validate real-scene channel identity; the
  2026-11-08 paired-RAW correction above supersedes the original Bayer choice.
- Sixteen IFE-only 4K frames at approximately 30 fps.
- Eight offline IPE submissions; mapped and debugfs readback agree byte for byte.
- Ninety live IFE-to-IPE frames, all input hashes distinct, in 5.76 seconds
  including startup (~15.6 fps). Median full IPE firmware time is 19.54 ms;
  this is not end-to-end throughput. FIFO copying, hashing, serial processing
  and startup are included in the elapsed result.
- Reproducible preparation regenerated the compared program blobs identically;
  a subsequent 30-frame live run passed with the final libcamera patch.
- Both live runs returned success, zero cleanup errors, ICP unloaded and runtime
  usage zero. Normal front-camera PipeWire capture subsequently passed 60 frames.
- Kernel CSID object and experimental libcamera compile; strict kernel patch
  style checks and the existing seven preparation boundary checks pass.
- The final experimental libcamera also captured 16 front-camera frames with
  its normal paired-RAW setup. Licensing checks pass for all 581 source files.

An early sensor-pattern sweep logged one CCI master-1 timeout at 21:08:13 EDT.
Later real-scene captures, IPE replay and live repeat tests did not reproduce
that timeout. Do not treat these short trials as long-duration reliability or
suspend/resume qualification.

## Adaptive capture

Rear RAW sampling maps the 4K output crop back to native GRBG at step 16,
with 32,400 full-frame and 8,160 centre samples per channel. Independent decoding
of retained RAW files reproduces both histograms exactly. RAW and NV12 share
hardware sequence numbers. Five optical exposure transitions establish a
two-frame sensor-control delay; the immediately following frame is withheld
when sensor controls change.

Rear DMA cache synchronization and RAW statistics run on a dedicated worker.
An eventfd returns completed statistics to the libcamera thread; buffers are
requeued there, and shutdown joins the worker before stopping/unmapping buffers.
The existing quarter-frame SOF deadline is retained. Histogram logging is batched.

A manual-to-auto trial completed 300 IFE-to-IPE frames in 11.01 s (27.25 fps),
with no late SOFs, no invalid statistics, and no cleanup errors. Exposure changed
from 5 ms/1x to 33.3 ms/2.07x; white balance changed from [1.80,1.30] to
[1.63,1.46]. This demonstrates response in one steady lit scene, not a full
lighting or colour-quality qualification. The experimental 625-entry rear bank
is generated locally and is not distributed.

## Remaining integration

The rear profile and separate socket/publisher units are experimental. Front and
rear share the exclusive ICP session lock and cannot stream simultaneously.
The rear profile temporarily applies the native GRBG lab override and restores
it at shutdown; the source kernel patch is not yet installed or boot-tested.

Installed desktop tests passed front → rear → front, with 90 non-black
PipeWire frames per stream. The rear worker published 218 frames at 29.49 fps
and exited cleanly; the front workers also exited cleanly. Switching took
7.49 seconds to rear and 6.85 seconds back to front. The final packages passed
90 front ISP frames and 120 rear ISP frames; both retained rear RAW histograms
match independent decoding exactly. The sleep hook stopped and restored both
idle workers without suspending the machine.

Lighting changes, colour calibration, long-duration reliability and actual
idle/streaming suspend/resume still need qualification. No second Surface Pro 11
has been tested.
