# Omarchy firmware extractor integration

Reviewed on 2026-10-17 against Omarchy's
[`qcom-firmware-extract` source](https://github.com/omacom/omarchy-pkgs/blob/eb420e26805c838d1704887fadbadeb61eaf929a/pkgbuilds/qcom-firmware-extract/qcom-firmware-extract)
and [package documentation](https://github.com/omacom/omarchy-pkgs/blob/eb420e26805c838d1704887fadbadeb61eaf929a/pkgbuilds/qcom-firmware-extract/README.package.md).
The reviewed package recipe is `1-5`; the repository commit is
`eb420e26805c838d1704887fadbadeb61eaf929a`.

## What it supplies

| Runtime input | Extractor coverage | Remaining work |
| --- | --- | --- |
| `CAMERA_ICP.mbn` | Copies missing firmware requested by the running DT from an owner's driver store or installer stage | Supply the matching camera driver package; verify the tested hash |
| `bank/bank.json` and IPE programs | Not generated or copied as a bank | Generate with `preparation/prepare.py` from the supported local driver files |
| `ife.yaml` | Not generated | Produced by the same preparation workflow |
| `awb-reference.json` | Not generated | Produced by the same preparation workflow |
| `local-data-manifest.json` | Created by preparation or the local board-data importer | Retain with the generated/imported assets |

No extractor change is needed for the camera firmware's existing filename:
our DT requests `qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn`. The extractor
searches for that basename and installs it under
`/usr/lib/firmware/updates/qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn`.
This establishes compatibility with the copy mechanism; it does not qualify
other firmware versions, sensors or boards.

## Use with the qualified Surface profile

With the Omarchy extractor installed and a kernel/DT that names the ICP image:

```sh
qcom-firmware-extract --list-missing
```

If the camera firmware is missing, an explicitly supplied directory can be
used instead of disk discovery. It may be the same machine model's complete
Windows `FileRepository`, or a directory containing its selected camera driver
package. The extractor copies all missing DT firmware it finds there:

```sh
ASKPASS_REASON="Install missing device-tree firmware from the selected local Windows driver directory" \
  sudo -A qcom-firmware-extract --install --no-rebuild \
  -d /path/to/selected-driver-directory
```

`--no-rebuild` fits our current on-demand ICP loading from the root filesystem.
If the same run also installs firmware needed in an initramfs, complete the
normal Omarchy boot-image update for those devices. The installer already
coordinates its own boot-image rebuild.

An existing firmware image is skipped, not upgraded or replaced. Verify the
installed file against the validated SHA-256 in [INSTALL.md](INSTALL.md).
The local Surface driver-package archive contains a `CAMERA_ICP.mbn` matching
that hash. Another local camera-driver archive contains a different version,
so matching the filename alone is insufficient.

Differing copies in a driver store are refused when the extractor cannot find
a unique compatible variant. For this ICP image, select the intended camera
package explicitly rather than assuming the newest file is suitable. Do not
add ICP to the extractor's `provided.d` exclusion list: we expect the extractor
to supply it. Firmware names omitted by the running DT will not be discovered.

## Verified behavior

The source-only integration check uses synthetic firmware in temporary
folders, with the extractor's existing test-root hooks. It does not read a real
Windows partition, load ICP, alter system firmware or rebuild a boot image:

```sh
python tools/check-firmware-extractor.py /path/to/reviewed/qcom-firmware-extract
```

Four checks passed against the pinned script:

1. Missing camera firmware is listed and staged at the exact DT path.
2. Installation preserves the bytes and records the filename/checksum.
3. An existing installed camera image is not replaced.
4. Different camera-image variants are refused instead of guessed.

Reviewed script SHA-256:
`c04b78934a87c8ac2142aa3468f269a53ca59b59d32193e5ee46245759234629`.
These are focused integration checks, not an end-to-end installer or Windows
partition test. The upstream full test script did not complete in this local
review environment: a disk-scanning fixture lacked its temporary `root/run`
directory. No upstream full-suite pass is claimed.

## Separate board-data preparation

[PREPARE-BOARD-DATA.md](PREPARE-BOARD-DATA.md) documents the supported Microsoft
driver package and `preparation/prepare.py`. The tool checks both input hashes,
builds the helper from source, generates the complete 625-entry bank, IFE YAML
and AWB references, and records source/input/output hashes. The helper executes
selected functions from the owner's Windows camera DLL during preparation.
The runtime does not call those functions while streaming.

The generated bank has passed comparison with the accepted local assets, with
the specific exceptions documented in the preparation guide. Deployment on the
qualified Surface passed a 4K streaming check, and the owner confirmed Snapshot,
Chromium and idle/streaming suspend/resume acceptance on 2026-10-17. Reproduction
by another Surface owner remains outstanding. Keep vendor files and generated
assets outside the source repository. Other X1E sensors require separate tuning
and qualification.
