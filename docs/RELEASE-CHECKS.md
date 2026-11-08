# Initial source publication checks

## Rear denoising precision, runtime 0.1-15 (2026-11-08)

- Corrected the ANR reserve extraction by four bytes, retaining all four
  serialized pass selectors. Preparation tests cover malformed selectors and
  flags. Rebuilt a separate 625-entry rear bank with P010 input/references and
  NV12 display, preserving tone, color, sharpening and temporal-filter programs.
- Same-input hardware replays reduce broad purple/green contours. Reserve
  correction alone is insufficient; retaining denoised chroma precision is the
  material improvement. Residual shadow tint remains. Eight-bit input promotion
  does not recover native sensor precision. Early IFE CCM remains experimental.
- The initial two-input P010 allocation exceeded the dedicated memory pool and
  cleaned up successfully. After explicit retry approval, the shared-input
  implementation passed hardware replay and a 180-output adaptive live capture.
  It retains two display slots and stages the next input in CPU memory.
- Package checks passed: 58 unittest cases and six PD10 byte-identical checks;
  preparation nine and color selection five tests also passed. All 625 bank
  manifests validated. DLL scratch requirements fit the qualified 6 MiB limit.
- Installed front/rear/front/cancelled-rear switching passed with first images
  in 3.06/3.41/3.85/5.10 seconds. A desktop consumer received 90 native 4K frames
  and saved a 3840x2160 PNG. This is not a Gaze frame-rate qualification.
- Package integrity: 59 files, zero altered. Publishers and WirePlumber active;
  ICP unloaded, runtime usage zero, temporary Bayer overrides empty, and no
  matching kernel faults in the diagnostic interval. No new suspend test.
- Rear configuration and previous asset selection are backed up locally. Front
  remains on its existing NV12 bank; mild neutral rear shading is retained.

## Mild neutral rear shading, libcamera 0.7.2-6 (2026-11-08)

- Added an opt-in, bounded neutral-LSC model for the rear RAW control algorithms.
  The default remains off. Non-neutral meshes, gains above 1.25 and nonlinear
  response remain unsupported with RAW AE/AWB; front keeps its existing guard.
- Hardware off/unity/mild trials passed at fixed controls. A smooth mesh capped
  at 1.2 lifted sampled outer regions roughly 7–10% in linearized pre-IPE values,
  while center changed below 1%. No per-channel color correction was fitted.
- Adaptive capture passed; saved native RAW and diagnostic histograms still
  agree exactly. Package source reconstruction matches all 17 provenance hashes;
  the model regression passed during package build. Installed libcamera84files
  and runtime58files both report zero altered files.
- Installed desktop front/rear/front/cancelled-start regression passed with
  first frames in 5.13/3.07/3.31/2.25 seconds. Local rear tuning is a separate,
  backed-up profile; imported assets and package-owned profiles are intact.
- See [model and qualification limits](../experiments/shading/README.md).

## Rear colour correction, runtime 0.1-14 (2026-11-08)

- Corrected native GRBG selector 3 → 1 after paired real-scene RAW/pre-IPE
  comparison proved a red/blue inversion before white balance. Independent
  red-only/blue-only gain trials verified control identity. The original sensor
  color-bar interpretation did not establish real-scene identity.
- Three bounded 180-frame diagnostic captures completed with clean teardown.
  Both saved-input color replay comparisons used one allocation set and passed
  35 submissions each; fixed-temperature and repeated baselines were identical.
  The first historical replay attempt exposed a test-harness reallocation error;
  cleanup succeeded, the user approved the corrected retry, and it passed.
- Package check phase passed 52 lifecycle/transport/demand tests and six PD10
  comparisons. Installed runtime has 58 files and zero alterations. Desktop
  front/rear/front and cancelled rear startup passed with first images in
  2.13/3.00/3.67/2.56 seconds. Existing camera assets and firmware remain intact.
- Added opt-in paired pre/post-IPE frame retention for bounded diagnostics;
  disabled by default. Fixed preparation's branch-specific CCM/CV CCT selection;
  five synthetic regressions and seven existing preparation checks pass.
- Broader lighting, unit calibration/shading, Windows still-capture parity and
  suspend/resume remain separate qualifications. Optional CCT sets remain off.

## Gaze switching follow-up, runtime 0.1-13 (2026-11-08)

- User acceptance found black viewfinders when switching front -> rear ->
  front before rear startup finished. At 09:59:05 the rear first requested
  hardware; old front idle grace and two five-second retries delayed its first
  frame until 09:59:18. The user had selected front at 09:59:17. Rear startup
  nevertheless set last_seen ten seconds into the future, then held the lock
  for the additional five-second idle grace. Front kept failing to acquire it
  until Gaze closed at 09:59:32; rear finally stopped at 09:59:34.
- The original rear session processed 236 frames with non-black luma and clean
  teardown. No kernel fault or ISP crash caused this incident. The earlier
  separate-process capture tests did not cover cancelled startup in Gaze.
- Runtime 0.1-13 releases an unused camera when its peer is requested, removes
  future consumer-presence time at startup, and reports lock contention as
  `busy` for one-second retries. Graph-query failure preserves ownership.
- Installed and tested through Gaze's actual picker. Rear imagery displayed;
  cancelled-start trace: rear first frame 10:09:42, release at 10:09:43,
  cleanup complete 10:09:44, front first frame 10:09:46 following selection
  at 10:09:42. No Gaze source change was needed. Package check phase passed
  52 lifecycle/transport/demand tests plus six PD10 comparisons. The added
  single-process, clock-synchronised capture regression passed: front first
  image 1.76 s, rear 3.66 s, front 2.69 s, front after cancelled rear startup
  2.47 s, each with ten non-black frames and increasing timestamps.
- Rear imagery has a conspicuous blue cast in this lighting. Colour/AWB
  qualification is still outstanding; this switching fix does not correct it.

## Rear activation and conditional routing, runtime 0.1-12 (2026-11-08)

- Imported the existing, hash-verified OV13858 adaptive bank; installed the
  separate rear configuration and enabled its socket and user publisher.
- Initial rear capture exposed stale sensor mode blanking limits: 115,566 us
  exposure exceeded the adaptive metadata envelope, causing a worker timeout.
  Private libcamera `0.7.2-5` refreshes limits after mode configuration. With
  1056x784 explicitly selected before capture, 180 desktop frames passed;
  247 processed frames including idle grace stayed at 66,697 us with no
  cleanup errors. The validation limit was not relaxed.
- Front 360p -> rear 1080p -> front 720p -> rear 4K -> rear 360p completed
  90 frames per consumer (7.35, 13.42, 11.41, 13.37, 6.09 seconds including
  startup/switching). The exclusive hardware lock rejected initial attempts
  during the previous camera's idle grace; automatic retry succeeded.
  Rear sessions exited normally, the Bayer override was restored, ICP unloaded,
  and runtime usage returned to zero. No new kernel faults were observed.
- WirePlumber policy checks passed for both publishers, each independent
  fallback, both fallbacks, both monitor/publisher startup orders and three
  publisher restarts. Only the two ISP Video/Source nodes remain when both are
  available. Kernel sensors and direct V4L2/libcamera access remain enabled.
- Runtime package check phase passed all 44 lifecycle/transport tests and six
  PD10 reference comparisons. The private libcamera and runtime were rebuilt
  and installed without replacing system libcamera or changing the kernel.

Rear remains experimental: broad colour/lighting and rear suspend/resume
qualification have not been performed in this session. Front and rear cannot
stream simultaneously. The conflicting-resolution consumer limitation below
is unchanged.

## Publisher formats and recovery, runtime 0.1-11 (2026-11-08)

- Runtime package built with all 44 transport/lifecycle tests and six PD10
  reference comparisons passing. The complete bridge diff reconstructs the
  vendored sources from all 18 verified pinned upstream blobs; modified-file
  hashes and patch hash were regenerated.
- Synthetic desktop integration passed against the built bridge: four output
  sizes, a 1080p-capped consumer, native default, rapid reconnects/size changes,
  compatible concurrent consumers, full-range gray and alpha, unsupported idle
  negotiation without replacing the node, idle stdin shutdown, one-error
  in-process rebuilding and the three-rebuild rate limit. Reproduce with
  `tools/test-publisher.py`; see [publisher notes](PUBLISHER.md).
- Installed 0.1-11 on the Surface Pro 11, running kernel
  `7.3.0-25.2-omarchy`. Package verification reported 50 files, zero altered.
  An unsupported 1024x768 request failed while the publisher remained available.
  Ninety-frame consumers then passed at 1920x1080, 1280x720, 3840x2160 and
  640x360, with the same publisher process and node throughout and zero service
  restarts. ICP unloaded and runtime usage returned to zero after idle.
- A fresh native 4K session after the worker stopped passed 90 frames in 6.09 s,
  followed by the same idle cleanup. No SMMU/translation/invalid-command/oops
  messages appeared in the kernel journal during these checks.
- The incompatible-concurrent-client stall documented in PUBLISHER.md remains
  unresolved and also reproduces with the unchanged 0.1-10 publisher. Gaze GUI,
  rear-camera output and suspend/resume were not retested for this change.

## Initial checks (2026-10-17)

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

## Driver inventory helper (2026-10-17)

- Eleven synthetic checks cover UTF-16/UTF-8/Windows-1252 metadata, string tokens,
  case-insensitive source-file references, source subdirectories, duplicate and
  differing payloads, split firmware, unreadable INFs, symlink/FIFO exclusion,
  unsafe references, malformed/oversized INFs, deterministic output and report
  destination protection. Fixtures contain no vendor data.
- A read-only scan of the locally extracted Microsoft Surface driver package
  examined 1,126 regular files and 205 INFs. It identified 229 candidate files
  across 24 camera-related INF candidates with no read/skip warnings. The three
  known DLL, IMX681 tuning and ICP firmware hashes matched the qualified inputs.
- The scan identified differing copies of `com.qti.tuned.default.bin` without
  choosing one. The report contained no absolute input path or raw file contents;
  the owner-package report remains local and is not committed.
- These checks establish inventory behavior on synthetic fixtures and one real
  package. Candidate matches can include unrelated support packages and do not
  establish camera support or identify the active sensor on another machine.

## Kernel 7.3-25.1 and runtime 0.1-8 (2026-11-07)

- `packaging/kernel` now holds the Surface Pro 11 `linux-omarchy` 7.3-25.1
  recipe (`turbineBMW/surface-pro-11-omarchy` 213b706) built as
  `linux-omarchy-isp`. The 69 ISP patches are byte-identical to the
  7.2.8-13.1 series (2300-2368), renumbered 3300-3368; `tools/kernel-sources.py`
  regenerated `ISP-SOURCES.json` and checks that every patch is in `source=()`.
- The installed kernel was built from this recipe (53 min full build) and then
  repackaged with `makepkg -R` after two packaging-only changes: `package()`
  writes `build-artifacts.json`, and the package no longer conflicts with or
  replaces `linux-omarchy-n1x`. The repackaged `vmlinuz` is identical to the
  booted one, the 8,507 modules match, and the generated manifest equals the
  hand-made one it replaces apart from its status line.
- `qualcomm-isp-runtime 0.1-8` (stop-path hardening, `proctree.py`, bridge
  `1.6.9-icp-recovery-timestamps-nolink`) built with its check phase: handoff
  13, daemon 9, session 2, capture-lease 16 and the PD10 and sleep-hook tests
  passed. Every installed Python module is identical to the source.
- Installed together with `pacman -U`, overwriting only the two files that had
  been installed by hand (the manifest and `proctree.py`); `pacman -Qkk`
  reports no altered runtime file. Preflight passed and a 150-frame native 4K
  RGBA consumer completed in 6.7 s with ICP unloaded afterwards.
- `reuse lint` passes for all 717 files; the per-patch license overrides were
  regenerated from the SPDX identifiers in the 7.3 patch set.

Camera suspend/resume, Snapshot and Chromium were not repeated on 7.3.

## Patch 3369 and runtime 0.1-9 (2026-11-07)

- With the ISP device tree, nothing binds the BPS and IPE nodes and the ICP
  driver loads only for a camera session, so `sync_state()` never ran for the
  camera clock controller and three interconnect providers. Every NoC kept its
  boot-time vote, and a 9.6 minute deep suspend never collapsed CX, AOSS or DDR
  (qcom_stats `Count: 0`). Forcing `sync_state` proved the cause (563 s of
  567 s in CX/AOSS/DDR); patch 3369 lists those providers as
  `post-init-providers` on the three nodes.
- On `7.3.0-25.1-omarchy-isp` with 3369: no provider left unsynced after boot,
  a 150-frame stream without SMMU faults, idle suspend 596.9 s of 601 s in
  CX/AOSS/DDR, and a suspend while streaming (sleep hook stopped the session,
  148 s of 152 s; the same consumer had frames again 8 s after the lid opened).
- The same series, folded into the Surface Pro 11 `linux-omarchy` 7.3-25.2
  (full build), booted with all providers synced; preflight, a 150-frame
  stream, face unlock and a deep suspend with CX/AOSS/DDR collapse passed.
  That build writes the manifest to `/usr/lib/modules/<release>/`, which
  runtime `0.1-9` requires (units, profiles and preflight).
- The kernel recipe here was updated to the same manifest location but was not
  rebuilt with it.

## Patch 3370 and runtime 0.1-10 (2026-11-07)

- After about 6 hours of uptime every camera session failed: the ICP's 140 MiB
  HFI allocation from the reusable (CMA) camera_icp_mem region returned -EBUSY.
  The region was 80% free; page isolation failed on unmapped, unevictable shmem
  pages charged to the desktop session (traced with page_isolation events), and
  dropping caches or compacting did not help.
- 3370 makes the region a dedicated no-map pool (same 272 MiB) and carves the
  HFI memory, its IPE/BPS mirror mappings and the lab buffers from it with a
  gen_pool. First test faulted (IPE CDM read of the uncached HFI region, cb=9)
  because the mirror still looked up the physical address through the DMA
  API's mapping; fixed in the same patch.
- On the Surface Pro 11 with the fixed module: MemTotal 272 MiB lower, the
  driver reports the dedicated memory, a 150-frame stream ran without SMMU
  faults, ICP unloaded afterwards. Long-uptime behaviour is the point of the
  patch and still to be observed over a day of use.
- Runtime 0.1-10 names the front node "Built-in Front Camera (ISP)", so apps
  no longer show two cameras with the stock libcamera node's name.
