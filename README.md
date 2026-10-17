# Qualcomm ISP on Omarchy

Experimental Qualcomm X1E front-camera ISP work integrated with the Omarchy ARM
kernel, formerly packaged as `linux-omarchy-n1x`. This repository gives Omarchy
and X1E developers the kernel patches, private libcamera changes and camera
runtime used on a Surface Pro 11.

**Qualified hardware: Surface Pro 11 Denali OLED, IMX681 front camera only.**
This is an initial developer source release. Other X1E machines need their own
sensor, device-tree, firmware and tuning qualification.

**Firmware and IPE program/tuning data are not included.** There is not yet a
public extraction workflow for all of the qualified board data. Installing the
ICP firmware alone is insufficient to reproduce the complete processed camera.
Developers can begin source review, platform inventory and staged bring-up now.

## Start here

- [Testing another X1E machine](docs/TESTERS.md): collect inventory and establish
  the sensor/RAW/IFE path before attempting IPE.
- [Build and install](docs/INSTALL.md): private dependencies, firmware placement,
  local board-data import, activation and rollback.
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

“N1x” here identifies the Omarchy kernel lineage. These Qualcomm ISP changes
have not been tested on NVIDIA N1x hardware.

## Layout

- `runtime/`: capture, processing, leases, publication and worker lifecycle.
- `profiles/`: explicit board/sensor/firmware configuration.
- `packaging/`: kernel, private libcamera and runtime Arch recipes.
- `vendor/`: pinned source manifests, complete dependency diffs and MIT bridge.
- `tools/`: inventory, build/export helpers and explicit local data import.

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
and runtime to `0.1-4` for license notices. The tested runtime behavior and
libcamera source contents are unchanged. Physical acceptance above applies to
the installed revisions; build checks cover the publication recipes.

## Next integration work

1. Confirm the Omarchy firmware extractor's interface and camera-data coverage.
2. Establish a reproducible, distributable route to the remaining board inputs.
3. Collect inventory/RAW results from a second X1E machine and add its profile.
4. Reduce the kernel changes to the necessary generic and per-board series for
   integration into the current Omarchy ARM kernel.

Original standalone code is MIT. Kernel, libcamera and PipeWire retain their
respective licenses and notices; see [LICENSE](LICENSE). This repository does
not grant redistribution rights to external firmware or extracted data.
