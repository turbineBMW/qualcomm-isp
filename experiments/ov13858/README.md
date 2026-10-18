# OV13858 rear-camera ISP experiment

First hardware proof on Surface Pro 11 Denali OLED, 2026-10-17: the rear camera
produces correctly ordered 3840x2160 NV12 through IFE, and those frames pass
through IPE with locally generated rear-sensor programs. The packaged front
camera remains the qualified desktop path. This experiment does not install a
rear PipeWire publisher or replace the installed kernel/libcamera.

## Findings

- The sensor is OV13858, ACPI ID **MSHW0491**, with native GRBG RAW10. Its full
  mode is 4224x3136. IFE scales to 3840x2850 and crops at `(0,344)` to 3840x2160.
- CSID 680 `CAMIF_FRAME_CFG`, register `0x330`, bits 26:24, selects the Bayer
  pattern forwarded to IFE: RGGB=0, GBRG=1, BGGR=2, GRBG=3. Tests swept all four
  values with the CSID generator and the rear sensor's own colour bars.
  GRBG=3 gives white/yellow/cyan/green/magenta/red/blue/black in the right order.
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
  rows and two CV lux rows. `preparation.patch` applies these changes to a
  private copy. It must not be used to generate front-camera assets.

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
`packaging/libcamera` if it has not already been prepared. Copy that patched
source into the private work directory, then apply the incremental rear patch.
The baseline archive is commit `aeae1f0c7155ae2a175f27b571496f87943197f9` plus
`vendor/libcamera-working.patch`; do not apply the incremental patch directly
to upstream libcamera.

```sh
cp -a "$REPO/packaging/libcamera/src/libcamera" "$WORK/libcamera"
patch --batch -p1 -d "$WORK/libcamera" \
  -i "$REPO/experiments/ov13858/libcamera.patch"
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
generated binary assets to this repository. Do not invoke the copied
`preparation/prepare.py`: that full-bank entry point still describes IMX681.

## Measured acceptance

On kernel `7.2.8-13.1-omarchy-isp+`:

- Three changing 4224x3136 packed RAW10 frames, both before and after ISP trials.
- Rear colour bars validate native GRBG; a real scene confirms an image through
  IFE and IPE. Fixed controls are 30 ms, 2x analogue gain and 1x sensor digital.
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

## Remaining integration

1. Establish rear frame-start metadata and frame/control association. Current
   metadata describes requested controls; it does not prove which sensor frame
   applied them.
2. Add rear-aware RAW statistics and qualify automatic exposure/white balance,
   black level, lens shading and image quality over lighting changes. The current
   white balance and IPE CCT/lux-index selections are fixed starting points.
3. Generalize preparation profiles without changing accepted front outputs;
   generate and validate a rear adaptive bank.
4. Integrate the rear path with capture leases, concurrent preparation/readback
   and selectable PipeWire publication. The existing desktop Back Camera node
   still uses the old system path, not this experiment.
5. Package and boot-test the CSID change, then test front/rear switching,
   repeated start/stop, application use and idle/streaming suspend/resume.
