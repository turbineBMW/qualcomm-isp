# Early rear color correction and exposure convergence

The rear camera retains less purple shadow tint when the sensor color matrix
runs before IFE gamma, with the late IPE matrix set to unity. The P010 interior
continues to preserve denoised chroma precision. This opt-in configuration uses
the existing fixed-temperature matrix; it does not add illuminant-dependent
matrix selection or fit colors to one phone photograph.

## Highlight protection

`early-ccm.patch` adds a rear-only RAW highlight model. Joint shaded Bayer quads
preserve the relationship between channel values, which independent histograms
cannot reconstruct. The model applies recorded frame WB, estimates RGB using
the two greens' average, and applies the hardware-quantized color matrix. Its
highlight envelope supplements the existing sensor/WB ceiling. This is a sparse
pre-gamma estimate, not a reconstruction of the hardware demosaic.

The model requires black64, paired native rear RAW, the existing qualified
shading constraints, a neutral-preserving matrix and zero offsets. Non-identity
CCM with RAW AE now requires explicit `raw_ccm_model: true`; unsupported front
or offset configurations are rejected. Diagnostic RAW histograms and AWB
statistics remain in their original domains. Missing joint samples hold AE.

The patch also resolves conflicting exposure constraints. The percentile used
for the peak target must cover at least the population permitted by the clipping
budget. A p99.5 target and 0.1% clipping budget previously alternated between
brightening and cutting exposure. A separate sensor-headroom constraint uses
1023 minus black64; a 0.95 target in post-black /1024 units alone was unreachable
for unity-gain green. WB callbacks run before the additional color analysis so
that its CPU time cannot age an otherwise fresh WB update. AE continues to use
the recorded WB applied to that frame.

## Qualification

On the prepared daylight scene with four colored/neutral objects, the old
controller's final 60 frames varied from about 31,073 to 45,537 exposure-time ×
analogue-gain units. A new early-color trial held 36,430 throughout its final
60 frames; a subsequent complete-bank trial varied from 38,511 to 40,593 as
conditions changed. White balance converged and the display-tone controller
reached its 8× setting instead of repeatedly holding around 1.5–2×. The gray
object's purple cast was reduced. These are successive live exposures, not
identical RAW frames or a calibrated color-error measurement.

Fixed exposure/WB trials separately compared early and late correction. The
full 625-entry generated bank matched the live trial's post commands exactly;
every other bank file was unchanged. Both saved RAW frames' native histograms
matched the diagnostic logs exactly. Front capture completed successfully.

`test-ccm-stats.cpp` checks neutral preservation, matrix-induced red clipping,
retained native saturation protection, independent centre counts, missing data,
hardware coefficient quantization, unsupported offsets and matrices, invalid
controls, clipping-budget coverage and closed-loop convergence. Package checks
also run the existing neutral-LSC and front/rear sampling tests. The pinned
archive plus incremental patches reconstructs all 18 recorded source hashes.

This qualification covers the prepared daylight scene and earlier room trials.
Warm light, mixed illumination, moving subjects, fine-edge demosaic behavior,
and a new suspend cycle remain separate checks. Stronger sharpening was not
part of this change.

## Preparation and local selection

Build/install private libcamera 0.7.2-7 before enabling this profile; runtime
0.1-15 provides the P010 interior. Run `preparation/prepare-early-colour.py`
unprivileged with `--assets` pointing at the qualified rear P010 asset directory,
`--tuning` at the pinned OV13858 tuning file, `--ife` at the existing mild neutral
shading YAML, and `--out` at a new private directory outside the source tree.

The generator validates the source bank and its sensor/color anchor, writes
the matrix and RAW model flag into `ife.yaml`, and replaces only the late CCM
commands with unity. Chroma conversion, sharpening, denoising, tone, temporal
filtering and geometry programs remain intact. Its manifest records provenance
and the generated IFE hash. Keep the generated YAML and bank together; select
them through a separate rear board profile and backed-up configuration. Owner
assets remain local and are not redistributable source.

On the qualification machine the new profile is
`/etc/qualcomm-isp/rear-early-colour-profile.json`. With camera clients closed,
restore `/var/lib/qualcomm-isp/backups/rear-before-early-colour-20261108/rear.json`
over `/etc/qualcomm-isp/rear.json` to return to the previous color placement and
P010 bank. Libcamera 0.7.2-7 can remain installed, retaining the exposure fix.
To reproduce the exact older exposure behavior, also restore the previous
0.7.2-6 package after restoring the configuration.
