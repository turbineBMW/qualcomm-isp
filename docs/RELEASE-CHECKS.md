# Initial source publication checks

Checked on 2026-10-17, separately from the physical camera acceptance recorded
in the README:

- A fresh `makepkg --nobuild` using the public libcamera URL and complete patch
  reproduced all 1,515 files of the accepted private libcamera source snapshot.
- `qualcomm-isp-runtime 0.1-4` built successfully; its check phase passed 13
  handoff tests, 5 daemon tests, 16 capture-lease tests and 6 byte-identical PD10
  conversion comparisons, including invalid-input rejection.
- All 15 installed runtime Python modules have identical parsed syntax trees
  to the publication source; the native PD10 source differs only by two license
  comments. The running camera packages were not replaced for publication.
- All 135 extra kernel patch hashes match `ISP-SOURCES.json`. All 482 kernel
  patch/signature files match the previously prepared and built package inputs.
- All 18 PipeWire upstream blob IDs match the pinned 1.6.9 source. The complete
  local diff contains the pool recovery and sink timestamp changes.
- `reuse lint` passed against the exact source export, with no missing or
  unused license texts, missing copyright notices or invalid SPDX expressions.
- The export was checked for excluded firmware/assets/build products, captures,
  external symlink targets and common credential/private-key signatures. The
  only binary files are 150 preserved upstream detached patch signatures.
- The runtime package contains its MIT license, project licensing explanation,
  PipeWire copyright/license and provenance documentation.

The kernel was not rebuilt for these documentation/license changes. The
libcamera source-delivery change was verified by exact reconstruction rather
than another compilation. These checks do not qualify additional hardware or
establish redistribution rights for the excluded camera data.

## Portable board-data preparation (2026-10-17)

- Fresh MSI extraction produced both supported input hashes and the accepted
  ICP firmware hash. The exact Microsoft package and paths are documented in
  [PREPARE-BOARD-DATA.md](PREPARE-BOARD-DATA.md).
- The first complete 625-entry build passed the runtime bank validator and
  comparison with the installed accepted assets: 10,000 byte-identical hardware
  files and 1,250 striping files differing only in four process-pointer fields.
  AWB matched except for the rebuilt helper hash; IFE settings matched except
  for the disabled CCM matrix. The guide records these exceptions and the
  canonical comparison digest.
- A second complete build used a clean directory containing only the 16
  preparation source files and the two required runtime Python modules, plus
  freshly extracted MSI inputs. It finished in 269 seconds and produced the
  same comparison counts and canonical digest against the accepted assets.
  No saved lab blobs or precompiled helper were copied into that workspace.
- Seven synthetic boundary tests pass, covering unsupported input rejection,
  output preservation, symlinks, source-directory exclusion, worker limits and
  strict striping comparison exceptions. The native loader separately rejected
  empty and unsupported DLLs before execution.
- REUSE passes for all 573 source files. The staged additions contain source and
  documentation only, with no binary inputs, generated assets, symlinks or
  common credential/private-key signatures. Python and JSON sources parse.
- Runtime recipe `0.1-6` includes the linked preparation, installation, extractor
  and provenance guides. Its regenerated source archive matches the allowlisted
  source files and passes `makepkg --verifysource`. Runtime code is unchanged;
  no new binary package build or installation is claimed for this docs update.

## Regenerated asset deployment and acceptance (2026-10-17)

- Installed the clean-workspace outputs alongside the original assets, verified
  all 12,504 output hashes and root ownership, then selected them in the runtime
  configuration. The previous assets and configuration were preserved for rollback.
- Installed preflight passed. A 120-frame native 4K RGBA PipeWire consumer
  completed successfully using the regenerated bank. The session reported no
  cleanup errors and restored sensor controls; ICP unloaded and runtime usage
  returned to zero after streaming stopped.
- The owner reported all requested physical checks passed: Snapshot, Chromium
  getUserMedia, suspend/resume while idle and while streaming in Chromium, with
  image quality and recovery unchanged. These are owner-reported acceptance
  results, separate from the automated streaming check.
- Kernel `7.2.8-13.1-omarchy-isp+`, private libcamera `0.7.2-2` and runtime
  `0.1-3` remained installed throughout. This verifies regenerated assets with
  the accepted binaries; it is not a new kernel/package build qualification.

Reproduction by another Surface owner remains required. No additional board,
sensor or driver version is qualified by these results.
