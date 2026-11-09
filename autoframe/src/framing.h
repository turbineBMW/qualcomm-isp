// Automatic-framing controller (shared with surface-pro-12-linux autoframe/src/framing.h; keep them in step).
// Coordinates are source pixels. The window is an output-aspect rectangle
// inside the source that the element crops and scales.
// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
#pragma once
#include <algorithm>
#include <cmath>
#include <vector>

namespace autoframe {

struct Det { float x, y, w, h, score; };

struct FaceTrack {
    float x, y, w, h, score;
    int seen, missed, id;
    float cx() const { return x + w / 2; }
    float cy() const { return y + h / 2; }
};

struct FrameParams {
    float max_zoom = 2.0f;      // window never smaller than full / max_zoom
    float face_frac = 0.30f;    // face height as a fraction of window height
    float headroom = 0.12f;     // face centre this fraction of window height above centre
    float dead_pos = 0.10f;     // re-target when the face drifts this fraction of the window
    float dead_size = 0.25f;    // ...or the wanted width differs by this ratio
    float omega = 4.0f;         // spring natural frequency, rad/s
    float vmax_frac = 1.2f;     // max pan speed, window widths per second
    int appear_frames = 5;
    int lost_frames = 15;
    float hold_time = 3.0f;
    float follow_time = 0.7f;
    bool group = true;
    int group_grow_frames = 30;
    float match_iou = 0.3f;
    // > 0: the consumer extends every window to this aspect (w/h) around its
    // centre before cropping (the ISP path), so the centre has to keep that
    // taller/wider rectangle inside the source too.
    float extend_aspect = 0.f;
};

inline float iou(float ax, float ay, float aw, float ah, float bx, float by, float bw, float bh) {
    float x1 = std::max(ax, bx), y1 = std::max(ay, by);
    float x2 = std::min(ax + aw, bx + bw), y2 = std::min(ay + ah, by + bh);
    float inter = std::max(0.f, x2 - x1) * std::max(0.f, y2 - y1);
    return inter / (aw * ah + bw * bh - inter + 1e-9f);
}

struct Window { float x, y, w, h; };

class Controller {
public:
    FrameParams p;

    Controller(int src_w, int src_h, float out_aspect) { reset(src_w, src_h, out_aspect); }
    Controller() = default;

    void reset(int src_w, int src_h, float out_aspect) {
        sw = src_w; sh = src_h; aspect = out_aspect;
        full_w = std::min<float>(sw, sh * aspect);
        min_w = full_w / p.max_zoom;
        cx = sw / 2.f; cy = sh / 2.f; w = full_w;
        vx = vy = vw = 0;
        tx = cx; ty = cy; tw = w;
        tracks.clear(); next_id = 1; lost_for = 0; follow_for = 0;
    }
    void set_enabled(bool e) { enabled = e; }
    bool is_enabled() const { return enabled; }
    const std::vector<FaceTrack>& faces() const { return tracks; }
    Window current() const { return {cx - w / 2, cy - w / aspect / 2, w, w / aspect}; }

    Window step(const std::vector<Det>& dets, float dt) {
        min_w = full_w / p.max_zoom;
        update_tracks(dets);
        std::vector<const FaceTrack*> faces;
        for (auto& t : tracks) if (t.seen >= p.appear_frames) faces.push_back(&t);
        if (p.group && faces.size() > 1) {
            std::vector<const FaceTrack*> old;
            for (auto* t : faces) if (t->seen >= p.group_grow_frames) old.push_back(t);
            if (old.empty())
                old.push_back(*std::max_element(faces.begin(), faces.end(),
                    [](auto* a, auto* b) { return a->seen < b->seen; }));
            faces = old;
        }
        if (!enabled) {
            tx = sw / 2.f; ty = sh / 2.f; tw = full_w;
        } else if (!faces.empty()) {
            lost_for = 0;
            float wcx, wcy, ww;
            wanted(faces, wcx, wcy, ww);
            limit(wcx, wcy, ww);
            if (std::fabs(wcx - tx) > p.dead_pos * tw ||
                std::fabs(wcy - ty) > p.dead_pos * tw / aspect ||
                std::fabs(ww / tw - 1) > p.dead_size)
                follow_for = p.follow_time;
            if (follow_for > 0) {
                follow_for -= dt;
                tx = wcx; ty = wcy; tw = ww;
            }
        } else {
            lost_for += dt;
            if (lost_for >= p.hold_time) { tx = sw / 2.f; ty = sh / 2.f; tw = full_w; }
        }
        float k = p.omega * p.omega, c = 2 * p.omega, vmax = p.vmax_frac * w;
        spring(cx, tx, vx, k, c, vmax, dt);
        spring(cy, ty, vy, k, c, vmax, dt);
        spring(w, tw, vw, k, c, vmax, dt);
        limit(cx, cy, w);
        return current();
    }

private:
    int sw = 1, sh = 1;
    float aspect = 16.f / 9, full_w = 1, min_w = 1;
    float cx = 0, cy = 0, w = 1, vx = 0, vy = 0, vw = 0, tx = 0, ty = 0, tw = 1;
    std::vector<FaceTrack> tracks;
    int next_id = 1;
    float lost_for = 0, follow_for = 0;
    bool enabled = true;

    static void spring(float& pos, float tgt, float& v, float k, float c, float vmax, float dt) {
        v += (k * (tgt - pos) - c * v) * dt;
        v = std::max(-vmax, std::min(vmax, v));
        pos += v * dt;
    }

    void update_tracks(const std::vector<Det>& dets) {
        std::vector<Det> un(dets);
        for (auto& t : tracks) {
            float best = 0; int bi = -1;
            for (size_t i = 0; i < un.size(); i++) {
                float v = iou(t.x, t.y, t.w, t.h, un[i].x, un[i].y, un[i].w, un[i].h);
                if (v > best) { best = v; bi = (int)i; }
            }
            if (bi >= 0 && best >= p.match_iou) {
                const Det& d = un[bi];
                t.x = d.x; t.y = d.y; t.w = d.w; t.h = d.h; t.score = d.score;
                t.seen++; t.missed = 0;
                un.erase(un.begin() + bi);
            } else {
                t.missed++;
            }
        }
        for (auto& d : un) tracks.push_back({d.x, d.y, d.w, d.h, d.score, 1, 0, next_id++});
        tracks.erase(std::remove_if(tracks.begin(), tracks.end(),
            [&](const FaceTrack& t) { return t.missed >= p.lost_frames; }), tracks.end());
    }

    void wanted(const std::vector<const FaceTrack*>& faces, float& ocx, float& ocy, float& ow) const {
        if (faces.size() == 1 || !p.group) {
            const FaceTrack* f = *std::max_element(faces.begin(), faces.end(),
                [](auto* a, auto* b) { return a->w * a->h < b->w * b->h; });
            float ww = std::max(f->h / p.face_frac * aspect, min_w);
            float hh = ww / aspect;
            ocx = f->cx(); ocy = f->cy() + p.headroom * hh; ow = ww;
            return;
        }
        float x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
        for (auto* t : faces) {
            x0 = std::min(x0, t->x - t->h); x1 = std::max(x1, t->x + t->w + t->h);
            y0 = std::min(y0, t->y - 1.3f * t->h); y1 = std::max(y1, t->y + t->h + 2.0f * t->h);
        }
        ow = std::max({x1 - x0, (y1 - y0) * aspect, min_w});
        ocx = (x0 + x1) / 2; ocy = (y0 + y1) / 2;
    }

    void limit(float& lcx, float& lcy, float& lw) const {
        lw = std::min(std::max(lw, min_w), full_w);
        float lh = lw / aspect;
        float ew = lw, eh = lh;
        if (p.extend_aspect > 0) {
            if (lw / lh > p.extend_aspect) eh = lw / p.extend_aspect; else ew = lh * p.extend_aspect;
        }
        ew = std::min(ew, (float)sw); eh = std::min(eh, (float)sh);
        lcx = std::min(std::max(lcx, ew / 2), sw - ew / 2);
        lcy = std::min(std::max(lcy, eh / 2), sh - eh / 2);
    }
};

}  // namespace autoframe
