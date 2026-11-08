# Controlled camera colour experiments

The production bank uses a fixed colour temperature. Experimental CCT variants
change the CC/CV programs while preserving the accepted bank's sharpening,
denoise, gamma, tone and geometry. They are not enabled by default and have not
been qualified across lighting conditions.

## Preparation

Use the exact sensor tuning accepted by `sensor_layout.py` and its installed
bank. All generated assets belong outside this source repository.

```sh
python -I preparation/prepare-colour-sets.py \
  --tuning /absolute/path/to/sensor-tuning.bin \
  --bank /absolute/path/to/assets/bank \
  --out /absolute/path/to/new-colour-sets
```

The tool reads no hardware and executes no Windows DLL. It checks each source
`post.bin` against the bank manifest, requires the supplied tuning to reproduce
the existing colour anchor, and verifies that sharpening commands are independent
of tone within each gain row. It emits 13 CCT choices for all 25 gain rows.
The runtime `--cct-sets --colour-sets PATH` option can load this format during
an explicitly configured experiment.

CCM and chroma enhancement must each traverse their own nested lux/CCT tree.
The rear tuning has branch-specific CCT boundaries. The old first-CCM-table
shortcut selected incorrect weights in some warm-light regions. The corrected
selection preserves the current fixed-temperature colour anchor on both sensors.

```sh
python -I preparation/test-colour-selection.py
python -I preparation/test-preparation.py
```

## Paired input and output evidence

For bounded diagnostic sessions, `ipe-live.py --save-input-every N` retains
pre-IPE NV12 for captured sensor sequences divisible by N. If those frames are
published, it also saves `matched-output-seqSEQUENCE.nv12`. The existing report
records the same sensor sequence, controls and selected program. A seed or
discarded frame can have an input without a matching published output; do not
silently pair it with a neighboring frame. `--save-raw` separately retains RAW
requests 58/59, and their hardware sequence must be checked before pairing.

Diagnostic file writes add capture latency. These sessions are for image
analysis, not throughput measurements, and all retention remains disabled by
default. A still-input IPE replay cannot qualify motion-dependent noise reduction.

## Conditions for enabling adaptive colour

First establish Bayer/channel ordering, the actual response to red/blue gains,
and neutral-patch white balance. Compare colour variants on exactly the same
retained input with the same bank entry and independently seeded temporal history.
Require the unchanged colour variant and a repeated baseline to reproduce the
same output before interpreting differences.

CCM rows approximately preserve neutral RGB. A strong tint on a neutral input
therefore needs investigation of white balance, calibration, channel ordering or
other stages; selecting a different CCM alone is not a demonstrated cure.

The optional runtime currently estimates CCT from applied white-balance gains.
Warmth retention and other deliberate gain biases can move those gains away
from the neutral illuminant locus. Front-camera adaptive colour consequently
needs an independently validated CCT/illuminant input before broad activation.
Keep per-device calibration and lens-shading qualification separate from a
visually pleasing adjustment to one scene.

## Rear channel correction on 2026-11-08

Native OV13858 GRBG requires CSID CAMIF pattern 1. The former pattern 3
inverted red/blue before the correctly mapped white-balance gains. Real-scene
paired RAW/pre-IPE comparisons exposed the error; sensor bars alone had not.
Runtime 0.1-14 and the bounded rear experiment use the corrected temporary
selector. The pending kernel patch carries the same mapping and still requires
build/boot qualification. Keep the legacy front row crop unchanged.

The fixed-temperature anchor and a repeated baseline reproduced identical
output across independently seeded saved-input IPE replays. In the prepared
scene, 5263 K and 6500 K programs also reproduced the baseline exactly. Adaptive
colour remains experimental; the observed improvement came from Bayer ordering.
