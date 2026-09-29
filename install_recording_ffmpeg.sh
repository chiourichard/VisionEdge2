#!/usr/bin/env bash
# Run as the VisionEdge owner, from the project; sudo is used only for build prerequisites.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project="$PWD"
[[ "$(uname -m)" == aarch64 ]] || { echo 'Requires Jetson aarch64.'; exit 1; }
[[ "$EUID" != 0 ]] || { echo 'Run as the VisionEdge user, not sudo bash.'; exit 1; }
[[ -f long_recording.py && -f recording_ffmpeg.py ]] || { echo 'Extract patch into VisionEdge first.'; exit 1; }
destination="$project/tools/recording-ffmpeg"
[[ ! -e "$destination" ]] || { echo "Already exists: $destination (not overwritten)."; exit 1; }
sudo apt-get update
sudo apt-get install --no-install-recommends --no-remove build-essential pkg-config libx264-dev curl ca-certificates xz-utils
# This does not install/downgrade the ffmpeg or libav packages supplied by NVIDIA.
mkdir -p "$project/tools"
build=$(mktemp -d "$project/tools/recording-ffmpeg-build.XXXXXXXX")
trap 'echo "Build files and diagnostic logs: $build"' EXIT
cd "$build"
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
    https://ffmpeg.org/releases/ffmpeg-7.1.3.tar.xz -o source.tar.xz
tar -xJf source.tar.xz
cd ffmpeg-7.1.3
# The FFmpeg libraries are linked statically into this private executable.
# libx264 and libc use standard Ubuntu libraries; no NVIDIA libav replacement.
env -u LD_LIBRARY_PATH -u PKG_CONFIG_PATH ./configure \
    --prefix="$build/install" --disable-shared --enable-static \
    --disable-autodetect --disable-everything --disable-network \
    --disable-doc --disable-debug --disable-ffplay --disable-ffprobe \
    --enable-gpl --enable-libx264 --enable-ffmpeg \
    --enable-avcodec --enable-avformat --enable-avfilter --enable-swscale \
    --enable-encoder=libx264 --enable-decoder=rawvideo \
    --enable-demuxer=rawvideo --enable-muxer=segment,mp4 \
    --enable-protocol=file,pipe --enable-filter=scale,format,null,fps \
    2>&1 | tee "$build/configure.log"
make -j2 2>&1 | tee "$build/build.log"
make install 2>&1 | tee "$build/install.log"
mv "$build/install/bin/ffmpeg" "$build/install/bin/ffmpeg.bin"
cat > "$build/install/bin/ffmpeg" <<'WRAPPER'
#!/bin/sh
# Keep CUDA/OpenCV's private library path out of the encoder subprocess only.
exec env -u LD_LIBRARY_PATH "$(dirname -- "$0")/ffmpeg.bin" "$@"
WRAPPER
chmod +x "$build/install/bin/ffmpeg"
cd "$project"
python_cmd=python3
if [[ -x .venv/bin/python ]]; then python_cmd="$project/.venv/bin/python"; fi
# Publish only after actual BGR -> H.264 -> closed MP4 segment validation.
VISIONEDGE_RECORDING_FFMPEG="$build/install/bin/ffmpeg" "$python_cmd" check_recording_env.py
mv "$build/install" "$destination"
"$python_cmd" check_recording_env.py
echo 'PASS: private recording FFmpeg installed. Restart VisionEdge to use the updated recorder.'
