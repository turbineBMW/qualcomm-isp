project('qualcomm-isp-autoframe', 'cpp',
  version: '0.1',
  default_options: ['cpp_std=c++17', 'warning_level=2', 'buildtype=release'])

gst_dep = dependency('gstreamer-1.0')
gstbase_dep = dependency('gstreamer-base-1.0')
gstvideo_dep = dependency('gstreamer-video-1.0')
opencv_dep = dependency('opencv5', required: false)
if not opencv_dep.found()
  opencv_dep = dependency('opencv4')
endif

shared_library('gstispautoframe',
  ['src/gstispautoframe.cpp', 'src/yunet.cpp'],
  dependencies: [gst_dep, gstbase_dep, gstvideo_dep, opencv_dep],
  install: true,
  install_dir: get_option('libdir') / 'gstreamer-1.0')
