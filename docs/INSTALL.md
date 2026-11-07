# Install the experimental runtime

This is currently a Surface Pro 11 Denali OLED profile on the verified
`7.3.0-25.1-omarchy-isp` kernel. Other X1E machines first need their own sensor,
firmware and DT qualification. Changing the profile does not make the Surface
IPE program bank correct for another sensor.

## Kernel

`packaging/kernel` contains the co-installable `linux-omarchy-isp` recipe based
on the Surface Pro 11 `linux-omarchy` 7.3-25.1 recipe (Omarchy ARM 7.3-25).
Build with `makepkg` on aarch64 after installing its build dependencies and
importing the recipe's public verification keys into your build user's
keyring. The recipe preserves upstream source signatures. The adjacent README
and upgrade notes originate in the pinned base recipe. The package writes the
build manifest the runtime's preflight checks
(`/usr/lib/modules/<release>/qualcomm-isp-manifest.json`); the runtime's units
start only on a kernel that has one.

Installing a kernel package is not a complete board boot setup. The tested
Surface boot entry uses the Denali OLED DTB and `iommu.passthrough=0`, with the
previous working kernel retained as a fallback. Select your own board's DTB
and follow your bootloader's kernel/UKI installation process. Review
`ISP-SOURCES.json` before rebasing onto a newer Omarchy kernel. The runtime
preflight checks the installed kernel build manifest and module identities;
a different build/profile must be qualified before enabling the worker.

## Build

On aarch64 Arch/Omarchy with base-devel and the PKGBUILD dependencies installed:

1. In `packaging/libcamera`, run `makepkg`. The recipe fetches a pinned public
   libcamera commit and applies the complete, hash-checked local patch.
2. Install the resulting `qualcomm-isp-libcamera` package with `pacman -U`,
   using the machine's elevation mechanism. This installs into a private prefix.
3. From the project root run `python tools/export-runtime.py`, then run
   `makepkg` in `packaging/runtime`. Its check phase exercises shared memory,
   capture leases, daemon lifecycle and accelerated PD10 conversion.
4. Install the resulting `qualcomm-isp-runtime` package. Installation alone
   does not enable or start its services.

The runtime package includes a private GStreamer bridge, loaded by exact path.
Neither dependency replaces system libcamera or the system PipeWire plugin.

## Install extracted ICP firmware

Omarchy's `qcom-firmware-extract` can supply the DT-named ICP image from a
locally supplied Windows driver directory. Its camera copy path has passed
focused integration checks. See [FIRMWARE-EXTRACTOR.md](FIRMWARE-EXTRACTOR.md)
for the pinned source, commands, version selection and coverage.

For the currently qualified Surface Pro 11 Denali OLED profile, install the
extracted single-file firmware as:

```
/usr/lib/firmware/updates/qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn
```

`Denali` and `CAMERA_ICP.mbn` are case-sensitive. This matches the device-tree
firmware request `qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn`. The standard
`/usr/lib/firmware/qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn` location also
works when no updates-directory copy overrides it. The kernel searches updates
before the base firmware directory; see the [kernel firmware search-path
reference](https://docs.kernel.org/driver-api/firmware/fw_search_path.html).
On this system `/lib` points to `/usr/lib`.

Example using this machine's elevation mechanism:

```sh
ASKPASS_REASON="Install the extracted Surface ICP firmware for the ISP camera" \
  sudo -A install -Dm644 /path/to/extracted/CAMERA_ICP.mbn \
  /usr/lib/firmware/updates/qcom/x1e80100/microsoft/Denali/CAMERA_ICP.mbn
```

The locally tested file is 905,880 bytes, SHA-256:

```
c214e486ea667d09766e5dbbd56442f40b6bff689dd910365c6a015a62a68c9a
```

This records the tested version, not a guarantee that other versions are
compatible. Our ICP module loads on demand from the root filesystem, so this
placement does not require rebuilding the initramfs for the current setup.
Other boards must use the firmware name requested by their own DT/profile.

Firmware alone does not supply the IPE program bank, IFE tuning YAML or AWB
references described below. The reviewed extractor does not generate these;
they remain separate locally supplied inputs. Firmware and extracted data
stay outside the GitHub source repository and runtime packages.

## Supply local board data

Review `PROVENANCE.md`. Firmware and program/tuning data are deliberately absent
from the packages. [PREPARE-BOARD-DATA.md](PREPARE-BOARD-DATA.md) documents the
supported Microsoft driver package and local generation of the complete Surface
asset directory. Generated assets already have the layout described below.
Generation and data comparison do not replace physical camera acceptance.

Alternatively, if you already have the qualified data, run the importer with
absolute paths to the bank directory, IFE tuning YAML and AWB reference JSON:

```sh
python tools/import-board-data.py --bank /path/to/bank --ife /path/to/ife.yaml \
  --awb /path/to/reference.json --destination /path/to/local-assets
```

Place those files in the assets directory selected by
`/etc/qualcomm-isp/config.json`. The default is
`/var/lib/qualcomm-isp/assets/surface-pro-11-denali-oled`. Keep configuration,
runtime code and program data owned by root and unwritable by camera clients.
The profile lists the separately installed firmware filename and the kernel
build manifest used to reject mismatched drivers.

Run `python -I /usr/lib/qualcomm-isp/isp_config.py`. It checks the selected
board, kernel/module identities, local inputs, firmware and sensor enumeration
without loading ICP or opening a camera stream.

## Activate

The desktop user needs membership in `video` to access the worker socket and
camera devices. Log in again if group membership changes. The daemon derives
the capture UID from `SO_PEERCRED`; the client cannot name another user.

If migrating from the local `sp11-ipe-preview` services, first close camera
applications and stop/disable `sp11-ipe-publisher.service` in the user manager,
then stop/disable `sp11-ipe-worker.socket` and `sp11-ipe-sleep.service` and stop
`sp11-ipe-worker.service` in the system manager. Both stacks share a hardware
lock, but only one publisher/worker stack should be enabled.

Enable/start the system `qualcomm-isp-worker.socket`; enable (do not manually
start) `qualcomm-isp-sleep.service`. Enable/start the user
`qualcomm-isp-publisher.service`. Use the machine's approved elevation mechanism
for system units. The sleep service runs automatically around sleep.

The node is `sp11-ipe-preview`, displayed as **Built-in Front Camera**, preserving
the existing desktop camera identity. After confirming it works, a WirePlumber
rule can hide the raw node named in the board profile. The kernel sensor must
remain enabled for the ISP worker. The existing local SP11 rule already does this.

## Verify and recover

Check one camera application, then close it and confirm ICP unloads and
`/sys/bus/platform/devices/ac01000.icp/power/runtime_usage` returns to zero.
Session reports are in `/var/lib/qualcomm-isp/sessions`. Check repeated opens
and both idle and active-camera suspend/resume after deployment.

To roll back, close camera applications, stop/disable the new user publisher
and system socket/sleep service, stop the new worker, and re-enable the old
stack. Restore the original WirePlumber camera entry if booting a kernel that
has no ISP support. The previous runtime, packages and boot entries remain
available during this migration.

## Experimental OV13858 rear ISP

The rear path has been tested on this Denali OLED with the existing qualified
lab kernel. It requires private libcamera package `0.7.2-4` and runtime `0.1-7`
or later. It uses the kernel's temporary native-GRBG register override, restored
when the session ends; the source CSID fix still needs a kernel build and boot
qualification. See [the experiment](../experiments/ov13858/README.md) for input
hashes, geometry, frame association and measured results.

Generate a separate local bank with `preparation/prepare.py --sensor ov13858`,
using the exact rear tuning file and DLL described there. Import it with
`tools/import-board-data.py` as for the front bank, choosing a new destination.
Do not reuse the front bank. Copy `/etc/qualcomm-isp/config.json` to
`/etc/qualcomm-isp/rear.json`, changing `profile` to
`/usr/share/qualcomm-isp/profiles/surface-pro-11-denali-oled-rear.json` and `assets`
to the imported rear bank directory. Keep the installed `cam`, library and bridge
paths. The configuration and imported data remain local.

Enable `qualcomm-isp-rear-worker.socket` in the system manager and
`qualcomm-isp-rear-publisher.service` in the user manager. Desktop applications
then see **Built-in Rear Camera (ISP)**. The two ISP workers share an exclusive
hardware lock: close the previous camera stream before selecting the other.
The five-second idle grace period plus retry can make switching take roughly
seven to twelve seconds. Simultaneous front/rear capture is not supported.

To hide the duplicate software rear-camera entry, add a WirePlumber rule matching
`node.name = "libcamera_input._base_soc_0_cci_ac15000_i2c-bus_1_camera_10"` with
`actions = { update-props = { node.disabled = true } }`, following the existing
front-camera rule. Restart WirePlumber after closing camera clients. This only
hides its desktop entry; the private worker can still open the sensor.

Rollback: close clients, disable the rear user publisher and system socket,
stop the rear worker service, remove the duplicate-hiding rule and restart
WirePlumber. Keep the local tuning assets if you intend to retry. Front capture
has its own configuration, warm-start state and capture directory.

The sleep hook now quiesces both workers and restores exactly the previously
active units. Automated lifecycle tests cover that logic; actual rear streaming
suspend/resume and wider lighting/colour qualification are still outstanding.
