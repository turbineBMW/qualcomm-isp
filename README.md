# Qualcomm ISP on Omarchy

Experimental Qualcomm X1E front-camera ISP work integrated with the Omarchy ARM
kernel. This repository gives Omarchy
and X1E developers the kernel patches, private libcamera changes and camera
runtime used on a Surface Pro 11.

**Qualified hardware: Surface Pro 11 Denali OLED, IMX681 front camera only.**
This is an initial developer source release. Other X1E machines need their own
sensor, device-tree, firmware and tuning qualification.

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
- [Kernel source manifest](packaging/kernel/ISP-SOURCES.json): pinned Omarchy
  recipe and ordered Surface/ISP patch groups.

## What is portable, and what is Surface-specific?

| Layer | Current scope |
| --- | --- |
| Runtime transport, capture leases, PipeWire publication and lifecycle | Reusable Linux code; client UID and media/sensor numbers are discovered dynamically |
| X1E CAMSS/IFE/ICP changes | Starting point for X1E bring-up; this is still an experimental debugfs-driven IPE path |
| Kernel integration | Omarchy ARM 7.2.8-13 plus 66 Surface prerequisite patches and 69 ISP patches; not yet a minimal generic series |
| Board DT, IMX681 controls, firmware name, IOMMU/mapping expectations | Qualified on Denali OLED only |
| Board profile, IFE tuning, AWB references and IPE program bank | Specific to the tested sensor/mode/firmware; not transferable by changing a model name |

The ISP work targets Qualcomm X1E hardware and uses the shared Omarchy ARM kernel.

## Layout

- `runtime/`: capture, processing, leases, publication and worker lifecycle.
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

1. Reproduce and test the preparation workflow on another Surface.
2. Collect inventory/RAW results from a second X1E model and add its profile.
3. Reduce the kernel changes to the necessary generic and per-board series for
   integration into the current Omarchy ARM kernel.

Original standalone code is MIT. Kernel, libcamera and PipeWire retain their
respective licenses and notices; see [LICENSE](LICENSE). This repository does
not grant redistribution rights to external firmware or extracted data.
