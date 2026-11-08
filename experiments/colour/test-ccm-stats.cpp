/* SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com> */
/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include <cassert>
#include <cmath>
#include <limits>
#include "camss_highlight_ae.h"

using Model = libcamera::CamssCcmStats;
using Ae = libcamera::CamssHighlightAe;

int main()
{
	const std::array<int32_t, 12> identity = { 1024, 0, 0, 0, 1024, 0, 0, 0, 1024, 0, 0, 0 };
	/* Synthetic neutral-preserving matrix; no owner tuning in this test. */
	const std::array<int32_t, 12> matrix = { 2560, -1024, -512, -512, 2048, -512, 0, -1024, 2048, 0, 0, 0 };
	Model model;
	assert(model.configure(identity));
	Model::Samples samples = { { { 264, 264, 264, 264 }, true } };
	auto neutral = model.evaluate(samples, 1, { 1, 1 }, 0.995, false);
	assert(neutral.valid && neutral.value == 200.0 / 1024 && neutral.saturated == 0);
	assert(model.configure(matrix));
	auto corrected = model.evaluate(samples, 1, { 1, 1 }, 0.995, false);
	assert(corrected.valid && corrected.value == neutral.value);
	/* A bright red sample can clip after CCM while RAW/WB is below ceiling. */
	samples[0].value = { 664, 264, 264, 264 };
	auto red = model.evaluate(samples, 1, { 1, 1 }, 0.995, false);
	assert(red.valid && red.value == 1200.0 / 1024 && red.saturated == 1);
	Ae::Stats stats;
	stats.fullSamples = stats.centreSamples = 1;
	stats.colourSamples = samples;
	for (unsigned int c = 0; c < 4; c++)
		stats.full[c][samples[0].value[c]] = stats.centre[c][samples[0].value[c]] = 1;
	auto original = Ae::evaluate(stats, { 1, 1 }, 70, 110, false, 0.95);
	auto guarded = Ae::evaluate(stats, { 1, 1 }, 70, 110, false, 0.95, 0.01, 0.001, 0.995, &model);
	assert(original.valid && guarded.valid && original.scale > 1 && guarded.scale < 1);
	assert(guarded.peak == red.value && guarded.saturated == 1);
	/* Centre protection must not silently use the whole-frame distribution. */
	samples.push_back({ { 164, 164, 164, 164 }, false });
	assert(model.evaluate(samples, 1, { 1, 1 }, 0.995, true).value == red.value);
	assert(!model.evaluate(samples, 2, { 1, 1 }, 0.995, true).valid);
	assert(!model.evaluate(samples, 1, { 1, 1 }, 0.995, false).valid);
	stats.colourSamples.clear();
	assert(!Ae::evaluate(stats, { 1, 1 }, 70, 110, false, 0.95, 0.01, 0.001, 0.995, &model).valid);
	/* Native RAW saturation remains protective even when CCM reduces a channel. */
	stats.colourSamples = { { { 264, 264, 264, 264 }, true } };
	for (unsigned int c = 0; c < 4; c++) {
		stats.full[c].fill(0);
		stats.centre[c].fill(0);
		stats.full[c][1023] = stats.centre[c][1023] = 1;
	}
	assert(Ae::evaluate(stats, { 1, 1 }, 70, 110, true, 0.95, 0.01, 0.001, 0.995, &model).scale < 1);
	/* Hardware reduces the entire matrix to Q9 by truncating signed values. */
	auto quantized = matrix;
	quantized[0]++; quantized[1]--;
	assert(model.configure(quantized));
	assert(model.evaluate({ { { 664, 264, 264, 264 }, true } }, 1, { 1, 1 }, 0.995, false).value == red.value);
	/* Reject offsets, non-neutral rows, invalid codes, controls and metadata. */
	auto bad = matrix; bad[9] = 1;
	assert(!model.configure(bad) && !model.enabled());
	bad = matrix; bad[0] += 100;
	assert(!model.configure(bad));
	bad = matrix; bad[0] = std::numeric_limits<int32_t>::min();
	assert(!model.configure(bad));
	assert(model.configure(identity));
	assert(!model.evaluate(samples, 2, { NAN, 1 }, 0.995, false).valid);
	assert(!model.evaluate(samples, 2, { 1, 1 }, NAN, false).valid);
	samples[0].value[0] = 1024;
	assert(!model.evaluate(samples, 2, { 1, 1 }, 0.995, false).valid);
	/* Peak and clipping controllers must target the same population. A
	 * 0.3% bright tail is outside p99.5 but exceeds a 0.1% clip budget. */
	Ae::Histogram tail{};
	for (auto &channel : tail) {
		channel[400] = 9970;
		channel[1000] = 30;
	}
	auto protectedTail = Ae::evaluate(tail, { 1, 1 }, 60, 110, 10000, 0.995, 0.001, 0.80);
	assert(protectedTail.valid && protectedTail.peak == 936.0 / 959 && protectedTail.scale < 1);
	/* A simple constant scene converges instead of alternating between
	 * brightening the body and cutting the clipped tail. */
	double exposure = 0.6;
	for (unsigned int frame = 0; frame < 60; frame++) {
		Ae::Histogram scene{};
		for (auto &channel : scene) {
			channel[std::min(1023, int(std::lround(64 + 336 * exposure)))] += 9970;
			channel[std::min(1023, int(std::lround(64 + 936 * exposure)))] += 30;
		}
		auto result = Ae::evaluate(scene, { 1, 1 }, 60 * std::pow(exposure, 1 / 1.32), 110,
			10000, 0.995, 0.001, 0.95);
		assert(result.valid);
		exposure *= result.scale;
		if (frame > 20)
			assert(std::abs(result.scale - 1) < 0.001);
	}
}
