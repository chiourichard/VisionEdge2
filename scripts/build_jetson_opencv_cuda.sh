#!/usr/bin/env bash
# Build the OpenCV Python extension against the Jetson CUDA toolkit in the uv venv.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$project_dir/.venv"
opencv_version="${OPENCV_VERSION:-4.11.0}"
build_root="${OPENCV_BUILD_ROOT:-$project_dir/.build/opencv-cuda-$opencv_version}"
source_dir="$build_root/opencv-$opencv_version"
archive="$build_root/opencv-$opencv_version.tar.gz"
contrib_dir="$build_root/opencv_contrib-$opencv_version"
contrib_archive="$build_root/opencv_contrib-$opencv_version.tar.gz"
cuda_dir="${CUDA_HOME:-/usr/local/cuda}"
cd "$project_dir"

if [[ ! -x "$venv_dir/bin/python" ]]; then
    echo "Create the uv Python 3.10 environment first: uv sync --inexact" >&2
    exit 1
fi
if [[ ! -x "$cuda_dir/bin/nvcc" ]]; then
    echo "CUDA compiler is missing: $cuda_dir/bin/nvcc" >&2
    exit 1
fi
mkdir -p "$build_root"
if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 3 --output "$archive" \
        "https://github.com/opencv/opencv/archive/refs/tags/$opencv_version.tar.gz"
fi
if [[ ! -d "$source_dir" ]]; then
    tar -xzf "$archive" -C "$build_root"
fi
if [[ ! -f "$contrib_archive" ]]; then
    curl --fail --location --retry 3 --output "$contrib_archive" \
        "https://github.com/opencv/opencv_contrib/archive/refs/tags/$opencv_version.tar.gz"
fi
if [[ ! -d "$contrib_dir" ]]; then
    tar -xzf "$contrib_archive" -C "$build_root"
fi

cmake -S "$source_dir" -B "$build_root/build" \
    -D CMAKE_BUILD_TYPE=Release \
    -D CMAKE_INSTALL_PREFIX="$venv_dir" \
    -D CMAKE_INSTALL_RPATH="$venv_dir/lib;$cuda_dir/lib64" \
    -D CUDA_TOOLKIT_ROOT_DIR="$cuda_dir" \
    -D CUDA_ARCH_BIN=8.7 \
    -D CUDA_ARCH_PTX= \
    -D OPENCV_EXTRA_MODULES_PATH="$contrib_dir/modules" \
    -D WITH_CUDA=ON \
    -D WITH_CUDNN=OFF \
    -D WITH_CUBLAS=OFF \
    -D WITH_GSTREAMER=ON \
    -D WITH_FFMPEG=ON \
    -D WITH_GTK=OFF \
    -D WITH_QT=OFF \
    -D WITH_OPENGL=OFF \
    -D BUILD_LIST=core,imgproc,imgcodecs,videoio,cudev,cudaarithm,cudaimgproc,python3 \
    -D BUILD_TESTS=OFF \
    -D BUILD_PERF_TESTS=OFF \
    -D BUILD_EXAMPLES=OFF \
    -D BUILD_DOCS=OFF \
    -D BUILD_opencv_apps=OFF \
    -D PYTHON3_EXECUTABLE="$venv_dir/bin/python" \
    -D PYTHON3_PACKAGES_PATH="$venv_dir/lib/python3.10/site-packages"

cmake --build "$build_root/build" --parallel "${OPENCV_BUILD_JOBS:-2}"
cmake --install "$build_root/build"

"$venv_dir/bin/python" - <<'PY'
import cv2
import numpy as np
from template_matching import TemplateMatcher

print('OpenCV:', cv2.__version__)
print('CUDA devices:', cv2.cuda.getCudaEnabledDeviceCount())
if cv2.cuda.getCudaEnabledDeviceCount() < 1:
    raise SystemExit('OpenCV was built without a usable CUDA device')
matcher = TemplateMatcher('cuda')
image = np.random.default_rng(3).integers(0, 256, (128, 128), dtype=np.uint8)
template = image[40:64, 60:84].copy()
matcher.prepare([template])
result = matcher.begin_gray(image).match({
    'id': 1, 'label': 'build-check', 'threshold': 0.8,
    'x': 60, 'y': 40, 'w': 24, 'h': 24, 'search_margin': 10,
    'th': 24, 'tw': 24, 'tpl_gray': template,
})
print('CUDA match:', result)
if not result['pass'] or result['match_loc'] != [60, 40]:
    raise SystemExit('CUDA template matching self-check failed')
PY
