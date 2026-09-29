"""Check the same encoder worker used by recording, without opening a camera."""
import csv
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from recording_ffmpeg import recording_ffmpeg


def mp4_complete(path):
    boxes = set()
    with path.open('rb') as stream:
        length = path.stat().st_size
        while stream.tell() + 8 <= length:
            start = stream.tell()
            size, name = struct.unpack('>I4s', stream.read(8))
            header = 8
            if size == 1:
                size = struct.unpack('>Q', stream.read(8))[0]
                header = 16
            if size == 0:
                size = length - start
            if size < header or start + size > length:
                return False
            boxes.add(name)
            stream.seek(start + size)
    return {b'moov', b'mdat'} <= boxes


def main():
    ffmpeg = recording_ffmpeg()
    if not ffmpeg:
        print('FAIL: Jetson Orin Nano requires FFmpeg with libx264 in the VisionEdge service PATH.')
        return 1
    with tempfile.TemporaryDirectory(prefix='visionedge-record-check-') as temporary:
        root = Path(temporary)
        pattern, manifest = root/'check_%06d.mp4.part', root/'closed.csv'
        # Match the recorder: forced boundary keyframes need no sc_threshold option.
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-filter_threads', '1',
                   '-f', 'rawvideo', '-pixel_format', 'bgr24', '-video_size', '640x360', '-framerate', '15',
                   '-i', 'pipe:0', '-map', '0:v:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast',
                   '-tune', 'zerolatency', '-threads', '2', '-pix_fmt', 'yuv420p', '-b:v', '2500000',
                   '-maxrate', '2500000', '-bufsize', '5000000', '-g', '15',
                   '-force_key_frames', 'expr:gte(t,n_forced*1)', '-f', 'segment', '-segment_format', 'mp4',
                   '-segment_time', '1', '-reset_timestamps', '1', '-segment_list', str(manifest),
                   '-segment_list_type', 'csv', str(pattern)]
        print('Testing FFmpeg/libx264:', ffmpeg)
        # Bounded memory; no camera, installation, service restart or network.
        errors = []
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=log, bufsize=0,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            def feed():
                try:
                    frame = bytes((32, 128, 224)) * (640 * 360)
                    for _ in range(60):
                        data = memoryview(frame)
                        while data:
                            count = process.stdin.write(data)
                            if not count:
                                raise RuntimeError('Encoder stopped accepting frames')
                            data = data[count:]
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    process.stdin.close()
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=5)
                errors.append('Encoder check timed out')
            writer.join(timeout=2)
            log.seek(0)
            diagnostic = log.read().decode('utf-8', 'replace')
        clips = list(root.glob('*.mp4.part'))
        rows = list(csv.reader(manifest.open(encoding='utf-8', newline=''))) if manifest.exists() else []
        if code or errors or len(clips) < 2 or len(rows) != len(clips) or not all(mp4_complete(p) for p in clips):
            print('FAIL: H.264 encoding / confirmed MP4 segmentation did not complete.')
            print('Files:', len(clips), 'Closed fragments:', len(rows), 'Exit:', code)
            print('\n'.join(errors))
            print(diagnostic[-8000:])
            return 1
        print(f'PASS: H.264 encoding and {len(clips)} closed MP4 fragments verified.')
        print('Jetson/PC software H.264 path verified; no NVENC is required.')
        print('Camera coexistence, 1080p load and 12/24-hour stability still require on-device testing.')
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('FAIL:', exc)
        sys.exit(1)
