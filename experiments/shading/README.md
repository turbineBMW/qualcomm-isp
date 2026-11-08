# Mild neutral rear lens shading

Private libcamera 0.7.2-6 supports an explicitly enabled, neutral rear mesh
with matching RAW exposure and white-balance statistics. It is qualified only
for OV13858 at the existing 4224×3136 sensor / 3840×2160 output geometry.
It does not enable shading by default or ship measured per-device calibration.

The original guard rejected all hardware shading while RAW AE/AWB was active.
The new opt-in `raw_lsc_model: true` requires identical R/Gr/Gb/B meshes, linear
hardware gain response, 221 points per channel, and programmed Q10 gains in
1024–1280 (1–1.25). Other meshes still fail configuration. Front-camera behavior
retains its original shading restriction.

The model maps output sample coordinates back to the native sensor and
interpolates the programmed mesh before capture starts. Algorithm histograms
and AWB tiles account for the gain above black level 64 and clamp at RAW10
saturation. Saved RAW, diagnostic RAW histograms and diagnostic coarse tiles
remain unmodified, preserving independent verification against sensor samples.
The kernel's resampling/rounding can differ slightly from this control model;
keep the gain range narrow. Per-channel color shading and nonlinear response
require separate qualification.

## Hardware validation on 2026-11-08

A textured wall under reduced ring lighting was a diagnostic target. Its
illumination was not treated as a calibrated flat field. A conservative smooth
mesh was chosen, rather than fitting the wall's texture or hotspot:

`gain = 1 + 0.1 * (x² + y²)`, with x/y spanning −1…1 across the native sensor.

All four channels share it. Mesh gain is 1 at center and at most 1.2 at full
sensor corners; the video crop uses less than the full vertical range.
`lsc_strength: 1` applies this already mild mesh, not a full OEM correction.

- Fixed exposure/gain/WB, shading off vs unity: sampled linearized pre-IPE
  channel values agreed within approximately 0.2%.
- Off vs mild mesh at the same fixed controls: sampled outer areas gained
  approximately 7–10%, center approximately 0.5–0.7%. These values use inverse
  configured gamma and are approximate, not radiometric calibration.
- Automatic RAW AE/AWB capture completed with normal control convergence and
  clean cleanup. Native histograms still matched both saved RAW frames exactly.
- The standalone model test covers disabled/unity behavior at every sampled
  coordinate, pedestal preservation, saturation, native-coordinate mapping,
  unsupported channel differences and gain limits. It runs in package check().
- Installed desktop front/rear/front and cancellation during rear startup
  passed. No sensor calibration, firmware or IPE sharpening/denoise bank changed.

Private captures, generated YAML and installation backups remain outside this
repository. The generated profile is a local aesthetic correction, not a
factory calibration or a claim of flat-field uniformity.

## Local deployment and rollback

Keep a local copy of the rear profile and point its `ife_tuning` at the mild
YAML; preserve all other profile keys. Select that profile through the existing
`/etc/qualcomm-isp/rear.json`. This avoids modifying imported bank manifests or
package-owned profiles. Install libcamera 0.7.2-6 first; older builds correctly
reject RAW algorithms combined with active shading.

On the qualification machine the original configuration is backed up at
`/var/lib/qualcomm-isp/backups/rear-before-neutral-lsc-20261108/rear.json`.
Close camera clients and restore that file over `/etc/qualcomm-isp/rear.json`
to disable the correction on the next session. The original bank was untouched,
and a libcamera downgrade is not required to disable the mesh.
