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
