/* SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com> */
/* SPDX-License-Identifier: MIT */
/* Native-4K box reduction and measured PD10 packing for the live lab stream.
 * Same round-to-even 10-bit samples as pd10.py; f is 4 or 16.
 *
 * pd10_reduce_planes() reads the luma and chroma planes separately, so a
 * capture buffer can be reduced in place without first assembling a padded
 * copy. Rows at or beyond h_valid repeat the last valid row, which is exactly
 * the 2160 -> 2176 edge padding the IPE input receives. One pass computes
 * 4x4 block sums; DS4 and DS16 are both derived from those sums, so DS16 is
 * bit-identical to the former separate 16x16 reduction (sum of 256 samples,
 * denominator 64) while reading the source once instead of twice.
 */
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define MAX_THREADS 8

static unsigned align(unsigned value, unsigned n) { return (value + n - 1) / n * n; }
static uint16_t average(unsigned sum, unsigned denominator)
{
    unsigned q = sum / denominator, r = sum % denominator;
    return q + (r * 2 > denominator || (r * 2 == denominator && (q & 1)));
}

struct sums {
    const uint8_t *y, *uv;
    unsigned w, h_valid, bw, bh;    /* bw x bh luma blocks of 4x4 */
    uint16_t *ysum;                 /* [bh][bw] */
    uint16_t *usum, *vsum;          /* [bh/2][bw/2] */
    unsigned first, last;           /* luma block rows [first, last) for one thread */
    int failed;
};

static void block_rows(struct sums *s)
{
    unsigned w = s->w;
    uint16_t *col = malloc(w * sizeof(*col));
    if (!col) { s->failed = 1; return; }
    for (unsigned r = s->first; r < s->last; r++) {
        memset(col, 0, w * sizeof(*col));
        for (unsigned j = 0; j < 4; j++) {
            unsigned row = r * 4 + j;
            if (row >= s->h_valid) row = s->h_valid - 1;
            const uint8_t *p = s->y + (size_t)row * w;
            for (unsigned x = 0; x < w; x++) col[x] += p[x];
        }
        uint16_t *out = s->ysum + (size_t)r * s->bw;
        for (unsigned c = 0; c < s->bw; c++)
            out[c] = col[4*c] + col[4*c+1] + col[4*c+2] + col[4*c+3];
    }
    /* Chroma block rows cover eight luma rows each: rows first/2 .. last/2. */
    unsigned ch_valid = s->h_valid / 2;
    for (unsigned r = s->first / 2; r < s->last / 2; r++) {
        memset(col, 0, w * sizeof(*col));
        for (unsigned j = 0; j < 4; j++) {
            unsigned row = r * 4 + j;
            if (row >= ch_valid) row = ch_valid - 1;
            const uint8_t *p = s->uv + (size_t)row * w;
            for (unsigned x = 0; x < w; x++) col[x] += p[x];
        }
        uint16_t *u = s->usum + (size_t)r * (s->bw / 2), *v = s->vsum + (size_t)r * (s->bw / 2);
        for (unsigned c = 0; c < s->bw / 2; c++) {
            u[c] = col[8*c] + col[8*c+2] + col[8*c+4] + col[8*c+6];
            v[c] = col[8*c+1] + col[8*c+3] + col[8*c+5] + col[8*c+7];
        }
    }
    free(col);
}

static void *worker(void *arg)
{
    block_rows(arg);
    return NULL;
}

static void pack(uint8_t *out, unsigned stride, unsigned row, unsigned col, const uint16_t values[6])
{
    uint64_t word = 0;
    for (unsigned k = 0; k < 6; k++) {
        word |= (uint64_t)(values[k] >> 2) << (k*8);
        word |= (uint64_t)(values[k] & 3) << (48+k*2);
    }
    memcpy(out + (size_t)row*stride + col*8, &word, 8);
}

static int threads(void)
{
    const char *env = getenv("PD10_THREADS");
    int n = env ? atoi(env) : 4;
    return n < 1 ? 1 : n > MAX_THREADS ? MAX_THREADS : n;
}

int pd10_reduce_planes(const uint8_t *y, const uint8_t *uv, unsigned w, unsigned h_valid,
                       unsigned h_total, uint8_t *out4, size_t out4_size,
                       uint8_t *out16, size_t out16_size)
{
    if (!y || !uv || w != 3840 || h_total != 2176 || h_valid < 2 || h_valid > h_total ||
        (h_valid & 1) || (!out4 && !out16))
        return -1;
    unsigned bw = w / 4, bh = h_total / 4;                 /* 960 x 544 */
    unsigned stride4 = align(align(bw / 2 * 8, 192), 256), slice4 = align(bh / 2, 32);
    unsigned bw16 = w / 16, bh16 = h_total / 16;            /* 240 x 136 */
    unsigned stride16 = align(align(bw16 / 2 * 8, 192), 256), slice16 = align(bh16 / 2, 32);
    if ((out4 && out4_size != (size_t)stride4 * slice4) ||
        (out16 && out16_size != (size_t)stride16 * slice16))
        return -1;
    uint16_t *ysum = malloc((size_t)bw * bh * sizeof(*ysum));
    uint16_t *usum = malloc((size_t)bw * bh / 4 * sizeof(*usum));
    uint16_t *vsum = malloc((size_t)bw * bh / 4 * sizeof(*vsum));
    if (!ysum || !usum || !vsum) { free(ysum); free(usum); free(vsum); return -2; }

    int n = threads(), started = 0, failed = 0;
    struct sums parts[MAX_THREADS];
    pthread_t ids[MAX_THREADS];
    /* Split on even block rows so each chroma block row has exactly one owner. */
    unsigned per = align((bh + n - 1) / n, 2), first = 0;
    for (int i = 0; i < n && first < bh; i++) {
        unsigned last = first + per > bh ? bh : first + per;
        parts[i] = (struct sums){ y, uv, w, h_valid, bw, bh, ysum, usum, vsum, first, last, 0 };
        if (last == bh || pthread_create(&ids[started], NULL, worker, &parts[i]))
            block_rows(&parts[i]);
        else
            started++;
        failed |= parts[i].failed;
        first = last;
    }
    for (int i = 0; i < started; i++)
        pthread_join(ids[i], NULL);
    for (int i = 0; i < n; i++)
        if (i < started + 1) failed |= parts[i].failed;
    if (failed) { free(ysum); free(usum); free(vsum); return -2; }

    if (out4) {
        memset(out4, 0, out4_size);
        for (unsigned row = 0; row < bh / 2; row++) {
            for (unsigned col = 0; col < bw / 2; col++) {
                uint16_t values[6] = {
                    average(ysum[(2*row)*bw + 2*col], 4), average(ysum[(2*row+1)*bw + 2*col], 4),
                    average(ysum[(2*row)*bw + 2*col+1], 4), average(ysum[(2*row+1)*bw + 2*col+1], 4),
                    average(usum[row*(bw/2) + col], 4), average(vsum[row*(bw/2) + col], 4) };
                pack(out4, stride4, row, col, values);
            }
        }
    }
    if (out16) {
        uint16_t *y16 = malloc((size_t)bw16 * bh16 * sizeof(*y16));
        uint16_t *u16 = malloc((size_t)bw16 * bh16 / 4 * sizeof(*u16));
        uint16_t *v16 = malloc((size_t)bw16 * bh16 / 4 * sizeof(*v16));
        if (!y16 || !u16 || !v16) {
            free(y16); free(u16); free(v16); free(ysum); free(usum); free(vsum);
            return -2;
        }
        for (unsigned R = 0; R < bh16; R++)
            for (unsigned C = 0; C < bw16; C++) {
                unsigned sum = 0;
                for (unsigned j = 0; j < 4; j++)
                    for (unsigned k = 0; k < 4; k++)
                        sum += ysum[(4*R+j)*bw + 4*C+k];
                y16[R*bw16 + C] = average(sum, 64);
            }
        for (unsigned R = 0; R < bh16 / 2; R++)
            for (unsigned C = 0; C < bw16 / 2; C++) {
                unsigned su = 0, sv = 0;
                for (unsigned j = 0; j < 4; j++)
                    for (unsigned k = 0; k < 4; k++) {
                        su += usum[(4*R+j)*(bw/2) + 4*C+k];
                        sv += vsum[(4*R+j)*(bw/2) + 4*C+k];
                    }
                u16[R*(bw16/2) + C] = average(su, 64);
                v16[R*(bw16/2) + C] = average(sv, 64);
            }
        memset(out16, 0, out16_size);
        for (unsigned row = 0; row < bh16 / 2; row++)
            for (unsigned col = 0; col < bw16 / 2; col++) {
                uint16_t values[6] = {
                    y16[(2*row)*bw16 + 2*col], y16[(2*row+1)*bw16 + 2*col],
                    y16[(2*row)*bw16 + 2*col+1], y16[(2*row+1)*bw16 + 2*col+1],
                    u16[row*(bw16/2) + col], v16[row*(bw16/2) + col] };
                pack(out16, stride16, row, col, values);
            }
        free(y16); free(u16); free(v16);
    }
    free(ysum); free(usum); free(vsum);
    return 0;
}

/* Previous interface: one padded 3840x2176 NV12 frame, one factor. */
int pd10_reduce(const uint8_t *src, size_t src_size, unsigned w, unsigned h,
                unsigned factor, uint8_t *out, size_t out_size)
{
    if (!src || !out || w != 3840 || h != 2176 || (factor != 4 && factor != 16) ||
        src_size != (size_t)w*h*3/2) return -1;
    if (factor == 4)
        return pd10_reduce_planes(src, src + (size_t)w*h, w, h, h, out, out_size, NULL, 0);
    return pd10_reduce_planes(src, src + (size_t)w*h, w, h, h, NULL, 0, out, out_size);
}
