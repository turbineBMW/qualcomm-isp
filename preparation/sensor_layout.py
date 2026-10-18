# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Exact tuning identities and layout differences for the two Denali sensors."""
import hashlib

LAYOUTS = {
    'imx681': dict(sha256='2c1c7fd9090e0bf338f44bd9de785509c1fbebc975facc5286f12865cf675f1d',
                  filename='com.surface.tuned.ffc_imx681.bin', tf_selector=157, dsx_occurrence=2,
                  tmc_shape=(2, 1, 3), ltm_shape=(2, 1, 4), gamma_rows=2, cc_rows=1, cv_rows=3),
    'ov13858': dict(sha256='4858ccb297eeecbc8e9b6d673f7ab4b0ead559adf16e3fe717eea9e40ccef635',
                   filename='com.surface.tuned.rfc_ov13858.bin', tf_selector=4, dsx_occurrence=1,
                   tmc_shape=(1, 1, 1), ltm_shape=(2, 1, 2), gamma_rows=3, cc_rows=2, cv_rows=2),
}


def identify(data):
    digest = hashlib.sha256(data).hexdigest()
    for sensor, layout in LAYOUTS.items():
        if digest == layout['sha256']:
            return sensor, layout
    raise ValueError(f'Unsupported sensor tuning SHA-256: {digest}')
