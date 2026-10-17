# Source and data provenance

Source review completed on 2026-10-17 for this initial experimental source
release. The review found no copied proprietary implementation or vendor binary
in the standalone runtime. It is an engineering provenance review, not a legal
opinion or a grant of rights to the external camera data.

| Component | Origin and license | Release representation |
| --- | --- | --- |
| Kernel | Linux GPLv2, with existing per-file licenses and exceptions | Pinned Omarchy ARM recipe plus 66 Surface prerequisites and 69 ISP patches; original author headers, sign-offs and source notices retained |
| Omarchy recipe/docs | `omacom/omarchy-pkgs` at `c8a524abb172b5f92e89da839907d133cced4baf`, MIT | Original inputs and `packaging/kernel/COPYING.omarchy`, with local ISP packaging adjustments |
| libcamera | Public `turbineBMW/libcamera` commit `aeae1f0c7155ae2a175f27b571496f87943197f9`; GPL cam / LGPL core, other upstream files retain their licenses | Pinned source URL and complete local patch, including all six new files; hashes in `vendor/libcamera-source.json` |
| PipeWire bridge | PipeWire 1.6.9, commit `8fa27cabdc6c0c1350c69c026af5850ef0af1e26`, MIT | Original notices, upstream blob IDs, complete local diff and source manifest |
| Standalone runtime/tools | Brandon Williams's local ISP development and portable packaging work, MIT | File-level SPDX notices; original lab import hashes in `vendor/runtime-origin.json` |
| IPE bank, IFE tuning and AWB reference data | Generated/extracted using the Surface Windows camera stack | Excluded; redistribution terms and a public extraction workflow remain unresolved |
| ICP firmware | Firmware requested by each board's device tree | Excluded; supplied separately by the machine owner |

## What was checked

- The complete libcamera patch reconstructs every file of the accepted 1,515-file
  source snapshot. The previous tracked-only diff omitted six additions; these
  are now included. The recipe fetches the public base by commit and verifies
  the archive and patch hashes. The source tree's COPYING.rst and LICENSES are
  retained and installed with the private package.
- All 18 PipeWire upstream blob IDs were checked against the pinned public
  source. Only `gstpipewirepool.c` and `gstpipewiresink.c` differ: video-driver
  recovery after starvation and timestamp handling. See
  `vendor/pipewire-working.patch`. The vendored bridge preserves upstream MIT
  copyright notices and COPYING.
- Kernel patch bodies and signatures were not rewritten for the license audit.
  Their author history and notices remain intact. `ISP-SOURCES.json` records
  the extra series and hashes. The sensor import patch records the
  STMicroelectronics VD55G0 source revision and its GPL licensing; observed
  Surface sensor register sequences are documented in the patch history.
- Original standalone files now carry MIT notices. Profiles, unit files, JSON
  manifests and documentation are covered by `REUSE.toml`. Kernel patch
  containers use the kernel's GPLv2 distribution terms; this does not erase
  embedded per-file dual licenses or syscall exceptions.
- Firmware, captured images, local session logs, extracted assets, built
  packages and source checkout/build directories are excluded from the Git
  export. Public keys and patch signatures are verification inputs, not
  private keys.

## Protocol implementation boundary

`runtime/icplab.py` implements the debugfs transport and HFI packet packing.
`runtime/ipeframe2.py` implements API 2.52 payload construction using Python
`struct`, offsets and numeric protocol identifiers. Its comments identify
firmware DWARF/layout observations, older CamX Kona layout comparisons and
observed program-descriptor behavior. Those references are retained; this is
not described as a clean-room implementation. Program instructions and lookup
tables are read from separately supplied files rather than embedded in the
source.

`runtime/pd10.py` implements NumPy packing/unpacking using format dimensions
and measured bit ordering, with a CamX stride-formula reference retained.
`runtime/pd10-fast.c` is the native conversion/downscale implementation checked
against that Python reference. Neither contains a vendor source header or
embedded tuning payload. The remaining runtime implements capture leases,
shared-memory handoff, adaptive control, service lifecycle and publication.
The original lab directory was not version-controlled; its import hashes are
an origin record, not an upstream commit history.

## External data remains separate

Not calling Windows DLLs while streaming does not establish redistribution
rights for precomputed outputs. Do not commit IPE programs, extracted tuning
or lookup tables, firmware, Windows binaries, private capture data or raw logs.
The importer copies explicitly supplied local inputs; it does not download
those inputs or grant rights to distribute them.

Omarchy's existing firmware extractor is the intended firmware delivery route.
Its exact interface and coverage still need to be established. In particular,
do not assume it also produces the qualified IPE bank, IFE YAML and AWB data.
[INSTALL.md](INSTALL.md) documents the exact firmware destination and separates
that requirement from the other camera assets.

## Contributions

Keep existing notices and attribution when modifying imported code. New
standalone runtime contributions use MIT; kernel and libcamera changes follow
the license of the file being changed. Preserve real author/sign-off history;
do not add sign-offs on behalf of someone else. Record the source URL, commit,
license and changes for any new imported code or data.

Run `reuse lint` from the repository root to check notice coverage. Passing
REUSE checks metadata, not ownership, patent rights or data redistribution
rights. Source origins are recorded here and in the adjacent manifests.

References: [REUSE specification](https://reuse.software/spec-3.3/),
[kernel licensing rules](https://docs.kernel.org/process/license-rules.html),
[libcamera contribution requirements](https://libcamera.org/contributing.html).
