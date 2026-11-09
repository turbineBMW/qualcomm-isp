// ispautoframe: automatic framing (face-following crop) for the Qualcomm ISP
// camera publisher. NV12 in at any size, NV12 out at the negotiated size; a
// YuNet face detector (OpenCV DNN on the CPU) finds the face(s), the
// controller in framing.h decides the window, OpenCV crops and scales.
//
// Pipeline (runtime/ipe-pipewire.py, through a publisher.d drop-in):
//   appsrc NV12 3840x2160 ! queue ! ispautoframe ! capsfilter caps=video/x-raw,width=1920,height=1080 ! videoconvertscale ! pipewiresink
//
// Ported from surface-pro-12-linux (sp12autoframe): same controller and
// behaviour, the detector moved from OpenVINO to OpenCV, and the Intel
// HAL-specific parts (ISP-side zoom, face exposure regions) left out.
// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
#include <gst/gst.h>
#include <gst/video/gstvideofilter.h>
#include <gst/video/video.h>
#include <opencv2/core/utility.hpp>
#include <opencv2/imgproc.hpp>
#include <sys/stat.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "framing.h"
#include "yunet.h"

GST_DEBUG_CATEGORY_STATIC(gst_isp_autoframe_debug);
#define GST_CAT_DEFAULT gst_isp_autoframe_debug

#define DEFAULT_MODEL "/usr/share/qualcomm-isp/face_detection_yunet_2023mar.onnx"

struct State {
    std::unique_ptr<autoframe::YuNet> det;
    autoframe::Controller ctl;
    std::vector<autoframe::Det> dets;  // last detections, input pixels
    cv::Mat ysmall, uvsmall, nv12small, bgr;
    guint64 frame_no = 0;
    GstClockTime last_pts = GST_CLOCK_TIME_NONE;
    struct timespec ctl_mtime {0, 0};
    off_t ctl_size = -1;
    double t_det = 0, t_crop = 0;
    int n_timed = 0;
    int in_w = 0, in_h = 0, out_w = 0, out_h = 0;
};

typedef struct _GstIspAutoFrame {
    GstVideoFilter parent;
    /* properties */
    gboolean enable;
    gchar *model;
    gint det_width, det_height;
    gint threads;
    gdouble max_zoom, face_frac, headroom, speed, hold_time;
    gboolean group;
    gint detect_interval;
    gchar *control_file;
    gboolean draw_boxes;
    /* state */
    State *st;
} GstIspAutoFrame;

typedef struct _GstIspAutoFrameClass {
    GstVideoFilterClass parent_class;
} GstIspAutoFrameClass;

enum {
    PROP_0,
    PROP_ENABLE,
    PROP_MODEL,
    PROP_DET_WIDTH,
    PROP_DET_HEIGHT,
    PROP_THREADS,
    PROP_MAX_ZOOM,
    PROP_FACE_FRAC,
    PROP_HEADROOM,
    PROP_SPEED,
    PROP_HOLD_TIME,
    PROP_GROUP,
    PROP_DETECT_INTERVAL,
    PROP_CONTROL_FILE,
    PROP_DRAW_BOXES,
};

#define CAPS_STR GST_VIDEO_CAPS_MAKE("NV12")
static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS(CAPS_STR));
static GstStaticPadTemplate src_template =
    GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS(CAPS_STR));

#define GST_TYPE_ISP_AUTOFRAME (gst_isp_autoframe_get_type())
G_DEFINE_TYPE(GstIspAutoFrame, gst_isp_autoframe, GST_TYPE_VIDEO_FILTER);
#define GST_ISP_AUTOFRAME(obj) (G_TYPE_CHECK_INSTANCE_CAST((obj), GST_TYPE_ISP_AUTOFRAME, GstIspAutoFrame))

static void apply_params(GstIspAutoFrame *self) {
    autoframe::FrameParams &p = self->st->ctl.p;
    p.max_zoom = self->max_zoom;
    p.face_frac = self->face_frac;
    p.headroom = self->headroom;
    p.omega = self->speed;
    p.hold_time = self->hold_time;
    p.group = self->group;
    self->st->ctl.set_enabled(self->enable);
}

static void gst_isp_autoframe_set_property(GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(object);
    GST_OBJECT_LOCK(self);
    switch (prop_id) {
    case PROP_ENABLE: self->enable = g_value_get_boolean(value); break;
    case PROP_MODEL: g_free(self->model); self->model = g_value_dup_string(value); break;
    case PROP_DET_WIDTH: self->det_width = g_value_get_int(value); break;
    case PROP_DET_HEIGHT: self->det_height = g_value_get_int(value); break;
    case PROP_THREADS: self->threads = g_value_get_int(value); break;
    case PROP_MAX_ZOOM: self->max_zoom = g_value_get_double(value); break;
    case PROP_FACE_FRAC: self->face_frac = g_value_get_double(value); break;
    case PROP_HEADROOM: self->headroom = g_value_get_double(value); break;
    case PROP_SPEED: self->speed = g_value_get_double(value); break;
    case PROP_HOLD_TIME: self->hold_time = g_value_get_double(value); break;
    case PROP_GROUP: self->group = g_value_get_boolean(value); break;
    case PROP_DETECT_INTERVAL: self->detect_interval = g_value_get_int(value); break;
    case PROP_CONTROL_FILE:
        g_free(self->control_file);
        self->control_file = g_value_dup_string(value);
        self->st->ctl_mtime = {0, 0};
        self->st->ctl_size = -1;
        break;
    case PROP_DRAW_BOXES: self->draw_boxes = g_value_get_boolean(value); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec); break;
    }
    apply_params(self);
    GST_OBJECT_UNLOCK(self);
}

static void gst_isp_autoframe_get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(object);
    GST_OBJECT_LOCK(self);
    switch (prop_id) {
    case PROP_ENABLE: g_value_set_boolean(value, self->enable); break;
    case PROP_MODEL: g_value_set_string(value, self->model); break;
    case PROP_DET_WIDTH: g_value_set_int(value, self->det_width); break;
    case PROP_DET_HEIGHT: g_value_set_int(value, self->det_height); break;
    case PROP_THREADS: g_value_set_int(value, self->threads); break;
    case PROP_MAX_ZOOM: g_value_set_double(value, self->max_zoom); break;
    case PROP_FACE_FRAC: g_value_set_double(value, self->face_frac); break;
    case PROP_HEADROOM: g_value_set_double(value, self->headroom); break;
    case PROP_SPEED: g_value_set_double(value, self->speed); break;
    case PROP_HOLD_TIME: g_value_set_double(value, self->hold_time); break;
    case PROP_GROUP: g_value_set_boolean(value, self->group); break;
    case PROP_DETECT_INTERVAL: g_value_set_int(value, self->detect_interval); break;
    case PROP_CONTROL_FILE: g_value_set_string(value, self->control_file); break;
    case PROP_DRAW_BOXES: g_value_set_boolean(value, self->draw_boxes); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec); break;
    }
    GST_OBJECT_UNLOCK(self);
}

static void gst_isp_autoframe_finalize(GObject *object) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(object);
    delete self->st;
    g_free(self->model);
    g_free(self->control_file);
    G_OBJECT_CLASS(gst_isp_autoframe_parent_class)->finalize(object);
}

/* Any input size maps to any output size: strip width/height. */
static GstCaps *gst_isp_autoframe_transform_caps(GstBaseTransform *trans, GstPadDirection direction, GstCaps *caps,
                                                 GstCaps *filter) {
    GstCaps *ret = gst_caps_copy(caps);
    for (guint i = 0; i < gst_caps_get_size(ret); i++) {
        GstStructure *s = gst_caps_get_structure(ret, i);
        gst_structure_set(s, "width", GST_TYPE_INT_RANGE, 2, G_MAXINT, "height", GST_TYPE_INT_RANGE, 2, G_MAXINT, NULL);
        gst_structure_remove_fields(s, "pixel-aspect-ratio", NULL);
    }
    if (filter) {
        GstCaps *tmp = gst_caps_intersect_full(filter, ret, GST_CAPS_INTERSECT_FIRST);
        gst_caps_unref(ret);
        ret = tmp;
    }
    GST_DEBUG_OBJECT(trans, "transform_caps %s: %" GST_PTR_FORMAT " -> %" GST_PTR_FORMAT,
                     direction == GST_PAD_SINK ? "sink" : "src", caps, ret);
    return ret;
}

/* Prefer the input size when the other side leaves the size open. */
static GstCaps *gst_isp_autoframe_fixate_caps(GstBaseTransform *trans, GstPadDirection direction, GstCaps *caps,
                                              GstCaps *othercaps) {
    (void)trans;
    (void)direction;
    GstStructure *in = gst_caps_get_structure(caps, 0);
    gint w = 0, h = 0;
    gst_structure_get_int(in, "width", &w);
    gst_structure_get_int(in, "height", &h);
    othercaps = gst_caps_make_writable(othercaps);
    othercaps = gst_caps_truncate(othercaps);
    GstStructure *out = gst_caps_get_structure(othercaps, 0);
    if (w > 0) gst_structure_fixate_field_nearest_int(out, "width", w);
    if (h > 0) gst_structure_fixate_field_nearest_int(out, "height", h);
    const GValue *fr = gst_structure_get_value(in, "framerate");
    if (fr && GST_VALUE_HOLDS_FRACTION(fr))
        gst_structure_fixate_field_nearest_fraction(out, "framerate", gst_value_get_fraction_numerator(fr),
                                                    gst_value_get_fraction_denominator(fr));
    return gst_caps_fixate(othercaps);
}

static gboolean gst_isp_autoframe_set_info(GstVideoFilter *filter, GstCaps *incaps, GstVideoInfo *in_info,
                                           GstCaps *outcaps, GstVideoInfo *out_info) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(filter);
    (void)incaps;
    (void)outcaps;
    State *st = self->st;
    st->in_w = GST_VIDEO_INFO_WIDTH(in_info);
    st->in_h = GST_VIDEO_INFO_HEIGHT(in_info);
    st->out_w = GST_VIDEO_INFO_WIDTH(out_info);
    st->out_h = GST_VIDEO_INFO_HEIGHT(out_info);
    GST_OBJECT_LOCK(self);
    st->ctl.p.extend_aspect = 0;
    st->ctl.reset(st->in_w, st->in_h, (float)st->out_w / st->out_h);
    apply_params(self);
    GST_OBJECT_UNLOCK(self);
    /* A fresh output buffer when the sizes differ: the crop writes every
     * pixel of it anyway, and downstream reads a compact 1080p buffer
     * instead of a 1080p picture inside a 4K one. */
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(filter), st->in_w == st->out_w && st->in_h == st->out_h);
    st->dets.clear();
    st->last_pts = GST_CLOCK_TIME_NONE;
    GST_INFO_OBJECT(self, "in %dx%d -> out %dx%d, full window %.0f px wide, max zoom %.2f", st->in_w, st->in_h,
                    st->out_w, st->out_h, st->ctl.current().w, self->max_zoom);
    return TRUE;
}

static gboolean gst_isp_autoframe_start(GstBaseTransform *trans) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(trans);
    State *st = self->st;
    GST_OBJECT_LOCK(self);
    std::string model = self->model ? self->model : DEFAULT_MODEL;
    int dw = self->det_width, dh = self->det_height, threads = self->threads;
    GST_OBJECT_UNLOCK(self);
    /* OpenCV's pool is process-wide; the publisher does nothing else with
     * OpenCV. Measured on the X1E (630 frames, 4K in, 1080p out): 1 thread
     * 5.6 ms of CPU per frame, 2 threads 4.1 ms, 4 threads 5.0 ms. */
    cv::setNumThreads(threads);
    try {
        st->det.reset(new autoframe::YuNet(model, dw, dh));
    } catch (const std::exception &e) {
        GST_ELEMENT_ERROR(self, LIBRARY, INIT, ("Face detector unavailable"), ("%s: %s", model.c_str(), e.what()));
        return FALSE;
    }
    GST_INFO_OBJECT(self, "face detector %s (OpenCV DNN, CPU, %d thread(s)), %dx%d", model.c_str(), threads, dw, dh);
    st->frame_no = 0;
    st->t_det = st->t_crop = 0;
    st->n_timed = 0;
    st->ctl_mtime = {0, 0};
    st->ctl_size = -1;
    return TRUE;
}

static gboolean gst_isp_autoframe_stop(GstBaseTransform *trans) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(trans);
    self->st->det.reset();
    return TRUE;
}

static gboolean truthy(const gchar *v) { return g_strcmp0(v, "1") == 0 || g_strcmp0(v, "true") == 0 || g_strcmp0(v, "on") == 0; }

/* Control file: "mode=auto|off" and key=value lines, re-read when it changes
 * (mtime with nanoseconds, or size). Lets the user's tool and the bar widget
 * reach the element inside the publisher; the same file is the saved
 * configuration, so there is nothing to restore at login. */
static void poll_control_file(GstIspAutoFrame *self) {
    GST_OBJECT_LOCK(self);
    gchar *path = g_strdup(self->control_file);
    GST_OBJECT_UNLOCK(self);
    if (!path) return;
    struct stat sb;
    if (stat(path, &sb) != 0) {
        g_free(path);
        return;
    }
    State *st = self->st;
    if (sb.st_mtim.tv_sec == st->ctl_mtime.tv_sec && sb.st_mtim.tv_nsec == st->ctl_mtime.tv_nsec &&
        sb.st_size == st->ctl_size) {
        g_free(path);
        return;
    }
    st->ctl_mtime = sb.st_mtim;
    st->ctl_size = sb.st_size;
    gchar *data = nullptr;
    if (g_file_get_contents(path, &data, nullptr, nullptr)) {
        gchar **lines = g_strsplit(data, "\n", -1);
        for (gchar **l = lines; *l; l++) {
            gchar *line = g_strstrip(*l);
            if (g_str_has_prefix(line, "mode=")) {
                gboolean on = g_strcmp0(line + 5, "off") != 0;
                GST_INFO_OBJECT(self, "control file: mode=%s", line + 5);
                g_object_set(self, "enable", on, NULL);
            } else if (g_str_has_prefix(line, "max-zoom=")) {
                g_object_set(self, "max-zoom", g_ascii_strtod(line + 9, nullptr), NULL);
            } else if (g_str_has_prefix(line, "face-frac=")) {
                g_object_set(self, "face-frac", g_ascii_strtod(line + 10, nullptr), NULL);
            } else if (g_str_has_prefix(line, "headroom=")) {
                g_object_set(self, "headroom", g_ascii_strtod(line + 9, nullptr), NULL);
            } else if (g_str_has_prefix(line, "speed=")) {
                g_object_set(self, "speed", g_ascii_strtod(line + 6, nullptr), NULL);
            } else if (g_str_has_prefix(line, "hold-time=")) {
                g_object_set(self, "hold-time", g_ascii_strtod(line + 10, nullptr), NULL);
            } else if (g_str_has_prefix(line, "group=")) {
                g_object_set(self, "group", truthy(line + 6), NULL);
            } else if (g_str_has_prefix(line, "draw-boxes=")) {
                g_object_set(self, "draw-boxes", truthy(line + 11), NULL);
            }
        }
        g_strfreev(lines);
        g_free(data);
    }
    g_free(path);
}

/* Detection on the frame: the NV12 planes are shrunk to the detector size
 * (letterboxed to an integer-friendly 16:9 strip inside it), converted to
 * BGR once at that size. Detections come back in frame pixels. */
static void run_detector(GstIspAutoFrame *self, const cv::Mat &y_in, const cv::Mat &uv_in) {
    State *st = self->st;
    const int iw = y_in.cols, ih = y_in.rows;
    const int dw = st->det->width(), dh = st->det->height();
    int tw = dw, th = std::min(dh, (int)std::lround((double)ih * dw / iw)) & ~1;
    if (th < 2) th = 2;
    if (st->nv12small.empty()) {
        st->nv12small.create(dh * 3 / 2, dw, CV_8UC1);
        st->nv12small(cv::Rect(0, 0, dw, dh)).setTo(0);
        st->nv12small(cv::Rect(0, dh, dw, dh / 2)).setTo(128);
    }
    cv::Mat ydst = st->nv12small(cv::Rect(0, 0, tw, th));
    cv::resize(y_in, ydst, ydst.size(), 0, 0, cv::INTER_AREA);
    cv::resize(uv_in, st->uvsmall, cv::Size(tw / 2, th / 2), 0, 0, cv::INTER_AREA);
    st->uvsmall.reshape(1, th / 2).copyTo(st->nv12small(cv::Rect(0, dh, tw, th / 2)));
    cv::cvtColor(st->nv12small, st->bgr, cv::COLOR_YUV2BGR_NV12);
    st->dets = st->det->infer(st->bgr);
    const float sx = (float)iw / tw, sy = (float)ih / th;
    for (auto &d : st->dets) {
        d.x *= sx; d.w *= sx; d.y *= sy; d.h *= sy;
    }
}

/* Common per-frame work: control file, dt, detection, controller. Returns the
 * window in input pixels. */
static autoframe::Window frame_step(GstIspAutoFrame *self, GstVideoFrame *in, gint64 *t_det_us) {
    State *st = self->st;
    if (st->frame_no % 30 == 0) poll_control_file(self);

    float dt = 1.f / 30;
    GstClockTime pts = GST_BUFFER_PTS(in->buffer);
    if (GST_CLOCK_TIME_IS_VALID(pts) && GST_CLOCK_TIME_IS_VALID(st->last_pts) && pts > st->last_pts) {
        dt = (float)((pts - st->last_pts) / 1e9);
        if (dt > 0.5f) dt = 0.5f;
    }
    st->last_pts = pts;

    GST_OBJECT_LOCK(self);
    gboolean enabled = self->enable;
    int interval = self->detect_interval > 0 ? self->detect_interval : 1;
    GST_OBJECT_UNLOCK(self);

    const int iw = GST_VIDEO_FRAME_WIDTH(in), ih = GST_VIDEO_FRAME_HEIGHT(in);
    cv::Mat y_in(ih, iw, CV_8UC1, GST_VIDEO_FRAME_PLANE_DATA(in, 0), GST_VIDEO_FRAME_PLANE_STRIDE(in, 0));
    cv::Mat uv_in(ih / 2, iw / 2, CV_8UC2, GST_VIDEO_FRAME_PLANE_DATA(in, 1), GST_VIDEO_FRAME_PLANE_STRIDE(in, 1));

    gint64 t0 = g_get_monotonic_time();
    if (enabled && st->det && st->frame_no % interval == 0) run_detector(self, y_in, uv_in);
    if (!enabled) st->dets.clear();
    *t_det_us = g_get_monotonic_time() - t0;

    GST_OBJECT_LOCK(self);
    autoframe::Window win = st->ctl.step(st->dets, dt);
    GST_OBJECT_UNLOCK(self);
    return win;
}

static void round_window(autoframe::Window &win, int max_w, int max_h, int &x, int &y, int &w, int &h) {
    x = ((int)std::lround(win.x)) & ~1; y = ((int)std::lround(win.y)) & ~1;
    w = ((int)std::lround(win.w)) & ~1; h = ((int)std::lround(win.h)) & ~1;
    if (w < 2) w = 2;
    if (h < 2) h = 2;
    if (x + w > max_w) x = max_w - w;
    if (y + h > max_h) y = max_h - h;
    if (x < 0) x = 0;
    if (y < 0) y = 0;
}

static void draw_dets(GstIspAutoFrame *self, cv::Mat &y_out, float ox, float oy, float sx, float sy) {
    for (const auto &d : self->st->dets)
        cv::rectangle(y_out, cv::Rect((int)((d.x - ox) * sx), (int)((d.y - oy) * sy), (int)(d.w * sx), (int)(d.h * sy)),
                      cv::Scalar(235), 2);
}

static void log_timing(GstIspAutoFrame *self, gint64 det_us, gint64 crop_us, int x, int y, int w, int h, float full_w) {
    State *st = self->st;
    st->t_det += det_us / 1000.0;
    st->t_crop += crop_us / 1000.0;
    if (++st->n_timed == 300) {
        GST_DEBUG_OBJECT(self, "avg per frame: detect %.2f ms, crop+scale %.2f ms; window %d,%d %dx%d zoom %.2f, %zu face(s)",
                         st->t_det / 300, st->t_crop / 300, x, y, w, h, w > 0 ? full_w / w : 0.f, st->dets.size());
        st->t_det = st->t_crop = 0;
        st->n_timed = 0;
    }
    st->frame_no++;
}

/* Equal sizes: crop and scale through a copy, in place. */
static GstFlowReturn gst_isp_autoframe_transform_frame_ip(GstVideoFilter *filter, GstVideoFrame *frame) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(filter);
    State *st = self->st;
    gint64 det_us = 0;
    autoframe::Window win = frame_step(self, frame, &det_us);
    gint64 t1 = g_get_monotonic_time();
    const int fw = GST_VIDEO_FRAME_WIDTH(frame), fh = GST_VIDEO_FRAME_HEIGHT(frame);
    GST_OBJECT_LOCK(self);
    gboolean draw = self->draw_boxes;
    GST_OBJECT_UNLOCK(self);
    int x, y, w, h;
    round_window(win, fw, fh, x, y, w, h);
    cv::Mat y_io(fh, fw, CV_8UC1, GST_VIDEO_FRAME_PLANE_DATA(frame, 0), GST_VIDEO_FRAME_PLANE_STRIDE(frame, 0));
    cv::Mat uv_io(fh / 2, fw / 2, CV_8UC2, GST_VIDEO_FRAME_PLANE_DATA(frame, 1), GST_VIDEO_FRAME_PLANE_STRIDE(frame, 1));
    if (w != fw || h != fh) {
        cv::Mat yc, uvc;
        cv::resize(y_io(cv::Rect(x, y, w, h)), yc, y_io.size(), 0, 0, cv::INTER_LINEAR);
        cv::resize(uv_io(cv::Rect(x / 2, y / 2, w / 2, h / 2)), uvc, uv_io.size(), 0, 0, cv::INTER_LINEAR);
        yc.copyTo(y_io);
        uvc.copyTo(uv_io);
    }
    if (draw) draw_dets(self, y_io, x, y, (float)fw / w, (float)fh / h);
    log_timing(self, det_us, g_get_monotonic_time() - t1, x, y, w, h, std::min(fw, (int)(fh * (float)fw / fh)));
    (void)st;
    return GST_FLOW_OK;
}

/* Different sizes: crop and scale from in to out. */
static GstFlowReturn gst_isp_autoframe_transform_frame(GstVideoFilter *filter, GstVideoFrame *in, GstVideoFrame *out) {
    GstIspAutoFrame *self = GST_ISP_AUTOFRAME(filter);
    State *st = self->st;
    const int iw = st->in_w, ih = st->in_h, ow = st->out_w, oh = st->out_h;
    gint64 det_us = 0;
    autoframe::Window win = frame_step(self, in, &det_us);
    gint64 t1 = g_get_monotonic_time();
    GST_OBJECT_LOCK(self);
    gboolean draw = self->draw_boxes;
    GST_OBJECT_UNLOCK(self);
    int x, y, w, h;
    round_window(win, iw, ih, x, y, w, h);

    cv::Mat y_in(ih, iw, CV_8UC1, GST_VIDEO_FRAME_PLANE_DATA(in, 0), GST_VIDEO_FRAME_PLANE_STRIDE(in, 0));
    cv::Mat uv_in(ih / 2, iw / 2, CV_8UC2, GST_VIDEO_FRAME_PLANE_DATA(in, 1), GST_VIDEO_FRAME_PLANE_STRIDE(in, 1));
    cv::Mat y_out(oh, ow, CV_8UC1, GST_VIDEO_FRAME_PLANE_DATA(out, 0), GST_VIDEO_FRAME_PLANE_STRIDE(out, 0));
    cv::Mat uv_out(oh / 2, ow / 2, CV_8UC2, GST_VIDEO_FRAME_PLANE_DATA(out, 1), GST_VIDEO_FRAME_PLANE_STRIDE(out, 1));
    if (w == ow && h == oh) {
        y_in(cv::Rect(x, y, w, h)).copyTo(y_out);
        uv_in(cv::Rect(x / 2, y / 2, w / 2, h / 2)).copyTo(uv_out);
    } else {
        /* INTER_AREA only at >= 2x (OpenCV's integer fast path); its generic
         * path at 1.x ratios is several times slower single-threaded. */
        int interp = w >= 2 * ow ? cv::INTER_AREA : cv::INTER_LINEAR;
        cv::resize(y_in(cv::Rect(x, y, w, h)), y_out, y_out.size(), 0, 0, interp);
        cv::resize(uv_in(cv::Rect(x / 2, y / 2, w / 2, h / 2)), uv_out, uv_out.size(), 0, 0, interp);
    }
    if (draw) draw_dets(self, y_out, x, y, (float)ow / w, (float)oh / h);
    log_timing(self, det_us, g_get_monotonic_time() - t1, x, y, w, h, std::min(iw, (int)(ih * (float)ow / oh)));
    return GST_FLOW_OK;
}

static void gst_isp_autoframe_class_init(GstIspAutoFrameClass *klass) {
    GObjectClass *gobject_class = G_OBJECT_CLASS(klass);
    GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
    GstBaseTransformClass *trans_class = GST_BASE_TRANSFORM_CLASS(klass);
    GstVideoFilterClass *filter_class = GST_VIDEO_FILTER_CLASS(klass);

    gobject_class->set_property = gst_isp_autoframe_set_property;
    gobject_class->get_property = gst_isp_autoframe_get_property;
    gobject_class->finalize = gst_isp_autoframe_finalize;

    auto flags = (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS);
    g_object_class_install_property(gobject_class, PROP_ENABLE,
        g_param_spec_boolean("enable", "Enable", "Follow faces (false: full view, scaled)", TRUE, flags));
    g_object_class_install_property(gobject_class, PROP_MODEL,
        g_param_spec_string("model", "Model", "YuNet ONNX model path", DEFAULT_MODEL, flags));
    g_object_class_install_property(gobject_class, PROP_DET_WIDTH,
        g_param_spec_int("det-width", "Detector width", "Detector input width (multiple of 32)", 32, 1024, 320, flags));
    g_object_class_install_property(gobject_class, PROP_DET_HEIGHT,
        g_param_spec_int("det-height", "Detector height", "Detector input height (multiple of 32)", 32, 1024, 192, flags));
    g_object_class_install_property(gobject_class, PROP_THREADS,
        g_param_spec_int("threads", "Threads", "OpenCV worker threads for the detector and the crop (2 measured cheapest in total CPU)", 1, 16, 2, flags));
    g_object_class_install_property(gobject_class, PROP_MAX_ZOOM,
        g_param_spec_double("max-zoom", "Max zoom", "Tightest crop as a factor of the full view", 1.0, 8.0, 2.0, flags));
    g_object_class_install_property(gobject_class, PROP_FACE_FRAC,
        g_param_spec_double("face-frac", "Face fraction", "Face height as a fraction of the window height", 0.1, 0.9, 0.30, flags));
    g_object_class_install_property(gobject_class, PROP_HEADROOM,
        g_param_spec_double("headroom", "Headroom", "Face centre above window centre, fraction of window height", -0.3, 0.4, 0.12, flags));
    g_object_class_install_property(gobject_class, PROP_SPEED,
        g_param_spec_double("speed", "Speed", "Spring natural frequency, rad/s (bigger: snappier)", 0.5, 20.0, 4.0, flags));
    g_object_class_install_property(gobject_class, PROP_HOLD_TIME,
        g_param_spec_double("hold-time", "Hold time", "Seconds to hold the window after the face is lost", 0.0, 60.0, 3.0, flags));
    g_object_class_install_property(gobject_class, PROP_GROUP,
        g_param_spec_boolean("group", "Group", "Frame all faces (false: the largest)", TRUE, flags));
    g_object_class_install_property(gobject_class, PROP_DETECT_INTERVAL,
        g_param_spec_int("detect-interval", "Detect interval", "Run the detector every N frames", 1, 30, 2, flags));
    g_object_class_install_property(gobject_class, PROP_CONTROL_FILE,
        g_param_spec_string("control-file", "Control file",
            "File with mode=auto|off (and max-zoom=, face-frac=, headroom=, speed=, hold-time=, group=, draw-boxes=), re-read on change", NULL, flags));
    g_object_class_install_property(gobject_class, PROP_DRAW_BOXES,
        g_param_spec_boolean("draw-boxes", "Draw boxes", "Draw detections on the output (debug)", FALSE, flags));

    gst_element_class_set_static_metadata(element_class, "Qualcomm ISP automatic framing", "Filter/Effect/Video",
        "Crops and scales NV12 video to follow faces (YuNet through OpenCV DNN)", "qualcomm-isp");
    gst_element_class_add_static_pad_template(element_class, &sink_template);
    gst_element_class_add_static_pad_template(element_class, &src_template);

    trans_class->transform_caps = GST_DEBUG_FUNCPTR(gst_isp_autoframe_transform_caps);
    trans_class->fixate_caps = GST_DEBUG_FUNCPTR(gst_isp_autoframe_fixate_caps);
    trans_class->start = GST_DEBUG_FUNCPTR(gst_isp_autoframe_start);
    trans_class->stop = GST_DEBUG_FUNCPTR(gst_isp_autoframe_stop);
    filter_class->set_info = GST_DEBUG_FUNCPTR(gst_isp_autoframe_set_info);
    filter_class->transform_frame = GST_DEBUG_FUNCPTR(gst_isp_autoframe_transform_frame);
    filter_class->transform_frame_ip = GST_DEBUG_FUNCPTR(gst_isp_autoframe_transform_frame_ip);
}

static void gst_isp_autoframe_init(GstIspAutoFrame *self) {
    self->enable = TRUE;
    self->model = g_strdup(DEFAULT_MODEL);
    self->det_width = 320;
    self->det_height = 192;
    self->threads = 2;
    self->max_zoom = 2.0;
    self->face_frac = 0.30;
    self->headroom = 0.12;
    self->speed = 4.0;
    self->hold_time = 3.0;
    self->group = TRUE;
    self->detect_interval = 2;
    self->control_file = NULL;
    self->draw_boxes = FALSE;
    self->st = new State();
    apply_params(self);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
}

static gboolean plugin_init(GstPlugin *plugin) {
    GST_DEBUG_CATEGORY_INIT(gst_isp_autoframe_debug, "ispautoframe", 0, "Qualcomm ISP automatic framing");
    return gst_element_register(plugin, "ispautoframe", GST_RANK_NONE, GST_TYPE_ISP_AUTOFRAME);
}

#define PACKAGE "qualcomm-isp-autoframe"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, ispautoframe, "Qualcomm ISP automatic framing", plugin_init,
                  "0.1", "MIT/X11", "qualcomm-isp-autoframe", "https://github.com/turbineBMW/qualcomm-isp")
