// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
#include "yunet.h"
#include <algorithm>
#include <opencv2/dnn.hpp>
#include <opencv2/objdetect.hpp>

namespace autoframe {

struct YuNet::Impl {
    cv::Ptr<cv::FaceDetectorYN> det;
};

YuNet::YuNet(const std::string& model, int width, int height, float score_thr, float nms_thr)
    : impl_(new Impl), w_(width), h_(height) {
    // OpenCV's own runner for this model: input scaling, the three-stride
    // decode and NMS are its job. Score = sqrt(cls * obj), as upstream.
    impl_->det = cv::FaceDetectorYN::create(model, "", cv::Size(width, height), score_thr, nms_thr, 50,
                                            cv::dnn::DNN_BACKEND_OPENCV, cv::dnn::DNN_TARGET_CPU);
}

YuNet::~YuNet() = default;

std::vector<Det> YuNet::infer(const cv::Mat& bgr) {
    CV_Assert(bgr.type() == CV_8UC3 && bgr.cols == w_ && bgr.rows == h_);
    cv::Mat faces;  // N x 15 float: x, y, w, h, five landmarks (x, y), score
    impl_->det->detect(bgr, faces);
    std::vector<Det> dets;
    for (int i = 0; i < faces.rows; i++) {
        const float* r = faces.ptr<float>(i);
        dets.push_back({r[0], r[1], r[2], r[3], r[14]});
    }
    std::sort(dets.begin(), dets.end(), [](const Det& a, const Det& b) { return a.score > b.score; });
    return dets;
}

}  // namespace autoframe
