# Prepare Surface board data locally

This workflow prepares the **Surface Pro 11 Denali OLED / IMX681 front camera**
assets for the qualified 3840×2176 ISP path. It is specific to the driver versions
below. It does not qualify another Surface variant or another X1E sensor.

The repository supplies source code. You supply two Windows driver files locally.
Preparation builds a small ARM64 helper which executes selected functions from
that DLL to calculate IPE programs and AWB references. It requires native
**aarch64 Linux**, runs as your normal user, and needs no camera access or root
privileges. The camera runtime does not execute the DLL while streaming.

## Obtain the inputs

Microsoft's [Surface Pro 11 drivers and firmware download page](https://www.microsoft.com/en-us/download/details.aspx?id=106119)
lists the tested package:
[`SurfacePro11_ARM_Win11_26100_26.041.12746.0.msi`](https://download.microsoft.com/download/92821b24-203a-4ce5-9a3a-5df3160a2fe3/SurfacePro11_ARM_Win11_26100_26.041.12746.0.msi).
Download it yourself and keep it outside this repository. Later packages may
contain different camera components; the preparation tool rejects unsupported
input hashes. There is no override for substituting a different DLL.

On Arch/Omarchy, install `msitools` for MSI extraction, and `clang`, `openssl`,
`python-numpy` and `python-pyyaml` for preparation, using your usual package
installation process. `openssl` supplies the helper's headers and crypto library.

Extract to a separate directory:

```sh
msiextract -C "$HOME/qualcomm-isp-drivers" \
  "$HOME/Downloads/SurfacePro11_ARM_Win11_26100_26.041.12746.0.msi"
```

The required files are:

| Path inside the extraction | Bytes | SHA-256 |
| --- | ---: | --- |
| `SurfaceUpdate/camavs/QcDeviceMFT8380.dll` | 23,998,368 | `c241b7fbb2ec54e439752a1ea7ad25da10ca740012a54bd0e7a87ea94a141c35` |
| `SurfaceUpdate/camfrontsensorextension/com.surface.tuned.ffc_imx681.bin` | 6,465,007 | `2c1c7fd9090e0bf338f44bd9de785509c1fbebc975facc5286f12865cf675f1d` |

Both corresponding driver INFs report `04/25/2025,1.0.4258.7908`. The same
files may be supplied from your Windows driver store if their hashes match.

`SurfaceUpdate/qccamisp/CAMERA_ICP.mbn` in this MSI also matches the firmware
hash documented in [INSTALL.md](INSTALL.md). Install it separately using the
[Omarchy firmware extractor](FIRMWARE-EXTRACTOR.md); this preparation command
does not install firmware.

## Generate the assets

From the repository root, check the selected files and a **new output directory
outside the source checkout**:

```sh
python -I preparation/prepare.py \
  --dll "$HOME/qualcomm-isp-drivers/SurfaceUpdate/camavs/QcDeviceMFT8380.dll" \
  --tuning "$HOME/qualcomm-isp-drivers/SurfaceUpdate/camfrontsensorextension/com.surface.tuned.ffc_imx681.bin" \
  --output "$HOME/qualcomm-isp-assets" --check-inputs
```

Input checking does not execute the DLL and also works on other host architectures.
Remove `--check-inputs` to generate the complete bank on aarch64:

```sh
python -I preparation/prepare.py \
  --dll "$HOME/qualcomm-isp-drivers/SurfaceUpdate/camavs/QcDeviceMFT8380.dll" \
  --tuning "$HOME/qualcomm-isp-drivers/SurfaceUpdate/camfrontsensorextension/com.surface.tuned.ffc_imx681.bin" \
  --output "$HOME/qualcomm-isp-assets" --jobs 2
```

Allow several minutes and approximately 1 GiB of free space for temporary work
and the resulting approximately 258 MiB directory. Progress is printed every
25 entries. Existing outputs are refused. A failed run can leave an incomplete
destination without `local-data-manifest.json`; inspect it before removing it
or select a new destination for a retry.

Outputs:

- `bank/`: 625 entries, covering 25 gains from 1× to 64× and 25 tone gains from
  1× to 8×; `bank.json` records the grid, geometry and hardware-file checksums.
- `ife.yaml`: the qualified active IFE settings and extracted gamma curve.
- `awb-reference.json`: extracted references and DLL-calculated reference results.
- `preparation.json`: input, source and derived-input hashes, compiler details
  and validation result.
- `local-data-manifest.json`: output file hashes; written last on success.

The runtime's own bank validator checks geometry and hashes before preparation
completes. The output already has the layout expected by the installer; the
local board-data importer is unnecessary for these generated assets. Continue
with [INSTALL.md](INSTALL.md) to place them in the configured asset directory,
run preflight, and activate the services. Retain `preparation.json` with the assets.

## Verification and limits

The full generated bank was compared with the accepted local bank:

- 10,000 of 11,250 hardware files were byte-identical.
- The other 1,250 files were striping payloads of 155,752 bytes. Their only
  differences were four retained process pointers at `0x144d0`, `0x14968`,
  `0x14e10` and `0x152b8` (eight bytes each). These fields also varied between
  earlier lab builds. Comparison masks them; generation preserves them.
- AWB JSON matched apart from the rebuilt helper hash. Parsed IFE settings
  matched apart from the disabled CCM matrix, now explicitly identity with
  `ccm_strength: 0.0`.

To compare with an existing accepted asset directory without opening a camera:

```sh
python -I preparation/compare-assets.py \
  /path/to/accepted-assets "$HOME/qualcomm-isp-assets"
```

The comparator rejects changes outside those specific exceptions and checks both
bank manifests. Its canonical comparison SHA-256 for the accepted grid is
`8796ff9a8f07d115b8b6ed91e4227e7a6bd716f7a22ca4f275dcb640b2e09da9`.
This is a comparison digest, not an archive checksum.

The regenerated assets were installed on the qualified Surface on 2026-10-17.
A 120-frame native 4K RGBA PipeWire consumer completed successfully; sensor
controls were restored, ICP unloaded and runtime usage returned to zero. The
session report confirmed use of the regenerated bank and no cleanup errors.
The owner then confirmed Snapshot, Chromium getUserMedia, idle suspend/resume
and streaming suspend/resume all passed, with image quality and recovery
unchanged. Kernel and runtime binaries were unchanged during these tests.
Another Surface owner must still reproduce and test the workflow; this result
does not qualify other machines or driver versions.

The white-balance reference gains remain provisional values from the qualified
session, not per-device calibration. Lens shading and IFE CCM remain disabled.
The bank uses a bounded experimental tone controller; it does not recreate
Windows AE/DRC behavior. The helper contains measured, version-specific ABI
layouts and offsets, not a general Windows execution environment.

Source-only boundary checks:

```sh
python -I preparation/test-preparation.py
```

Keep the driver package, DLL, tuning, generated programs and raw diagnostic logs
local. The MIT license covers our preparation source; it grants no rights to
redistribute the external inputs or outputs. See [PROVENANCE.md](PROVENANCE.md)
for the source origins and implementation boundary.
