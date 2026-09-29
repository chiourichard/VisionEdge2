"""Bounded, independent H.264 encoding for annotated / portable recording.

An isolated FFmpeg/libx264 process owns encoding and keyframe-aligned segmentation.
Jetson Orin Nano has no hardware NVENC; CUDA inference is independent. Frames are scheduled
against a monotonic clock; the latest *complete* inference image is repeated
between inferences (old boxes are never drawn onto a different camera image).
There is no unbounded queue and no silent codec fallback.
"""
from __future__ import annotations

import collections
import datetime
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
from recording_ffmpeg import recording_ffmpeg


class H264Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.mode = 'raw'
        self.path = None
        self.frames = 0
        self.error = ''
        self.prefix = ''
        self.folder = None
        self.completed = set()
        self.completed_bytes = 0
        self.size = None
        self.fps = 0
        self.started_at = None
        self.stderr_tail = collections.deque(maxlen=12)
        self.samples = collections.deque(maxlen=61)
        self.last_scan = 0
        self.total_bytes = 0
        self.segment_count = 0
        self.overruns = 0
        self.latest = None
        self.latest_at = 0
        self.encoder = 'libx264'

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def owns_active(self, path):
        # FFmpeg writes with a private .part extension until it closes a segment.
        return path.name.endswith('.mp4.part') and path.name.startswith(self.prefix)

    def _drain_errors(self, process):
        for line in iter(process.stderr.readline, b''):
            self.stderr_tail.append(line.decode('utf-8', 'replace').strip())
        process.stderr.close()

    def _watchdog(self, process):
        while not self.stop_event.wait(.5):
            if not self.active:
                return
            if time.monotonic() - self.last_progress > 5:
                self.error = self.error or '錄影編碼器超過 5 秒未回應，最後一段可能不完整'
                self.stop_event.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return

    def start(self, frame, path, mode, fps=None):
        if self.active:
            return False, 'recording already active'
        if getattr(self, 'watchdog', None):
            self.watchdog.join(timeout=1)
        # Orin Nano has no NVENC. CUDA/GStreamer capture must not select a hardware encoder.
        executable = recording_ffmpeg()
        self.encoder = 'libx264'
        if not executable:
            return False, '長時間錄影需要 FFmpeg（含 libx264）；請由設備管理員安裝後重試。'
        self.mode = mode
        self.folder = Path(path).parent
        self.folder.mkdir(parents=True, exist_ok=True)
        self.prefix = 'rec_' + datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8]
        self.path = None
        self.frames = 0
        self.error = ''
        self.completed = set()
        self.completed_bytes = 0
        self.stderr_tail.clear()
        self.samples.clear()
        self.total_bytes = self.segment_count = self.overruns = 0
        self.last_scan = 0
        self.fps = float(self.cfg.recording_fps)
        h, w = frame.shape[:2]
        if self.cfg.recording_max_height:
            scale = min(1., self.cfg.recording_max_height / h)
        else:
            scale = min(1., (self.cfg.recording_width or w) / w, (self.cfg.recording_height or h) / h)
        self.size = (max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2))
        bitrate = int(self.cfg.recording_bitrate or 2_500_000)
        segment = int(self.cfg.recording_segment_seconds)
        # Use the segment counter for the live filename. A wall-clock step must
        # never overwrite an earlier segment in the same recording session.
        pattern = self.folder / (self.prefix + '_%06d.mp4.part')
        # Only finalized segments appear in this CSV. It is kept outside media
        # listings and removed on shutdown; incomplete .part files remain visible.
        self.manifest = self.folder / (self.prefix + '.csv')
        # Force a keyframe at each segment boundary. Do not require the optional
        # sc_threshold option: some deployed FFmpeg builds do not expose it.
        command = [executable, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n', '-filter_threads', '1',
                   '-f', 'rawvideo', '-pixel_format', 'bgr24', '-video_size', f'{self.size[0]}x{self.size[1]}',
                   '-framerate', str(self.fps), '-i', 'pipe:0', '-map', '0:v:0', '-an', '-c:v', 'libx264',
                   '-preset', 'veryfast', '-tune', 'zerolatency', '-threads', '2',
                   '-pix_fmt', 'yuv420p', '-b:v', str(bitrate), '-maxrate', str(bitrate),
                   '-bufsize', str(bitrate * 2), '-g', str(max(1, round(self.fps * 2))),
                   '-force_key_frames', f'expr:gte(t,n_forced*{segment})',
                   '-f', 'segment', '-segment_format', 'mp4', '-segment_time', str(segment),
                   '-reset_timestamps', '1', '-segment_list', str(self.manifest),
                   '-segment_list_type', 'csv', str(pattern)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, bufsize=0,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            return False, f'無法啟動 H.264 編碼器：{exc}'
        self.error_thread = threading.Thread(target=self._drain_errors, args=(self.process,), daemon=True)
        self.error_thread.start()
        self.stop_event.clear()
        self.ready.clear()
        self.started_at = time.time()
        self.start_mono = time.monotonic()
        self.last_progress = self.start_mono
        self.latest = frame.copy()
        self.latest_at = self.start_mono
        self.thread = threading.Thread(target=self._run, name='h264-record-writer', daemon=True)
        self.thread.start()
        self.watchdog = threading.Thread(target=self._watchdog, args=(self.process,), daemon=True)
        self.watchdog.start()
        if not self.ready.wait(8):
            self.error = 'H.264 編碼器啟動逾時'
            self.stop()
        if self.error or not self.active:
            return False, self.error or 'H.264 編碼器啟動失敗'
        return True, str(self.path or pattern)

    def write(self, raw, result):
        if not self.active or self.stop_event.is_set():
            return
        frame = result if self.mode == 'result' else raw
        if frame is None:
            return
        with self.lock:
            # Runtime owns these immutable frame arrays. Keep one reference;
            # resizing/copying/pipe I/O belongs exclusively to the worker.
            self.latest = frame
            self.latest_at = time.monotonic()

    def _scan(self, publish=True):
        import csv
        if not self.folder:
            return
        if publish and self.manifest.exists():
            with self.manifest.open(encoding='utf-8', newline='') as stream:
                for row in csv.reader(stream):
                    if len(row) != 3:
                        continue
                    source = self.folder / Path(row[0]).name
                    if not source.name.startswith(self.prefix) or source.name in self.completed:
                        continue
                    if source.exists():
                        segment_time = datetime.datetime.fromtimestamp(self.started_at + float(row[1]))
                        stamp = segment_time.strftime('%Y-%m-%d_%H%M%S_%f')[:-3]
                        destination = source.with_name(source.name.removesuffix('.mp4.part') + '_' + stamp + '.mp4')
                        source.rename(destination)
                        self.completed.add(source.name)
                        self.completed_bytes += destination.stat().st_size
        paths = sorted(self.folder.glob(self.prefix + '_*.mp4*'))
        if paths:
            self.path = paths[-1]
        pending = [p for p in paths if p.suffix == '.part']
        self.segment_count = len(self.completed) + len(pending)
        self.total_bytes = self.completed_bytes + sum(p.stat().st_size for p in pending if p.exists())
        now = time.monotonic()
        if now - self.last_scan >= 1:
            self.samples.append((now, self.total_bytes))
            self.last_scan = now

    def _run(self):
        process = self.process
        next_frame = self.start_mono
        try:
            while not self.stop_event.is_set():
                if process.poll() is not None:
                    raise RuntimeError('H.264 編碼器提前退出')
                now = time.monotonic()
                if self.stop_event.wait(max(0., next_frame - now)):
                    break
                now = time.monotonic()
                # Do not create a time-compressed or indefinitely delayed file
                # under sustained encoder overload. Fail visibly instead.
                if now - next_frame > 2:
                    self.overruns += 1
                    raise RuntimeError('錄影編碼落後超過 2 秒；請降低錄影解析度或 FPS')
                with self.lock:
                    frame, received = self.latest, self.latest_at
                if now - received > 3:
                    raise RuntimeError('錄影影像超過 3 秒未更新')
                frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                data = memoryview(frame).cast('B')
                while data:
                    written = process.stdin.write(data)
                    if not written:
                        raise RuntimeError('錄影寫入失敗')
                    data = data[written:]
                self.frames += 1
                self.last_progress = time.monotonic()
                next_frame = self.start_mono + self.frames / self.fps
                if now - self.last_scan >= .25:
                    self._scan()
                    if self.path and self.path.exists() and self.path.stat().st_size:
                        self.ready.set()
                if self.cfg.min_free_mb > 0 and shutil.disk_usage(self.folder).free < self.cfg.min_free_mb * 1024**2:
                    raise RuntimeError('錄影已停止：儲存空間低於保留值')
        except Exception as exc:
            self.error = self.error or str(exc)
        finally:
            code = None
            try:
                process.stdin.close()
                code = process.wait(timeout=12)
                if code:
                    self.error = self.error or f'H.264 編碼器失敗（{code}）'
            except Exception as exc:
                self.error = self.error or f'影片關檔失敗：{exc}'
                process.kill()
                process.wait()
            self.error_thread.join(timeout=2)
            if self.error and self.stderr_tail:
                self.error += ': ' + ' '.join(self.stderr_tail)[-1500:]
            try:
                self._scan(publish=code == 0)
                if code == 0 and (not self.completed or list(self.folder.glob(self.prefix + '_*.part'))):
                    self.error = self.error or '錄影未完整完成封裝，請檢查未完成檔案'
                self.manifest.unlink(missing_ok=True)
            except Exception as exc:
                self.error = self.error or f'影片完成狀態更新失敗：{exc}'
            self.ready.set()
            self.stop_event.set()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                self.error = '錄影關檔逾時，最後一段可能不完整'
                self.process.kill()  # also unblocks a stalled pipe write
                self.thread.join(timeout=5)
        if getattr(self, 'watchdog', None):
            self.watchdog.join(timeout=1)
        return {'success': not bool(self.error), 'error': self.error,
                'path': str(self.path or ''), 'frames': self.frames,
                'duration_sec': round(self.frames / self.fps, 2) if self.fps else 0,
                'segments': len(self.completed), 'fps': self.fps}

    def status(self, free_mb=None, reserve_mb=0):
        samples = list(self.samples)
        rate = None
        if len(samples) >= 2 and samples[-1][0] - samples[0][0] >= 5:
            rate = max(0., (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0]))
        remaining = None
        if self.active and free_mb is not None and rate and rate > 0:
            remaining = max(0, free_mb - reserve_mb) * 1024**2 / rate
        return {'recording_encoder': self.encoder, 'recording_error': self.error,
                'recording_output_size': self.size, 'recording_output_fps': self.fps,
                'recording_segments': self.segment_count, 'recording_bytes': self.total_bytes,
                'recording_mbps': round(rate * 8 / 1e6, 3) if rate is not None else None,
                'recording_remaining_sec': round(remaining) if remaining is not None else None,
                'recording_overruns': self.overruns}
