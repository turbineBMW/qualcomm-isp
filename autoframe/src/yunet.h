// YuNet face detector through OpenCV's DNN module (CPU).
// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
#pragma once
#include <memory>
#include <string>
#include <vector>
#include <opencv2/core.hpp>
#include "framing.h"

namespace autoframe {

class YuNet {
public:
    // Throws cv::Exception on failure (missing or unreadable model).
    YuNet(const std::string& model, int width, int height, float score_thr = 0.6f, float nms_thr = 0.3f);
    ~YuNet();
    int width() const { return w_; }
    int height() const { return h_; }
    // bgr: CV_8UC3, exactly width x height. Detections in input pixels, best first.
    std::vector<Det> infer(const cv::Mat& bgr);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    int w_, h_;
};

}  // namespace autoframe
