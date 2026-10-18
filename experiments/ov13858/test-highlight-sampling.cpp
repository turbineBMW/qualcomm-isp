/* SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com> */
/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include <cassert>
#include <cmath>
#include "camss_highlight_ae.h"

int main()
{
	using Ae = libcamera::CamssHighlightAe;
	Ae::Stats front, rear;
	rear.fullSamples = 32400;
	rear.centreSamples = 8160;
	for (auto *stats : { &front, &rear }) {
		for (unsigned int channel = 0; channel < 4; channel++) {
			stats->full[channel][128] = stats->fullSamples;
			stats->centre[channel][128] = stats->centreSamples;
		}
	}
	for (bool centre : { false, true }) {
		auto a = Ae::evaluate(front, { 1.6, 1.5 }, 90, 110, centre);
		auto b = Ae::evaluate(rear, { 1.6, 1.5 }, 90, 110, centre);
		assert(a.valid && b.valid);
		assert(a.peak == b.peak && a.scale == b.scale);
	}
	/* A missing sample or wrong declared count must still fail closed. */
	rear.full[2][128]--;
	assert(!Ae::evaluate(rear, { 1.6, 1.5 }, 90, 110, false).valid);
	rear.full[2][128]++;
	rear.centre[1][128]--;
	assert(!Ae::evaluate(rear, { 1.6, 1.5 }, 90, 110, true).valid);
}
