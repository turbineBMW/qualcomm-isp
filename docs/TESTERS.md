# First report from another X1E machine

Start with inventory on the machine's working kernel:

```sh
python tools/collect-platform.py > camera-platform.json
```

This reads sysfs/device-tree metadata. It does not load ICP, open a camera,
change the boot setup, or include the full kernel command line or machine
serial numbers. Include the machine model, current kernel, and camera-platform
JSON when reporting a new board.

## Preserve Windows drivers and stage firmware

Other X1E owners can start with their own driver files on their current working
kernel. The Surface kernel, DTB and generated camera assets are not prerequisites
for this inventory and staging work.

### 1. Export the drivers from Windows

If Windows is still available, open an **Administrator Command Prompt** and run:

```bat
mkdir C:\X1E-Drivers
pnputil /export-driver * C:\X1E-Drivers
```

This exports third-party driver packages from the Windows driver store using
[Microsoft's supported PnPUtil command](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-command-syntax).
Copy the complete directory to storage accessible from Linux, and preserve it
before removing Windows. Retain the camera DLLs, sensor tuning and INF files as
well as firmware: later ISP preparation needs more than firmware alone.

An existing copy of `Windows/System32/DriverStore/FileRepository` is also a
usable source. Use drivers from the same laptop model. If Windows is already
gone and no driver backup exists, obtain that model's driver package from its
manufacturer; record the package URL and version. Archive extraction methods
vary by manufacturer, and a package may not contain every camera component.

### 2. Stage missing firmware under Linux

With Omarchy's `qcom-firmware-extract` installed, run:

```sh
qcom-firmware-extract --list-missing

qcom-firmware-extract --stage "$HOME/x1e-firmware-stage" \
  -d /path/to/X1E-Drivers
```

Replace `/path/to/X1E-Drivers` with the exported directory. With an explicitly
supplied, readable driver directory and a writable staging destination, these
commands need no root access. Staging copies files into the chosen directory;
it does not install firmware, rebuild a boot image or open a camera.

The extractor uses firmware names from **the running machine's device tree**
and stages only missing firmware. Existing installed files, or firmware absent
from that DT, will not be collected by these commands. An empty result therefore
does not establish that camera firmware is unavailable or that the camera works.
Preserve the complete driver export even if staging finds nothing.

Identical duplicate files are accepted; differing variants require a unique
compatible match or are skipped. Review the reported missing/ambiguous names
before any installation. See the pinned [extractor documentation](https://github.com/omacom/omarchy-pkgs/blob/eb420e26805c838d1704887fadbadeb61eaf929a/pkgbuilds/qcom-firmware-extract/README.package.md)
and our [integration review](FIRMWARE-EXTRACTOR.md). The Surface installation
path in that review is specific to the Surface profile; another board uses its
own DT firmware paths.

### 3. Inventory the exported camera drivers

From this repository's root, using Python 3.10 or newer:

```sh
python -I tools/inventory-drivers.py /path/to/X1E-Drivers \
  --output camera-drivers.json
```

This uses only Python's standard library. It works on an extracted driver export
or unpacked OEM package; it does not unpack MSI/EXE archives itself. It reads
regular files, never executes driver code, and needs no root or camera access.
The output filename must be new and outside the scanned directory. Without
`--output`, JSON goes to stdout and the short scan summary goes to stderr.

The report includes:

- Candidate firmware (including split images), DLLs, sensor modules, tuning and
  package support files, with relative paths, sizes and SHA-256 hashes.
- Camera-related INF metadata: provider, setup class, `DriverVer`, catalog names
  and declared source files. UTF-16, UTF-8 and Windows-1252 INFs are supported.
- Selection reasons: filename hints, Camera/Image setup class, camera-related
  INF text, explicit source-file entries, or files beside a candidate INF.
- Filename-derived sensor hints, and duplicate filenames grouped by hash so
  identical copies can be distinguished from differing versions.
- Read/skip warnings and source-file entries that were not found.

The scanner does not include the input directory's absolute path, host identity
or raw file contents. Review the JSON before posting: relative directory names
and vendor metadata remain visible. Keep the original export local.

Exit status `0` means the scan completed without read/skip warnings; `1` means a
partial report was produced, with details in `warnings`; `2` means invalid usage
or an input/output failure. Symlinks, junctions and special files are skipped.
INF parsing is capped at 4 MiB per INF; payload hashing reads files in chunks.

These are **candidates**, not confirmed hardware or compatibility findings.
The metadata reader is not Windows SetupAPI and does not verify signatures,
choose a driver version or perform installation. It examines all source-file
section decorations, so an unmatched entry can belong to another architecture.
An empty report does not establish that the machine has no camera components.
Opaque OEM naming can require follow-up inspection; keep the full driver export.

Metadata interpretation follows Microsoft's [INF Version section](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/inf-version-section)
and [SourceDisksFiles section](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/inf-sourcedisksfiles-section)
documentation. The helper's synthetic checks can be run with
`python -I tools/test-inventory-drivers.py`; no driver files are required.

### 4. Report inventory and available inputs

Include these in a [new board report](https://github.com/turbineBMW/qualcomm-isp/issues):

- Exact laptop model and `camera-platform.json` from the command above.
- `camera-drivers.json` from the driver inventory helper, reviewed before sharing.
- Whether Windows or an exported driver directory is available.
- OEM driver package URL/version, if available.
- Missing or ambiguous firmware names reported by the extractor.
- Camera-related filenames, driver versions and SHA-256 hashes, when identified.

Keep firmware, DLLs, tuning and the exported driver directory local; do not attach
them to the public report.

**The current `preparation/prepare.py` accepts only the tested Surface DLL and
IMX681 tuning hashes.** Other machines first need their sensor, firmware and
driver versions examined and a compatible preparation path qualified. Extracting
their firmware is useful progress, but does not by itself enable the ISP camera.

## Camera bring-up stages

1. Confirm the board DT describes the actual camera sensor, CCI bus, supplies,
   clocks, CSI lanes and endpoints. Confirm the correct sensor driver binds.
2. Establish RAW capture with that board's native sensor configuration.
3. Qualify the IFE path and matching libcamera controls/metadata.
4. Qualify ICP firmware initialization, shutdown and runtime-PM cleanup.
5. Only then attempt IPE using compatible, sensor-appropriate program data.

The checked-in Denali OLED profile, DTB and program-data expectations are for
the Surface Pro 11 test device. Other machines must use their own DTB and
profile. The complete kernel recipe includes 66 Surface prerequisite patches
as a separate ordered group alongside the ISP work; it is not yet a minimal
generic X1E series.

For each qualified profile, record the kernel/source identity, sensor and
firmware versions, matching libcamera/bridge packages, successful capture,
buffer cleanup, repeated open/close, application behavior and idle/active
suspend results. A compiled DTB or enumerated node is not a camera test.

## Current evidence

- Kernel: Omarchy ARM 7.2.8-13 base, ISP candidate 7.2.8-13.1.
- Board: Surface Pro 11, Denali OLED, IMX681 front camera.
- Original runtime: 4K processed capture, Snapshot, Chromium Mozilla
  getUserMedia, and idle/active-camera system suspend passed.
- Packaged runtime: 120-frame installed capture with 137/137 leases returned;
  420-frame PipeWire consumer survived explicit sleep-hook stop/restore.
- Packaged revision 0.1-3: user confirmed Snapshot, Chromium getUserMedia and
  idle/active-camera actual system suspend. Logs confirm two clean deep-sleep
  cycles and first-frame recovery about five seconds after service restoration.
- No second X1E machine has been tested.
