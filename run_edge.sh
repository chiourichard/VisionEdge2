#!/bin/sh
# Backward-compatible launcher. VisionEdge is the project/product name.
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
    # OpenCV CUDA installs native libraries into this uv environment on Jetson.
    export LD_LIBRARY_PATH="$PWD/.venv/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
    exec .venv/bin/python -u visionedge_server.py "$@"
fi
exec python3 -u visionedge_server.py "$@"
