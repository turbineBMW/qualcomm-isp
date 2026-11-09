# Qualcomm ISP on Omarchy

Experimental Qualcomm X1E front-camera ISP work integrated with the Omarchy ARM
kernel. This repository gives Omarchy
and X1E developers the kernel patches, private libcamera changes and camera
runtime used on a Surface Pro 11.

**Qualified hardware: Surface Pro 11 Denali OLED, IMX681 front camera, and
the OV13858 rear camera on this machine.** This is a developer source release.
Other X1E machines need their own sensor, device-tree, firmware and tuning
qualification.

Rear OV13858 has [4K ISP desktop capture](experiments/ov13858/README.md) with
RAW-driven automatic exposure and white balance, ten-bit denoising precision,
spatial denoising at the sensor's physical gain, a temporal-filter bypass that
removes motion ghosting, colour from the Windows tuning, and a preview path
measured at a 48 ms median sensor-to-publisher age. The packages include an
optional rear profile and publisher; front/rear switching has passed on this
machine. Broader rear lighting and suspend/resume qualification remain open.
Runtime `0.1-12` prefers each available ISP publisher in PipeWire camera pickers
and automatically restores its software libcamera entry when the publisher stops.

**Firmware and IPE program/tuning data are not included.** The preparation
workflow generates Surface board data from two locally supplied, version-checked
Windows driver files. Installing ICP firmware alone is insufficient. Other X1E
developers can begin source review, platform inventory and staged bring-up.

## Start here

- [Testing another X1E machine](docs/TESTERS.md): export Windows drivers, stage
  firmware, inventory camera driver files and establish the sensor/RAW/IFE path
  before IPE.
- [Build and install](docs/INSTALL.md): private dependencies, firmware placement,
  local board-data import, activation and rollback.
- [Firmware extractor integration](docs/FIRMWARE-EXTRACTOR.md): verified ICP
  copy support and the separate board-data preparation workflow.
- [Prepare board data](docs/PREPARE-BOARD-DATA.md): supported Microsoft driver
  package, offline generation and comparison with the accepted Surface assets.
- [Source provenance and licenses](docs/PROVENANCE.md): origins, source hashes,
  retained attribution and excluded data.
- [Publisher formats and recovery](docs/PUBLISHER.md): desktop resolutions,
  error handling, synthetic tests and the concurrent-client limitation.
- [Automatic framing](docs/AUTOFRAME.md): the face-following crop for the
  front camera, its controls, and the NPU and ISP-crop work still open.
- [Kernel source manifest](packaging/kernel/ISP-SOURCES.json): pinned Omarchy
  recipe and ordered Surface/ISP patch groups.

## What is portable, and what is Surface-specific?

| Layer | Current scope |
| --- | --- |
| Runtime transport, capture leases, PipeWire publication and lifecycle | Reusable Linux code; client UID and media/sensor numbers are discovered dynamically |
| X1E CAMSS/IFE/ICP changes | Starting point for X1E bring-up; this is still an experimental debugfs-driven IPE path |
| Kernel integration | The Surface Pro 11 `linux-omarchy` 7.3-25.1 recipe (Omarchy ARM 7.3-25 plus its Surface patches) and 69 ISP patches; not yet a minimal generic series |
| Board DT, IMX681 controls, firmware name, IOMMU/mapping expectations | Qualified on Denali OLED only |
| Board profile, IFE tuning, AWB references and IPE program bank | Specific to the tested sensor/mode/firmware; not transferable by changing a model name |

The ISP work targets Qualcomm X1E hardware and uses the shared Omarchy ARM kernel.

## Layout

- `runtime/`: capture, processing, leases, publication and worker lifecycle.
- `autoframe/`: automatic framing for the front camera (`qualcomm-isp-autoframe`,
  a GStreamer element the publisher loads through `publisher.d`); see
  [docs/AUTOFRAME.md](docs/AUTOFRAME.md).
- `profiles/`: explicit board/sensor/firmware configuration.
- `packaging/`: kernel, private libcamera and runtime Arch recipes.
- `vendor/`: pinned source manifests, complete dependency diffs and MIT bridge.
- `tools/`: inventory, build/export helpers and explicit local data import.
- `preparation/`: source for offline, version-specific Surface asset generation.

Dependencies install privately under `/usr/lib/qualcomm-isp`. Configuration is
in `/etc/qualcomm-isp/config.json`; external board data belongs under
`/var/lib/qualcomm-isp/assets`. The source/runtime contains no developer-home
or checkout path. Installation does not automatically enable the services.

## Validation

On 2026-11-09, kernel `7.3.0-25.2-omarchy`, private libcamera `0.7.2-8` and
runtime `0.1-19` passed on the Surface Pro 11 with the rear camera active:

- Rear preview at 3840x2160: 28.8 fps output from the 30 fps sensor, median
  sensor-to-publisher age 47.8 ms (p95 54.6 ms), publisher sink latency median
  53.8 ms with no queue overruns. Startup priming removed a 283 ms consumer
  offset. Front timing unchanged.
- Rear image: the P010 interior, motion-safe temporal-filter bypass and spatial
  denoising at the physical gain, with libcamera's OV13858 gain bounded to the
  range that still adds signal. A 300-output capture settled at 33,307 us and
  15.5x gain; front/rear/front/cancelled-rear switching passed.
- Runtime check phase (nine PD10/widening comparisons, precision, session,
  handoff, worker, lease, sleep and demand suites), 14 synthetic publisher
  checks, preparation and colour regressions, and a full spatial bank rebuild
  that reproduces the installed assets.
- Worker services at nice -10 and RealtimeKit priority for the publisher's
  frame threads; no kernel faults, ICP unloaded after every session.

Details and limits for each release are in [docs/RELEASE-CHECKS.md](docs/RELEASE-CHECKS.md).

On 2026-11-07, kernel `7.3.0-25.1-omarchy-isp`, private libcamera `0.7.2-4`
and runtime `0.1-8` passed on the Surface Pro 11:

- Preflight, and 150-frame native 4K RGBA PipeWire consumers in about 7 s,
  with ICP unloaded and runtime usage back to zero after every stop.
- A slow 4K PNG consumer followed by ordinary consumers, repeated: every
  consumer completed (the bridge no longer holds a frame across a disconnect).
- A forced hang (`ipe-live` stopped with SIGSTOP): the session tree was killed,
  ICP unloaded and the next start worked; the stop finished 20.8 s after it
  was requested.
- The first camera session after a cold boot stopped in 0.37 s.

Snapshot, Chromium and suspend/resume with the camera were not repeated on
7.3; the 2026-10-17 results below are from 7.2.8.

On 2026-10-17, kernel `7.2.8-13.1-omarchy-isp+`, private libcamera `0.7.2-2`
and runtime `0.1-3` passed:

- 4K processing, Snapshot and Chromium Mozilla getUserMedia.
- Actual suspend/resume with the camera idle and with Chromium streaming.
- Automatic camera recovery about five seconds after service restoration.
- 34 transport/lifecycle tests and six PD10 reference comparisons.
- Installed 120-frame capture with all 137/137 buffer leases returned.
- 420-frame PipeWire streaming across sleep-hook stop/restore.

Public recipes increment libcamera to `0.7.2-3` for the pinned source delivery
and runtime to `0.1-6` for license notices and preparation/extractor documentation.
The tested runtime behavior and libcamera source contents are unchanged.
Physical acceptance above applies to the installed revisions; build checks
cover the publication recipes.

The offline preparation workflow regenerated all 625 bank entries. Comparison
with the accepted assets passed with the documented striping-pointer and
inactive-CCM exceptions. After installing those assets, a 120-frame 4K PipeWire
check passed. On 2026-10-17 the owner also confirmed Snapshot, Chromium and both
idle and streaming suspend/resume passed with image quality and recovery
unchanged; see the [preparation guide](docs/PREPARE-BOARD-DATA.md).

## Next integration work

1. Qualify the rear camera more broadly: low light, mixed lighting, and
   suspend/resume while streaming.
2. Shorten the preview path further: cheaper exposure program switches,
   DMA-BUF output to the publisher with GPU colour conversion, and a dedicated
   P010 input per slot once the ICP memory region can grow.
3. Reduce the kernel changes to the necessary generic and per-board series for
   integration into the current Omarchy ARM kernel, and replace the DLL-derived
   preparation data with a clean-room derivation.

Independent tester work remains open: reproduce preparation on another Surface
and collect inventory/RAW results from another X1E model. Local development does
not depend on those reports arriving.

Original standalone code is MIT. Kernel, libcamera and PipeWire retain their
respective licenses and notices; see [LICENSE](LICENSE). This repository does
not grant redistribution rights to external firmware or extracted data.
