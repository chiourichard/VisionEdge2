#!/usr/bin/env python3
"""VisionEdge single-camera runtime based on LIVE_FLOW_GRAPH_REVIEW.

Goals
-----
* One physical camera owner per process.
* Preview, template/SOP inference, snapshot and recording can run together.
* Qualcomm uses qtiqmmfsrc/v4l2h264enc; Jetson USB cameras use OpenCV with
  V4L2 or GStreamer MJPEG capture and optional CUDA template matching.
* Existing LIVE_FLOW_GRAPH_REVIEW product/template/SOP data model is reused by VisionEdge.

The QTI backend uses one camera pipeline with a tee. One branch produces JPEG
frames for CPU-side template matching; another branch encodes H.264 once and
feeds an on-demand MP4 recorder. This avoids the old capture-vs-inference camera
handoff used by smartcam_webserver.
"""
from __future__ import annotations

import copy
import functools
import configparser
import datetime as _dt
import json
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

try:  # Only present on the Qualcomm image / GStreamer-enabled Linux hosts.
    import gi  # type: ignore
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst  # type: ignore
    GST_AVAILABLE = True
except Exception:
    Gst = None  # type: ignore
    GST_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / 'config' / 'edge.ini'
EDGE_RUNTIME_DIR = BASE_DIR / 'runtime_data' / 'edge'
EDGE_PHOTO_DIR = EDGE_RUNTIME_DIR / 'photos'
EDGE_RECORD_DIR = EDGE_RUNTIME_DIR / 'recordings'
EDGE_LOG_DIR = EDGE_RUNTIME_DIR / 'logs'


def _ensure_dirs() -> None:
    for p in (EDGE_RUNTIME_DIR, EDGE_PHOTO_DIR, EDGE_RECORD_DIR, EDGE_LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _safe_int(v: Any, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        x = int(v)
    except Exception:
        x = int(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _safe_float(v: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        x = float(v)
    except Exception:
        x = float(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _timestamp() -> str:
    return _dt.datetime.now().strftime('%Y-%m-%d_%H%M%S_%f')[:-3]


def _jpeg(frame: np.ndarray, quality: int = 80, max_width: int = 0) -> bytes:
    if max_width and frame.shape[1] > max_width:
        height = round(frame.shape[0] * max_width / frame.shape[1])
        frame = cv2.resize(frame, (max_width, height), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError('JPEG encode failed')
    return bytes(buf)


def _decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


@dataclass
class EdgeConfig:
    backend: str = 'auto'               # auto | qti | opencv
    source: str = '0'                   # camera index / file / URI for opencv
    capture_mode: str = 'v4l2'          # v4l2 | gstreamer (USB MJPEG)
    camera: int = 0                     # qti camera id
    width: int = 1920
    height: int = 1080
    framerate: str = '30/1'
    rotation: int = 180                 # 0 | 180
    ldc: bool = False
    product_id: int = 0                 # 0 = preview only
    infer_fps: float = 5.0
    preview_quality: int = 80
    preview_max_width: int = 0         # 0 = original resolution
    snapshot_quality: int = 92
    # 0 = automatic by resolution (8 Mbps at 1080p, 16 Mbps at 4K).
    recording_bitrate: int = 0
    recording_fps: float = 30.0
    record_source: str = 'raw'          # raw | result; QTI HW recorder is raw
    min_free_mb: int = 10240
    reconnect_sec: float = 2.0
    loop_source: bool = True
    auto_start: bool = True
    method: str = 'TM_CCOEFF_NORMED'
    inference_device: str = 'cpu'       # cpu | cuda
    recording_width: int = 0            # 0 = camera width
    recording_height: int = 0           # 0 = camera height
    camera_controls_mode: str = 'off'  # off | manual for V4L2 UVC cameras
    camera_control_values: str = '{}'

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> 'EdgeConfig':
        cfg = cls()
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8')
        sec = cp['edge'] if cp.has_section('edge') else {}
        cfg.backend = str(sec.get('backend', cfg.backend)).strip().lower()
        cfg.source = str(sec.get('source', cfg.source)).strip()
        cfg.capture_mode = str(sec.get('capture_mode', cfg.capture_mode)).strip().lower()
        if cfg.capture_mode not in ('v4l2', 'gstreamer'):
            raise ValueError('capture_mode must be v4l2 or gstreamer')
        cfg.camera = _safe_int(sec.get('camera', cfg.camera), cfg.camera, 0)
        cfg.width = _safe_int(sec.get('width', cfg.width), cfg.width, 64)
        cfg.height = _safe_int(sec.get('height', cfg.height), cfg.height, 64)
        cfg.framerate = str(sec.get('framerate', cfg.framerate)).strip() or cfg.framerate
        cfg.rotation = 180 if _safe_int(sec.get('rotation', cfg.rotation), cfg.rotation) == 180 else 0
        cfg.ldc = _truthy(sec.get('ldc', cfg.ldc))
        cfg.product_id = _safe_int(sec.get('product_id', cfg.product_id), cfg.product_id, 0)
        cfg.infer_fps = _safe_float(sec.get('infer_fps', cfg.infer_fps), cfg.infer_fps, 0.1, 30.0)
        cfg.preview_quality = _safe_int(sec.get('preview_quality', cfg.preview_quality), cfg.preview_quality, 30, 100)
        cfg.preview_max_width = _safe_int(sec.get('preview_max_width', cfg.preview_max_width), cfg.preview_max_width, 0, 8192)
        cfg.snapshot_quality = _safe_int(sec.get('snapshot_quality', cfg.snapshot_quality), cfg.snapshot_quality, 50, 100)
        cfg.recording_bitrate = _safe_int(sec.get('recording_bitrate', cfg.recording_bitrate), cfg.recording_bitrate, 0)
        cfg.recording_fps = _safe_float(sec.get('recording_fps', cfg.recording_fps), cfg.recording_fps, 1.0, 120.0)
        cfg.record_source = str(sec.get('record_source', cfg.record_source)).strip().lower()
        cfg.record_source = cfg.record_source if cfg.record_source in ('raw', 'result') else 'raw'
        cfg.min_free_mb = _safe_int(sec.get('min_free_mb', cfg.min_free_mb), cfg.min_free_mb, 0)
        cfg.reconnect_sec = _safe_float(sec.get('reconnect_sec', cfg.reconnect_sec), cfg.reconnect_sec, 0.2, 30.0)
        cfg.loop_source = _truthy(sec.get('loop_source', cfg.loop_source))
        cfg.auto_start = _truthy(sec.get('auto_start', cfg.auto_start))
        cfg.method = str(sec.get('method', cfg.method)).strip() or cfg.method
        cfg.inference_device = str(sec.get('inference_device', cfg.inference_device)).strip().lower()
        if cfg.inference_device not in ('cpu', 'cuda'):
            raise ValueError('inference_device must be cpu or cuda')
        cfg.recording_width = _safe_int(sec.get('recording_width', cfg.recording_width), cfg.recording_width, 0, 8192)
        cfg.recording_height = _safe_int(sec.get('recording_height', cfg.recording_height), cfg.recording_height, 0, 8192)
        from v4l2_controls import normalize
        mode, values = normalize(sec.get('camera_controls_mode', 'off'), sec.get('camera_control_values', '{}'))
        cfg.camera_controls_mode = mode
        cfg.camera_control_values = json.dumps(values)
        return cfg

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve deployment sections such as [server] (HTTPS cert/port).
        # The old implementation rewrote edge.ini with only [edge], which
        # could silently erase server/TLS settings after a UI Save.
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8-sig')
        if cp.has_section('edge'):
            cp.remove_section('edge')
        cp['edge'] = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in asdict(self).items()}
        with path.open('w', encoding='utf-8') as f:
            cp.write(f)

    def public(self) -> Dict[str, Any]:
        return asdict(self)

    def update(self, data: Dict[str, Any]) -> None:
        # Central whitelist/normalization so HTTP cannot inject arbitrary INI keys.
        for key in ('backend', 'source', 'capture_mode', 'framerate', 'record_source', 'method', 'inference_device'):
            if key in data:
                setattr(self, key, str(data[key]).strip())
        if 'backend' in data:
            self.backend = self.backend.lower()
            if self.backend not in ('auto', 'qti', 'opencv'):
                self.backend = 'auto'
        if 'record_source' in data:
            self.record_source = self.record_source.lower()
            if self.record_source not in ('raw', 'result'):
                self.record_source = 'raw'
        if 'inference_device' in data:
            self.inference_device = self.inference_device.lower()
            if self.inference_device not in ('cpu', 'cuda'):
                raise ValueError('inference_device must be cpu or cuda')
        if 'camera_controls_mode' in data or 'camera_control_values' in data:
            from v4l2_controls import normalize
            mode, values = normalize(data.get('camera_controls_mode', self.camera_controls_mode),
                                     data.get('camera_control_values', self.camera_control_values))
            self.camera_controls_mode = mode
            self.camera_control_values = json.dumps(values)
        if 'capture_mode' in data:
            self.capture_mode = self.capture_mode.lower()
            if self.capture_mode not in ('v4l2', 'gstreamer'):
                raise ValueError('capture_mode must be v4l2 or gstreamer')
        for key, default, lo, hi in (
            ('camera', self.camera, 0, None), ('width', self.width, 64, 8192),
            ('height', self.height, 64, 8192), ('product_id', self.product_id, 0, None),
            ('preview_quality', self.preview_quality, 30, 100),
            ('preview_max_width', self.preview_max_width, 0, 8192),
            ('snapshot_quality', self.snapshot_quality, 50, 100),
            ('recording_bitrate', self.recording_bitrate, 0, 100_000_000),
            ('recording_width', self.recording_width, 0, 8192),
            ('recording_height', self.recording_height, 0, 8192),
            ('min_free_mb', self.min_free_mb, 0, None),
        ):
            if key in data:
                setattr(self, key, _safe_int(data[key], default, lo, hi))
        if 'rotation' in data:
            self.rotation = 180 if _safe_int(data['rotation'], self.rotation) == 180 else 0
        for key, default, lo, hi in (
            ('infer_fps', self.infer_fps, 0.1, 30.0),
            ('recording_fps', self.recording_fps, 1.0, 120.0),
            ('reconnect_sec', self.reconnect_sec, 0.2, 30.0),
        ):
            if key in data:
                setattr(self, key, _safe_float(data[key], default, lo, hi))
        for key in ('ldc', 'loop_source', 'auto_start'):
            if key in data:
                setattr(self, key, _truthy(data[key]))


class CameraBackend:
    name = 'base'
    hardware_recording = False

    def start(self) -> None:
        raise NotImplementedError

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def start_recording(self, path: Path) -> Tuple[bool, str]:
        return False, 'hardware recording unavailable'

    def stop_recording(self) -> Dict[str, Any]:
        return {'success': False, 'error': 'hardware recording unavailable'}

    def status(self) -> Dict[str, Any]:
        return {'backend': self.name, 'hardware_recording': self.hardware_recording}


class OpenCVCameraBackend(CameraBackend):
    name = 'opencv'

    def __init__(self, cfg: EdgeConfig):
        self.cfg = cfg
        self.cap = None
        self._is_file = False
        self._file_fps = 0.0
        self._next_deadline = 0.0
        self._actual_size = None
        self._capture_mode = ''
        self.camera_controls = None

    def _source(self):
        src = self.cfg.source.strip()
        if src.isdigit():
            return int(src)
        return src

    def start(self) -> None:
        src = self._source()
        is_v4l2 = sys.platform.startswith('linux') and (isinstance(src, int) or
                    isinstance(src, str) and src.startswith(('/dev/video', '/dev/v4l/')))
        self._is_file = isinstance(src, str) and os.path.isfile(src) and not is_v4l2
        self._actual_size = None
        use_gst = is_v4l2 and self.cfg.capture_mode == 'gstreamer'
        if use_gst:
            device = f'/dev/video{src}' if isinstance(src, int) else src
            pipeline = (f'v4l2src device={device} ! '
                        f'image/jpeg,width={self.cfg.width},height={self.cfg.height},'
                        f'framerate={self.cfg.framerate} ! jpegdec ! videoconvert ! '
                        'video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false')
            self.cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            self._capture_mode = 'gstreamer'
        else:
            self.cap = cv2.VideoCapture(src, cv2.CAP_V4L2) if is_v4l2 else cv2.VideoCapture(src)
            self._capture_mode = 'v4l2' if is_v4l2 else 'default'
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = None
            raise RuntimeError(f'OpenCV cannot open source: {src}')
        if not use_gst:
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        if is_v4l2 and not use_gst:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
            try:
                numerator, denominator = self.cfg.framerate.split('/', 1)
                self.cap.set(cv2.CAP_PROP_FPS, float(numerator) / float(denominator))
            except (ValueError, ZeroDivisionError):
                raise ValueError(f'invalid camera framerate: {self.cfg.framerate}')
        elif isinstance(src, int):
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        if is_v4l2:
            from v4l2_controls import apply as apply_v4l2_controls
            try:
                self.camera_controls = apply_v4l2_controls(self.cfg.source,
                    self.cfg.camera_controls_mode, self.cfg.camera_control_values)
            except Exception as exc:
                self.camera_controls = {'mode': self.cfg.camera_controls_mode,
                                        'verified': False, 'errors': {'device': str(exc)}}
        if self._is_file:
            try:
                self._file_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
            except Exception:
                self._file_fps = 0.0
            if not (0.1 <= self._file_fps <= 240.0):
                self._file_fps = 25.0
            self._next_deadline = time.monotonic()

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        del timeout
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if ok and frame is not None:
            self._actual_size = (frame.shape[1], frame.shape[0])
            if self._is_file and self._file_fps > 0:
                now = time.monotonic()
                wait = self._next_deadline - now
                if wait > 0:
                    time.sleep(min(wait, 1.0))
                self._next_deadline = max(self._next_deadline + 1.0 / self._file_fps, time.monotonic())
            return frame
        if self._is_file and self.cfg.loop_source:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._next_deadline = time.monotonic()
            ok, frame = self.cap.read()
            if ok:
                return frame
        return None

    def stop(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out.update({'source': self.cfg.source, 'capture_mode': self._capture_mode,
                    'opened': bool(self.cap is not None and self.cap.isOpened()),
                    'camera_controls': self.camera_controls})
        if self.cap is not None and self.cap.isOpened():
            fourcc = 0
            if self._capture_mode != 'gstreamer':
                try:
                    fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
                except (ValueError, OverflowError):
                    pass
            width, height = self._actual_size or (
                int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            out.update({'width': width, 'height': height,
                        'fps': round(float(self.cap.get(cv2.CAP_PROP_FPS)), 2),
                        'fourcc': 'MJPG' if self._capture_mode == 'gstreamer' else
                                  ''.join(chr((fourcc >> (8 * i)) & 0xff) for i in range(4))})
        return out


class GstH264Mp4Recorder:
    """Small self-contained H264 appsink -> appsrc/mp4mux recorder.

    It intentionally mirrors the stable mechanism used by smartcam_smd_sdk:
    wait for a keyframe, rebase timestamps, push into a separate mux pipeline,
    EOS-finalize that pipeline without stopping the camera.
    """

    def __init__(self, path: Path, caps):
        if not GST_AVAILABLE:
            raise RuntimeError('GStreamer unavailable')
        self.path = Path(path)
        self.caps = caps
        self.pipeline = None
        self.src = None
        self.started = False
        self.base_pts = None
        self.base_dts = None
        self.frames = 0
        self.lock = threading.RLock()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pipeline = Gst.Pipeline.new('edge-mp4-recorder')
        self.src = Gst.ElementFactory.make('appsrc', 'rec_src')
        parser = Gst.ElementFactory.make('h264parse', 'rec_parse')
        mux = Gst.ElementFactory.make('mp4mux', 'rec_mux')
        sink = Gst.ElementFactory.make('filesink', 'rec_sink')
        if None in (self.pipeline, self.src, parser, mux, sink):
            raise RuntimeError('Missing appsrc/h264parse/mp4mux/filesink')
        self.src.set_property('is-live', True)
        self.src.set_property('format', Gst.Format.TIME)
        self.src.set_property('do-timestamp', False)
        if self.caps is not None:
            self.src.set_property('caps', self.caps)
        try:
            parser.set_property('config-interval', -1)
        except Exception:
            pass
        try:
            mux.set_property('faststart', True)
        except Exception:
            pass
        sink.set_property('location', str(self.path))
        sink.set_property('sync', False)
        for el in (self.src, parser, mux, sink):
            self.pipeline.add(el)
        if not self.src.link(parser) or not parser.link(mux) or not mux.link(sink):
            raise RuntimeError('Cannot link MP4 recorder pipeline')
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('MP4 recorder failed to enter PLAYING')
        self.started = True

    def push(self, buf) -> None:
        with self.lock:
            if not self.started or self.src is None or buf is None:
                return
            try:
                # Do not begin a file on a delta frame; wait for an IDR.
                if self.frames == 0 and buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
                    return
                out = buf.copy_deep()
                if self.frames == 0:
                    self.base_pts = out.pts if out.pts != Gst.CLOCK_TIME_NONE else 0
                    self.base_dts = out.dts if out.dts != Gst.CLOCK_TIME_NONE else self.base_pts
                if out.pts != Gst.CLOCK_TIME_NONE:
                    out.pts = max(0, out.pts - int(self.base_pts or 0))
                if out.dts != Gst.CLOCK_TIME_NONE:
                    out.dts = max(0, out.dts - int(self.base_dts or 0))
                flow = self.src.emit('push-buffer', out)
                if flow == Gst.FlowReturn.OK:
                    self.frames += 1
            except Exception:
                # Never let a mux failure destroy the camera streaming thread.
                return

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self.started:
                return {'success': True, 'path': str(self.path), 'frames': self.frames}
            self.started = False
            src, pipeline = self.src, self.pipeline
            self.src = None
            self.pipeline = None
        error = ''
        try:
            if src is not None:
                src.emit('end-of-stream')
            if pipeline is not None:
                bus = pipeline.get_bus()
                msg = bus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
                if msg is not None and msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    error = f'{err}: {dbg}'
                pipeline.set_state(Gst.State.NULL)
                pipeline.get_state(2 * Gst.SECOND)
        except Exception as exc:
            error = str(exc)
        if self.frames == 0:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass
        return {'success': not bool(error), 'error': error, 'path': str(self.path), 'frames': self.frames}


class QtiGstCameraBackend(CameraBackend):
    name = 'qti'
    hardware_recording = True

    def __init__(self, cfg: EdgeConfig):
        if not GST_AVAILABLE:
            raise RuntimeError('PyGObject/GStreamer unavailable')
        self.cfg = cfg
        self.pipeline = None
        self.frame_sink = None
        self.h264_sink = None
        self.encoder = None
        self._frames: 'queue.Queue[np.ndarray]' = queue.Queue(maxsize=1)
        self._recorder: Optional[GstH264Mp4Recorder] = None
        self._record_path = ''
        self._record_lock = threading.RLock()
        self._bus_stop = threading.Event()
        self._bus_thread = None
        self._last_error = ''

    @staticmethod
    def available() -> bool:
        if not GST_AVAILABLE:
            return False
        try:
            Gst.init(None)
            return Gst.ElementFactory.find('qtiqmmfsrc') is not None
        except Exception:
            return False

    @staticmethod
    def _fps_text(v: str) -> str:
        text = str(v or '30/1').strip()
        if '/' not in text:
            try:
                return f'{max(1, int(float(text)))}/1'
            except Exception:
                return '30/1'
        return text

    def _frame_sample(self, sink):
        try:
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            try:
                frame = _decode_jpeg(bytes(info.data))
            finally:
                buf.unmap(info)
            if frame is not None:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    pass
        except Exception as exc:
            self._last_error = f'frame callback: {exc}'
        return Gst.FlowReturn.OK

    def _h264_sample(self, sink):
        try:
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.OK
            rec = self._recorder
            if rec is not None:
                rec.push(sample.get_buffer())
        except Exception as exc:
            self._last_error = f'h264 callback: {exc}'
        return Gst.FlowReturn.OK

    def _bus_loop(self):
        bus = self.pipeline.get_bus() if self.pipeline is not None else None
        while bus is not None and not self._bus_stop.wait(0.1):
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                self._last_error = f'{err}: {dbg}'
                break
            if msg.type == Gst.MessageType.WARNING:
                warn, dbg = msg.parse_warning()
                self._last_error = f'warning: {warn}: {dbg}'
            if msg.type == Gst.MessageType.EOS:
                break

    @staticmethod
    def _make_gst_element(factory: str, name: str):
        elem = Gst.ElementFactory.make(factory, name)
        if elem is None:
            raise RuntimeError(f'GStreamer element not found: {factory}')
        return elem

    @staticmethod
    def _set_if_property(elem, prop: str, value) -> bool:
        try:
            if elem is None or elem.find_property(prop) is None:
                return False
            elem.set_property(prop, value)
            return True
        except Exception:
            return False

    @staticmethod
    def _link_or_raise(src, dst, desc: str) -> None:
        if not src.link(dst):
            raise RuntimeError(f'GStreamer link failed: {desc}')

    @staticmethod
    def _request_preview_pad(camsrc):
        """Request qtiqmmfsrc video_0 and set PREVIEW before linking.

        Qualcomm's newer QIR/QIM camera service is sensitive to the stream
        usecase selected on the request pad.  The PC-era parse_launch form can
        request/link video_0 before Python gets a chance to set pad.type=0,
        allowing the service to select an unsuitable default usecase.  Match
        the known-good LIVE_FLOW_GRAPH_REVIEW/vendor path: request video_0,
        set type=PREVIEW (0), then link it to the capsfilter.
        """
        pad = None
        try:
            pad = camsrc.get_static_pad('video_0')
        except Exception:
            pad = None
        if pad is None and hasattr(camsrc, 'request_pad_simple'):
            try:
                pad = camsrc.request_pad_simple('video_0')
            except Exception:
                pad = None
        if pad is None and hasattr(camsrc, 'get_request_pad'):
            try:
                pad = camsrc.get_request_pad('video_0')
            except Exception:
                pad = None
        if pad is None:
            raise RuntimeError('qtiqmmfsrc video_0 request pad unavailable')
        try:
            if pad.find_property('type') is not None:
                pad.set_property('type', 0)  # PREVIEW
        except Exception as exc:
            raise RuntimeError(f'cannot set qtiqmmfsrc video_0 type=PREVIEW: {exc}') from exc
        return pad

    @staticmethod
    def _startup_bus_error(pipeline) -> str:
        try:
            bus = pipeline.get_bus()
            if bus is None:
                return ''
            msg = bus.timed_pop_filtered(
                250 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.WARNING,
            )
            if msg is None:
                return ''
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                return f'{err}: {dbg}'
            warn, dbg = msg.parse_warning()
            return f'warning: {warn}: {dbg}'
        except Exception:
            return ''

    def start(self) -> None:
        Gst.init(None)
        fps = self._fps_text(self.cfg.framerate)
        pipeline = None
        try:
            # Build programmatically so qtiqmmfsrc.video_0 can be configured as
            # PREVIEW *before* the pad is linked.  This intentionally mirrors the
            # newer QIR camera builder already used by LIVE_FLOW_GRAPH_REVIEW.
            pipeline = Gst.Pipeline.new('visionedge_qti_camera')
            if pipeline is None:
                raise RuntimeError('cannot create GStreamer pipeline')

            camsrc = self._make_gst_element('qtiqmmfsrc', 'camsrc')
            camcaps = self._make_gst_element('capsfilter', 'camcaps')
            tee = self._make_gst_element('tee', 'edge_tee')
            frame_q = self._make_gst_element('queue', 'frame_queue')
            jpegenc = self._make_gst_element('jpegenc', 'jpeg_encoder')
            frame_sink = self._make_gst_element('appsink', 'frame_sink')
            h264_q = self._make_gst_element('queue', 'h264_queue')
            encoder = self._make_gst_element('v4l2h264enc', 'encoder')
            parser = self._make_gst_element('h264parse', 'h264_parser')
            h264_sink = self._make_gst_element('appsink', 'h264_sink')

            self._set_if_property(camsrc, 'camera', int(self.cfg.camera))
            self._set_if_property(camsrc, 'ldc', bool(self.cfg.ldc))
            # Keep only the minimal safe control used by the QIR base path.
            self._set_if_property(camsrc, 'white-balance-mode', 0)
            preview_pad = self._request_preview_pad(camsrc)

            camcaps.set_property(
                'caps',
                Gst.Caps.from_string(
                    f'video/x-raw,format=NV12,width={self.cfg.width},height={self.cfg.height},framerate={fps}'
                ),
            )
            self._set_if_property(tee, 'allow-not-linked', True)

            for q, max_buffers in ((frame_q, 2), (h264_q, 30)):
                self._set_if_property(q, 'max-size-buffers', max_buffers)
                self._set_if_property(q, 'max-size-bytes', 0)
                self._set_if_property(q, 'max-size-time', 0)
                self._set_if_property(q, 'leaky', 2)  # downstream

            self._set_if_property(jpegenc, 'quality', int(self.cfg.preview_quality))
            for sink, max_buffers, drop in ((frame_sink, 1, True), (h264_sink, 16, False)):
                self._set_if_property(sink, 'emit-signals', True)
                self._set_if_property(sink, 'sync', False)
                self._set_if_property(sink, 'max-buffers', max_buffers)
                self._set_if_property(sink, 'drop', drop)

            self._set_if_property(encoder, 'capture-io-mode', 0)
            self._set_if_property(encoder, 'output-io-mode', 0)
            self._set_if_property(parser, 'config-interval', -1)

            controls = Gst.Structure.new_empty('controls')
            controls.set_value('h264_i_frame_period', 30)
            bitrate = int(self.cfg.recording_bitrate or 0)
            if bitrate <= 0:
                bitrate = 16_000_000 if int(self.cfg.width or 0) >= 3840 else 8_000_000
            controls.set_value('video_bitrate', bitrate)
            self._set_if_property(encoder, 'extra-controls', controls)

            elements = [camsrc, camcaps]
            transform = None
            postcaps = None
            if self.cfg.rotation == 180:
                transform = self._make_gst_element('qtivtransform', 'rotate180')
                postcaps = self._make_gst_element('capsfilter', 'post_rotate_caps')
                self._set_if_property(transform, 'rotate', 3)
                postcaps.set_property(
                    'caps',
                    Gst.Caps.from_string(
                        f'video/x-raw,format=NV12,width={self.cfg.width},height={self.cfg.height},framerate={fps}'
                    ),
                )
                elements.extend([transform, postcaps])
            elements.extend([tee, frame_q, jpegenc, frame_sink, h264_q, encoder, parser, h264_sink])
            for elem in elements:
                pipeline.add(elem)

            sink_pad = camcaps.get_static_pad('sink')
            if sink_pad is None:
                raise RuntimeError('camcaps sink pad unavailable')
            link_ret = preview_pad.link(sink_pad)
            if link_ret not in (Gst.PadLinkReturn.OK, Gst.PadLinkReturn.WAS_LINKED):
                raise RuntimeError(f'GStreamer pad link failed: camsrc.video_0 -> camcaps ({link_ret.value_nick})')

            if transform is not None:
                self._link_or_raise(camcaps, transform, 'camcaps -> qtivtransform')
                self._link_or_raise(transform, postcaps, 'qtivtransform -> post caps')
                self._link_or_raise(postcaps, tee, 'post caps -> tee')
            else:
                self._link_or_raise(camcaps, tee, 'camcaps -> tee')

            self._link_or_raise(tee, frame_q, 'tee -> frame queue')
            self._link_or_raise(frame_q, jpegenc, 'frame queue -> jpegenc')
            self._link_or_raise(jpegenc, frame_sink, 'jpegenc -> frame appsink')
            self._link_or_raise(tee, h264_q, 'tee -> h264 queue')
            self._link_or_raise(h264_q, encoder, 'h264 queue -> encoder')
            self._link_or_raise(encoder, parser, 'encoder -> h264parse')
            self._link_or_raise(parser, h264_sink, 'h264parse -> h264 appsink')

            self.pipeline = pipeline
            self.encoder = encoder
            self.frame_sink = frame_sink
            self.h264_sink = h264_sink
            self.frame_sink.connect('new-sample', self._frame_sample)
            self.h264_sink.connect('new-sample', self._h264_sample)
            self._bus_stop.clear()

            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                detail = self._startup_bus_error(pipeline)
                raise RuntimeError('QTI camera pipeline failed to start' + (f': {detail}' if detail else ''))
            ret, state, pending = pipeline.get_state(5 * Gst.SECOND)
            if state != Gst.State.PLAYING:
                detail = self._startup_bus_error(pipeline)
                raise RuntimeError(
                    f'QTI camera did not reach PLAYING ({ret}, state={state}, pending={pending})'
                    + (f': {detail}' if detail else '')
                )

            self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True, name='edge-qti-bus')
            self._bus_thread.start()
        except Exception:
            # A failed QMMF start must be returned to NULL synchronously.  This
            # avoids leaving cam-server with a half-open RecorderClient session
            # before a retry or application restart.
            self._bus_stop.set()
            if pipeline is not None:
                try:
                    pipeline.set_state(Gst.State.NULL)
                    pipeline.get_state(2 * Gst.SECOND)
                except Exception:
                    pass
            self.pipeline = None
            self.frame_sink = None
            self.h264_sink = None
            self.encoder = None
            raise

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        if self._last_error.lower().startswith('gst-resource-error'):
            return None
        try:
            return self._frames.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None

    def start_recording(self, path: Path) -> Tuple[bool, str]:
        with self._record_lock:
            if self._recorder is not None:
                return False, 'recording already active'
            caps = None
            try:
                caps = self.h264_sink.get_static_pad('sink').get_current_caps() if self.h264_sink is not None else None
            except Exception:
                caps = None
            if caps is None:
                return False, 'H.264 encoder caps not ready yet; retry after live view is running'
            rec = GstH264Mp4Recorder(path, caps)
            rec.start()
            self._recorder = rec
            self._record_path = str(path)
            # With a one-second GOP, the recorder begins on the next IDR even if
            # the vendor force-key-unit event is unavailable.
            return True, str(path)

    def stop_recording(self) -> Dict[str, Any]:
        with self._record_lock:
            rec = self._recorder
            self._recorder = None
            self._record_path = ''
        if rec is None:
            return {'success': False, 'error': 'not recording'}
        return rec.stop()

    def stop(self) -> None:
        try:
            self.stop_recording()
        except Exception:
            pass
        self._bus_stop.set()
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline.get_state(2 * Gst.SECOND)
            except Exception:
                pass
            self.pipeline = None
        if self._bus_thread and self._bus_thread.is_alive():
            self._bus_thread.join(timeout=1.0)
        self._bus_thread = None

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out.update({
            'camera': self.cfg.camera,
            'last_error': self._last_error,
            'recording': self._recorder is not None,
            'record_path': self._record_path,
        })
        return out


class SoftwareRecorder:
    def __init__(self, cfg: EdgeConfig):
        self.cfg = cfg
        self.writer = None
        self.path: Optional[Path] = None
        self.frames = 0
        self.started_at = None
        self.mode = 'raw'
        self.size: Optional[Tuple[int, int]] = None
        self.fps: float = 0.0
        self.next_frame_at: Optional[float] = None
        self._queue: Optional[queue.Queue] = None
        self._worker: Optional[threading.Thread] = None
        self.dropped_inputs = 0
        self._error = ''
        self.lock = threading.RLock()

    def start(self, frame: np.ndarray, path: Path, mode: str, fps: Optional[float] = None) -> Tuple[bool, str]:
        with self.lock:
            if self.writer is not None:
                return False, 'recording already active'
            h, w = frame.shape[:2]
            w = min(w, self.cfg.recording_width) if self.cfg.recording_width else w
            h = min(h, self.cfg.recording_height) if self.cfg.recording_height else h
            path.parent.mkdir(parents=True, exist_ok=True)
            target_fps = float(fps or 0.0)
            if not (1.0 <= target_fps <= 120.0):
                target_fps = float(self.cfg.recording_fps)
            target_fps = min(target_fps, float(self.cfg.recording_fps))
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*'mp4v'),
                target_fps, (w, h),
            )
            if not writer.isOpened():
                writer.release()
                path = path.with_suffix('.avi')
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*'MJPG'),
                    target_fps, (w, h),
                )
            if not writer.isOpened():
                writer.release()
                return False, 'OpenCV VideoWriter cannot open mp4v or MJPG encoder'
            self.writer = writer
            self.path = path
            self.frames = 0
            self.started_at = time.time()
            self.mode = mode
            self.size = (w, h)
            self.fps = target_fps
            self.next_frame_at = time.monotonic()
            self.dropped_inputs = 0
            self._error = ''
            self._queue = queue.Queue(maxsize=2)
            self._worker = threading.Thread(
                target=self._encode_loop, args=(self._queue,), daemon=True,
                name='edge-record-encoder',
            )
            self._worker.start()
            return True, str(path)

    def write(self, raw: np.ndarray, result: Optional[np.ndarray],
              captured_at: Optional[float] = None) -> None:
        with self.lock:
            if self.writer is None or self._queue is None:
                return
            frame = result if self.mode == 'result' and result is not None else raw
            item = (time.monotonic() if captured_at is None else captured_at, frame)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # Keep the newest camera frame if encoding temporarily lags.
                try:
                    self._queue.get_nowait()
                    self.dropped_inputs += 1
                except queue.Empty:
                    pass
                self._queue.put_nowait(item)

    def _encode_loop(self, frames: queue.Queue) -> None:
        while True:
            item = frames.get()
            if item is None:
                return
            captured_at, frame = item
            try:
                interval = 1.0 / self.fps
                if captured_at < self.next_frame_at:
                    continue
                due = 1 + int((captured_at - self.next_frame_at) / interval)
                due = min(due, max(1, int(self.fps)))
                if self.size is not None and (frame.shape[1], frame.shape[0]) != self.size:
                    frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                for _ in range(due):
                    self.writer.write(frame)
                self.frames += due
                self.next_frame_at += due * interval
            except Exception as exc:
                self._error = str(exc)

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            writer, path = self.writer, self.path
            if writer is None:
                return {'success': False, 'error': 'not recording'}
            frames_queue, worker = self._queue, self._worker
            self._queue = None
            if frames_queue is not None:
                frames_queue.put(None)
            if worker is not None:
                worker.join(timeout=15.0)
                if worker.is_alive():
                    return {'success': False, 'error': 'recording encoder did not stop'}
            # Keep active/path visible until the container has finalized.
            writer.release()
            frames = self.frames
            started = self.started_at
            dropped_inputs = self.dropped_inputs
            error = self._error
            self.writer = None
            self.path = None
            self.frames = 0
            self.started_at = None
            fps = self.fps
            self.size = None
            self.fps = 0.0
            self.next_frame_at = None
            self._worker = None
        return {
            'success': not bool(error),
            'error': error,
            'path': str(path) if path else '',
            'frames': frames,
            'dropped_inputs': dropped_inputs,
            'duration_sec': round(time.time() - started, 2) if started else 0,
            'fps': round(fps, 2),
        }

    @property
    def active(self) -> bool:
        return self.writer is not None


def serialized_control(fn):
    @functools.wraps(fn)
    def call(self, *args, **kwargs):
        with self.control_lock:
            return fn(self, *args, **kwargs)
    return call


class EdgeRuntime:
    """Single-camera orchestrator. `base` is the imported server.py module."""

    def __init__(self, base_module, config_path: Path = DEFAULT_CONFIG_PATH):
        _ensure_dirs()
        self.control_lock = threading.RLock()
        self.definition_pending = False
        self.active_revision = None
        self.recording_started_at = None
        self.base = base_module
        self.config_path = Path(config_path)
        self.cfg = EdgeConfig.load(self.config_path)
        self.lock = threading.RLock()
        self.frame_cond = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.thread = None
        self._definition_request = None
        self.backend: Optional[CameraBackend] = None
        self.soft_rec = SoftwareRecorder(self.cfg)
        self.cache_mgr = None
        self.matcher = None
        self.engine = None
        self.packaging = None
        self.packaging_templates = {}
        self.product = None
        self.latest_raw: Optional[np.ndarray] = None
        self.latest_result: Optional[np.ndarray] = None
        self.latest_raw_jpeg: bytes = b''
        self.latest_result_jpeg: bytes = b''
        self.frame_seq = 0
        self.infer_seq = 0
        self.started_at = None
        self.last_frame_at = None
        self.last_infer_at = None
        self.last_error = ''
        self.last_warning = ''
        self.status_text = 'STOPPED'
        self.verdict = 'WAIT'
        self.frame_pass = False
        self.results = []
        self.rules = []
        self.sop = None
        self.actual_backend = ''
        self._fps_frames = 0
        self._fps_mark = time.monotonic()
        self.source_fps = 0.0
        self.infer_actual_fps = 0.0
        self._infer_frames = 0
        self._infer_mark = time.monotonic()
        self._last_storage_check = 0.0
        # Media catalogue revision. Bumped whenever a photo/video is created or
        # deleted so the browser can refresh only when the filesystem changes.
        self.media_version = 0

    def _bump_media_version(self) -> int:
        with self.lock:
            self.media_version += 1
            return self.media_version

    def _method(self):
        return getattr(self.base, 'METHOD_MAP_PC', {}).get(
            self.cfg.method, cv2.TM_CCOEFF_NORMED
        )

    def _prepare_inference(self) -> None:
        self._release_inference()
        if int(self.cfg.product_id or 0) <= 0:
            # Camera-only mode is a normal operating mode, not a warning.
            self.last_warning = ''
            return
        product = self.base._load_product_dict(int(self.cfg.product_id))
        if not product:
            self.last_warning = f'product_id={self.cfg.product_id} not found; preview continues'
            return
        mgr = self.base.vc.CacheManager(self.base.DB_PATH, int(self.cfg.product_id), reload_interval=5.0)
        if not mgr.initial_load():
            self.last_warning = 'No valid template/SOP for selected product; preview continues'
            return
        # Edge publishes the complete definition only at an explicit apply/restart.
        self.active_revision = mgr._version
        steps = self.base.vc.load_runtime_inspection_items(self.base.DB_PATH, int(self.cfg.product_id))
        sop_cfg = self.base.vc.load_product_sop_config(self.base.DB_PATH, int(self.cfg.product_id))
        self.cache_mgr = mgr
        self.engine = self.base.vc.SopFlowEngine(steps, sop_cfg) if sop_cfg.get('enabled') and steps else None
        self.product = product
        self.sop = self.engine.summary() if self.engine else None
        self._prepare_packaging(cache=mgr.get())
        if hasattr(self.base.vc, 'TemplateMatcher'):
            self.matcher = self.base.vc.TemplateMatcher(self.cfg.inference_device, self._method())
            cache = mgr.get()
            templates = [reg['tpl_gray'] for reg in cache.regions]
            templates.extend(sample['tpl_gray'] for item in cache.inspection_items
                             for sample in item.get('samples', []))
            templates.extend(reg['tpl_gray'] for reg in self.packaging_templates.values())
            self.matcher.prepare(templates)
        elif self.cfg.inference_device == 'cuda':
            raise RuntimeError('CUDA template matching is unavailable in this inference module')
        if mgr._db_version() != self.active_revision:
            raise RuntimeError('設定正在更新，請重新套用')
        self.definition_pending = False
        cache = mgr.get()
        self.last_warning = '' if cache and (cache.regions or cache.inspection_items) else '尚未建立有效樣板，請到流程設定新增樣板'

    def _prepare_packaging(self, cache):
        import packaging_cycle as pc
        conn = self.base.get_db()
        try:
            cfg = pc.load_settings(conn, self.cfg.product_id)
        finally:
            conn.close()
        if not cfg['enabled']:
            return
        if getattr(self, 'inspection_active', lambda: False)():
            raise RuntimeError('請先結束手動檢測，再啟動自動包裝循環')
        if not self.engine or self.cfg.infer_fps < 2:
            raise RuntimeError('影像包裝循環需要啟用 SOP，且檢測更新率至少每秒 2 次')
        steps = self.engine.steps_cfg
        conn = self.base.get_db()
        try:
            pc.validate_settings(conn, self.cfg.product_id, cfg, steps, True)
        finally:
            conn.close()
        by_id = {x['id']: x for x in cache.regions}
        for role, field in [('vacant','vacant_region_id'),('presence','presence_region_id'),('ready','ready_region_id')]:
            if role == 'ready' and cfg['mode'] == 'fixture':
                continue
            if cfg[field] not in by_id:
                raise RuntimeError('包裝觸發樣板無法讀取，請重新設定')
            reg = copy.deepcopy(by_id[cfg[field]])
            reg['search_margin'] = cfg['margin']
            self.packaging_templates[role] = reg
        for item in cache.inspection_items:
            for sample in item.get('samples', []):
                # Every sample is position-constrained in packaging mode, including NG.
                sample['search_margin'] = cfg['margin']
        journal = pc.CycleJournal(EDGE_RUNTIME_DIR / 'packaging_history.sqlite3')
        journal.recover(self.cfg.product_id)
        pid, revision = self.cfg.product_id, self.active_revision
        def record(cycle_id, status, event, meta, evidence):
            if not self._storage_ok():
                raise RuntimeError('儲存空間不足')
            meta = {**meta, 'product_id': pid, 'revision': revision,
                    'product_name': (self.product or {}).get('serial', ''), 'event': event}
            raw = result = b''
            if evidence is not None:
                source, annotated = evidence[0], evidence[1].copy()
                cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 76), (28, 28, 28), -1)
                cv2.putText(annotated, 'PACKAGING: '+event, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, .6, (240,240,240), 1, cv2.LINE_AA)
                raw, result = (_jpeg(img, self.cfg.snapshot_quality) for img in (source, annotated))
                if not raw or not result:
                    raise RuntimeError('影像證據編碼失敗')
            journal.record(cycle_id, pid, status, event, meta, raw, result)
        self.packaging = pc.PackagingCycle(self.engine, cfg, record)
        self.sop = self.packaging.summary()

    def _release_inference(self) -> None:
        if self.packaging:
            self.packaging.interrupt('相機停止或設定變更，請清空工作區後重新開始')
        self.packaging = None
        self.packaging_templates = {}
        if self.cache_mgr is not None:
            try:
                self.cache_mgr.stop_watcher()
            except Exception:
                pass
        self.cache_mgr = None
        self.matcher = None
        self.engine = None
        self.product = None
        self.sop = None
        self.active_revision = None
        self.definition_pending = False
        self.results = []
        self.rules = []
        self.frame_pass = False

    def _make_backend(self) -> CameraBackend:
        requested = self.cfg.backend.lower()
        qti_available = QtiGstCameraBackend.available()

        # On Qualcomm camera images, the presence of qtiqmmfsrc means QTI is the
        # authoritative camera path.  Falling back to OpenCV after QTI *starts*
        # but cannot acquire the camera is misleading: these devices normally do
        # not expose the sensor as /dev/video0, and the fallback only produces a
        # flood of V4L2 warnings while hiding the real cause (most commonly that
        # another app already owns camera 0).  Auto therefore falls back to
        # OpenCV only when the QTI plugin is not present at all (PC/dev hosts).
        if requested in ('auto', 'qti') and qti_available:
            try:
                b = QtiGstCameraBackend(self.cfg)
                b.start()
                self.actual_backend = 'qti'
                return b
            except Exception as exc:
                self.actual_backend = 'qti'
                hint = (
                    f'QTI camera failed to start: {exc}. '
                    f'Camera {self.cfg.camera} may already be in use by another '
                    'VisionEdge/SmartCam process or service.'
                )
                self.last_warning = ''
                raise RuntimeError(hint) from exc

        if requested == 'qti' and not qti_available:
            raise RuntimeError('QTI backend requested but qtiqmmfsrc is unavailable')

        b = OpenCVCameraBackend(self.cfg)
        b.start()
        self.actual_backend = 'opencv'
        return b

    @serialized_control
    def start(self) -> Dict[str, Any]:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {'success': True, 'already_running': True, 'status': self.status()}
            self.stop_event.clear()
            self.status_text = 'STARTING'
            self.last_error = ''
            self.last_warning = ''
            # Do not present stale frames/FPS from the previous camera session as live.
            self.latest_raw = None
            self.latest_result = None
            self.latest_raw_jpeg = b''
            self.latest_result_jpeg = b''
            self.source_fps = 0.0
            self.infer_actual_fps = 0.0
            self._fps_frames = 0
            self._infer_frames = 0
            self._fps_mark = time.monotonic()
            self._infer_mark = time.monotonic()
            self.last_frame_at = None
            self.last_infer_at = None
            self.verdict = 'WAIT'
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._run, daemon=True, name='edge-camera-runtime')
            self.thread.start()
        return {'success': True}

    @serialized_control
    def stop(self, force=False) -> Dict[str, Any]:
        if not force and self.status()['recording']:
            return {'success': False, 'code': 'recording_active', 'error': '請先停止錄影'}
        self.stop_event.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=8.0)
        with self.lock:
            alive = bool(t and t.is_alive())
            if alive:
                self.status_text = 'STOPPING'
                return {'success': False, 'error': 'runtime thread did not stop within timeout'}
            self.thread = None
            self.status_text = 'STOPPED'
            self.source_fps = 0.0
            self.infer_actual_fps = 0.0
        return {'success': True}

    @serialized_control
    def restart(self) -> Dict[str, Any]:
        result = self.stop()
        if not result.get('success'):
            return result
        return self.start()

    @serialized_control
    def update_config(self, data: Dict[str, Any], restart: bool = False) -> Dict[str, Any]:
        if self.packaging and self.packaging.cycle_id:
            return {'success': False, 'code': 'inspection_active', 'error': '請先完成或中止本箱，再變更設定'}
        if getattr(self, 'inspection_active', lambda: False)():
            return {'success': False, 'code': 'inspection_active', 'error': '請先結束目前這件產品，再變更設定'}
        if self.status()['recording']:
            return {'success': False, 'code': 'recording_active', 'error': '請先停止錄影，再套用設定'}
        was_running = bool(self.thread and self.thread.is_alive())
        if was_running and not restart:
            return {'success': False, 'code': 'restart_required', 'error': '相機執行中，套用設定需要重新啟動相機'}
        candidate = copy.deepcopy(self.cfg)
        try:
            candidate.update(data)
        except ValueError as exc:
            return {'success': False, 'error': str(exc)}
        if was_running:
            stopped = self.stop()
            if not stopped.get('success'):
                return stopped
        try:
            candidate.save(self.config_path)
        except Exception as exc:
            if was_running:
                self.start()
            return {'success': False, 'error': str(exc)}
        with self.lock:
            self.cfg = candidate
            self.soft_rec.cfg = candidate
        if was_running:
            started = self.start()
            if not started.get('success'):
                return started
        return {'success': True, 'config': self.cfg.public(), 'restart_required': False}

    @serialized_control
    def apply_definition(self, product_id):
        if self.packaging and self.packaging.cycle_id:
            return {'success': False, 'code': 'inspection_active', 'error': '請先完成或中止本箱，再套用設定'}
        if getattr(self, 'inspection_active', lambda: False)():
            return {'success': False, 'code': 'inspection_active', 'error': '請先結束目前這件產品，再套用設定'}
        if self.status()['recording']:
            return {'success': False, 'code': 'recording_active', 'error': '請先停止錄影，再套用流程'}
        candidate = copy.deepcopy(self.cfg)
        candidate.product_id = int(product_id or 0)
        with self.lock:
            if not (self.thread and self.thread.is_alive()):
                try:
                    candidate.save(self.config_path)
                except Exception as exc:
                    return {'success': False, 'error': str(exc)}
                self.cfg = candidate
                self.soft_rec.cfg = candidate
                return {'success': True, 'applied': False, 'message': '已儲存；啟動相機時生效'}
            req = {'cfg': candidate, 'done': threading.Event(), 'result': None}
            self._definition_request = req
        # The camera thread swaps definitions between frames, leaving capture open.
        if req['done'].wait(10):
            return req['result']
        with self.lock:
            if req['done'].is_set():
                return req['result']
            if self._definition_request is req:
                self._definition_request = None
        return {'success': False, 'error': '流程已儲存，但執行緒未完成套用；請檢查相機狀態後重試'}

    def _apply_pending_definition(self):
        with self.lock:
            req = self._definition_request
            if req is None:
                return
            self._definition_request = None
            if (self.packaging and self.packaging.cycle_id) or getattr(self, 'inspection_active', lambda: False)():
                req['result'] = {'success': False, 'code': 'inspection_active', 'error': '請先結束目前工件，再套用流程'}
                req['done'].set()
                return
            try:
                req['cfg'].save(self.config_path)
                self.cfg = req['cfg']
                self.soft_rec.cfg = self.cfg
                self._prepare_inference()
                self.latest_result = None
                self.latest_result_jpeg = b''
                self.last_infer_at = None
                self.verdict = 'WAIT'
                req['result'] = {'success': True, 'applied': True,
                                 'active_revision': self.active_revision,
                                 'message': '流程已套用，檢測已重置；相機持續運作'}
            except Exception as exc:
                self._release_inference()
                self.definition_pending = True
                self.last_warning = '流程套用失敗：' + str(exc)
                req['result'] = {'success': False, 'error': self.last_warning}
            finally:
                req['done'].set()

    def _rotate_if_needed(self, frame: np.ndarray) -> np.ndarray:
        # QTI applies rotation in hardware. OpenCV fallback mirrors the same setting.
        if self.actual_backend == 'opencv' and self.cfg.rotation == 180:
            return cv2.rotate(frame, cv2.ROTATE_180)
        return frame

    def _process_inference(self, frame: np.ndarray) -> Tuple[np.ndarray, bool, list, list, Any]:
        if self.cache_mgr is not None and hasattr(self.cache_mgr, '_db_version'):
            if self.cache_mgr._db_version() != self.active_revision:
                if self.packaging and not self.definition_pending:
                    self.packaging.interrupt('設定已變更，本箱已中止；請套用後重新開始')
                self.definition_pending = True
                self.last_warning = '產品設定已更新，請套用後繼續檢測'
                self.sop = None
                return frame, False, [], [], None
        cache = self.cache_mgr.get() if self.cache_mgr is not None else None
        if cache is None or not (cache.regions or cache.inspection_items):
            return frame, False, [], [], None
        prepare = getattr(self.base, '_prepare_edge_frame', self.base._prepare_frame)
        prepared = prepare(frame, self.product or {})
        match_frame = self.matcher.begin(prepared) if self.matcher is not None else None
        kwargs = {'method': self._method(), 'draw_vis': True}
        if match_frame is not None:
            kwargs['match_frame'] = match_frame
        frame_pass, results, vis = self.base.vc.run_inference(prepared, cache, **kwargs)
        rules = getattr(cache, 'last_rule_results', []) or []
        if self.packaging:
            if match_frame is not None:
                signals = {key: bool(match_frame.match(reg).get('pass'))
                           for key, reg in self.packaging_templates.items()}
            else:
                gray = cv2.cvtColor(prepared, cv2.COLOR_BGR2GRAY)
                signals = {key: bool(self.base.vc._match_one_template(gray, *gray.shape, reg, self._method()).get('pass'))
                           for key, reg in self.packaging_templates.items()}
            sop = self.packaging.update(signals, rules, (frame, vis if vis is not None else prepared))
            frame_pass = bool(sop.get('complete'))
            if vis is not None:
                cv2.rectangle(vis, (0, 0), (vis.shape[1], 76), (28, 28, 28), -1)
                label = 'PACKAGING: ' + sop['packaging']['state']
                cv2.putText(vis, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, .65, (240, 240, 240), 1, cv2.LINE_AA)
        else:
            sop = self.engine.update(rules) if self.engine else None
        if vis is None:
            vis = prepared.copy()
        return vis, bool(frame_pass), results, rules, sop

    def _storage_ok(self) -> bool:
        if self.cfg.min_free_mb <= 0:
            return True
        try:
            free = shutil.disk_usage(EDGE_RUNTIME_DIR).free // (1024 * 1024)
            return free >= self.cfg.min_free_mb
        except Exception:
            return False

    def _run(self) -> None:
        backend = None
        try:
            self._prepare_inference()
            backend = self._make_backend()
            with self.lock:
                self.backend = backend
                self.status_text = 'LIVE'
            next_infer = 0.0
            while not self.stop_event.is_set():
                self._apply_pending_definition()
                frame = backend.read(timeout=1.0)
                if frame is None:
                    if self.stop_event.is_set():
                        break
                    self.last_error = 'camera frame timeout / EOF'
                    self.status_text = 'RECONNECTING'
                    # A recording never silently spans a lost/reopened camera session.
                    if self.soft_rec.active:
                        self.soft_rec.stop()
                        self._bump_media_version()
                        self.last_warning = '相機中斷，錄影已停止並儲存'
                    elif backend.status().get('recording'):
                        backend.stop_recording()
                        self._bump_media_version()
                        self.last_warning = '相機中斷，錄影已停止並儲存'
                    if self.packaging:
                        self.packaging.interrupt()
                    if self.engine:
                        try:
                            self.engine.interrupt(reason='SOURCE_INTERRUPTED')
                        except Exception:
                            pass
                    # File/USB OpenCV EOF may recover by reopen; QTI errors also get a clean restart.
                    try:
                        backend.stop()
                    except Exception:
                        pass
                    if self.stop_event.wait(self.cfg.reconnect_sec):
                        break
                    backend = self._make_backend()
                    with self.lock:
                        self.backend = backend
                        self.status_text = 'LIVE'
                        self.last_error = ''
                    continue

                frame = self._rotate_if_needed(frame)
                now = time.monotonic()
                self._fps_frames += 1
                if now - self._fps_mark >= 2.0:
                    self.source_fps = self._fps_frames / max(0.001, now - self._fps_mark)
                    self._fps_frames = 0
                    self._fps_mark = now

                # Raw preview is updated for every received frame.
                raw_jpg = _jpeg(frame, self.cfg.preview_quality, self.cfg.preview_max_width)
                result_frame = self.latest_result
                did_infer = False
                if self.cache_mgr is not None and now >= next_infer:
                    next_infer = now + (1.0 / max(0.1, self.cfg.infer_fps))
                    result_frame, frame_pass, results, rules, sop = self._process_inference(frame)
                    result_jpg = _jpeg(result_frame, self.cfg.preview_quality, self.cfg.preview_max_width)
                    self._infer_frames += 1
                    if now - self._infer_mark >= 2.0:
                        self.infer_actual_fps = self._infer_frames / max(0.001, now - self._infer_mark)
                        self._infer_frames = 0
                        self._infer_mark = now
                    verdict = (
                        'COMPLETE' if sop and sop.get('complete')
                        else 'ALARM' if sop and (sop.get('alarm') or {}).get('active')
                        else 'PASS' if frame_pass else 'RUNNING'
                    )
                    with self.lock:
                        self.latest_result = result_frame
                        self.latest_result_jpeg = result_jpg
                        self.frame_pass = frame_pass
                        self.results = results
                        self.rules = rules
                        self.sop = sop
                        self.verdict = verdict
                        self.infer_seq += 1
                        self.last_infer_at = time.time()
                    did_infer = True
                elif self.cache_mgr is None:
                    result_frame = frame
                    with self.lock:
                        self.latest_result = frame
                        self.latest_result_jpeg = raw_jpg
                        self.verdict = 'PREVIEW'

                if self.definition_pending and self.soft_rec.active and self.soft_rec.mode == 'result':
                    self.soft_rec.stop()
                    self._bump_media_version()
                self.soft_rec.write(frame, result_frame, captured_at=now)
                backend_status = backend.status()
                if (self.soft_rec.active or backend_status.get('recording')) and now - self._last_storage_check >= 2.0:
                    self._last_storage_check = now
                    if not self._storage_ok():
                        stopped = False
                        if self.soft_rec.active:
                            self.soft_rec.stop()
                            stopped = True
                        elif backend_status.get('recording'):
                            backend.stop_recording()
                            stopped = True
                        if stopped:
                            self._bump_media_version()
                        self.last_warning = 'Recording auto-stopped: low/unavailable storage'

                with self.frame_cond:
                    self.latest_raw = frame
                    self.latest_raw_jpeg = raw_jpg
                    self.frame_seq += 1
                    self.last_frame_at = time.time()
                    self.status_text = 'LIVE'
                    self.last_error = ''
                    self.frame_cond.notify_all()
        except Exception as exc:
            with self.lock:
                self.status_text = 'ERROR'
                self.last_error = str(exc)
        finally:
            had_recording = self.soft_rec.active
            if backend is not None:
                try:
                    had_recording = had_recording or bool(backend.status().get('recording'))
                except Exception:
                    pass
            try:
                self.soft_rec.stop()
            except Exception:
                pass
            if backend is not None:
                try:
                    backend.stop()
                except Exception:
                    pass
            if had_recording:
                self._bump_media_version()
            self._release_inference()
            with self.lock:
                self.backend = None
                if self._definition_request is not None:
                    req = self._definition_request
                    self._definition_request = None
                    req['result'] = {'success': False, 'error': self.last_error or '相機已停止，流程尚未套用'}
                    req['done'].set()
                if self.status_text != 'ERROR':
                    self.status_text = 'STOPPED'

    def _storage_status(self) -> Dict[str, Any]:
        try:
            usage = shutil.disk_usage(EDGE_RUNTIME_DIR)
            free_mb = usage.free // (1024 * 1024)
            total_mb = usage.total // (1024 * 1024)
            reserve_mb = int(self.cfg.min_free_mb)
            return {
                'storage_free_mb': int(free_mb),
                'storage_total_mb': int(total_mb),
                'storage_reserve_mb': reserve_mb,
                'storage_ok': bool(reserve_mb <= 0 or free_mb >= reserve_mb),
            }
        except Exception:
            return {
                'storage_free_mb': None,
                'storage_total_mb': None,
                'storage_reserve_mb': int(self.cfg.min_free_mb),
                'storage_ok': False,
            }

    def status(self) -> Dict[str, Any]:
        storage = self._storage_status()
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            backend_status = self.backend.status() if self.backend is not None else {}
            recording = self.soft_rec.active or bool(backend_status.get('recording'))
            fresh = running and self.status_text == 'LIVE' and self.last_frame_at is not None and time.time() - self.last_frame_at < 3
            cache = self.cache_mgr.get() if self.cache_mgr else None
            ready = bool(cache and (getattr(cache, 'regions', []) or getattr(cache, 'inspection_items', []))) and not self.definition_pending
            result_fresh = fresh and self.last_infer_at is not None and time.time() - self.last_infer_at < max(3, 2 / max(.1, self.cfg.infer_fps))
            return {
                'inference_ready': ready and result_fresh,
                'definition_pending': self.definition_pending,
                'active_revision': self.active_revision,
                'recording_started_at': self.recording_started_at if recording else None,
                'frame_fresh': fresh,
                'result_fresh': result_fresh and not self.definition_pending,
                'running': running,
                'status': self.status_text,
                'error': self.last_error,
                'warning': self.last_warning,
                'backend_requested': self.cfg.backend,
                'inference_device': self.cfg.inference_device,
                'inference_device_active': self.matcher.device if self.matcher is not None else None,
                'backend': self.actual_backend or backend_status.get('backend', ''),
                'backend_status': backend_status,
                'frame_seq': self.frame_seq,
                'infer_seq': self.infer_seq,
                'source_fps': round(self.source_fps, 2),
                'infer_fps': round(self.infer_actual_fps, 2),
                'last_frame_at': self.last_frame_at,
                'last_infer_at': self.last_infer_at,
                'uptime_sec': round(time.time() - self.started_at, 1) if running and self.started_at else 0,
                'product': self.product,
                'product_id': self.cfg.product_id,
                'verdict': self.verdict,
                'frame_pass': self.frame_pass,
                'results': self.results,
                'rules': self.rules,
                'sop': self.sop,
                'packaging': (self.sop or {}).get('packaging') if self.packaging else None,
                'recording': recording,
                'record_path': (str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path else str(backend_status.get('record_path') or '')),
                'media_version': self.media_version,
                'has_raw': bool(self.latest_raw_jpeg) and fresh,
                'has_result': bool(self.latest_result_jpeg) and result_fresh and not self.definition_pending,
                **storage,
            }

    def jpeg(self, mode: str = 'result') -> bytes:
        with self.lock:
            if str(mode).lower() == 'raw':
                return bytes(self.latest_raw_jpeg)
            return bytes(self.latest_result_jpeg or self.latest_raw_jpeg)

    def preview_frame(self, mode='raw', after=None):
        """Return one current frame and an opaque revision, never a frame queue."""
        with self.lock:
            now = time.time()
            if (self.stop_event.is_set() or self.status_text != 'LIVE'
                    or self.last_frame_at is None or now - self.last_frame_at >= 3):
                return '', b''
            result = str(mode).lower() != 'raw' and self.cache_mgr is not None
            if result:
                if (self.definition_pending or self.last_infer_at is None
                        or now - self.last_infer_at >= max(3, 2 / max(.1, self.cfg.infer_fps))):
                    return '', b''
                seq, data = self.infer_seq, self.latest_result_jpeg
            else:
                seq, data = self.frame_seq, self.latest_raw_jpeg
            token = f'{self.started_at}:{"result" if result else "raw"}:{seq}'
            return token, b'' if token == after else bytes(data)

    def wait_jpeg(self, after_seq, mode: str = 'result', timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        with self.frame_cond:
            while not self.stop_event.is_set():
                seq, data = self.preview_frame(mode, after_seq)
                if data:
                    return seq, data
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.frame_cond.wait(timeout=remaining)
            return after_seq, b''

    def snapshot(self, mode: str = 'result') -> Dict[str, Any]:
        mode = str(mode or 'result').lower()
        status = self.status()
        if not status['frame_fresh'] or (mode != 'raw' and not status['inference_ready']):
            return {'success': False, 'error': '目前沒有有效畫面，請確認相機與檢測狀態'}
        if not self._storage_ok():
            return {'success': False, 'error': '儲存空間不足'}
        with self.lock:
            if mode == 'raw':
                frame = self.latest_raw.copy() if self.latest_raw is not None else None
            else:
                src = self.latest_result if self.latest_result is not None else self.latest_raw
                frame = src.copy() if src is not None else None
        if frame is None:
            return {'success': False, 'error': 'no frame available'}
        data = _jpeg(frame, self.cfg.snapshot_quality)
        name = f'{mode}_{_timestamp()}.jpg'
        path = EDGE_PHOTO_DIR / name
        path.write_bytes(data)
        version = self._bump_media_version()
        return {'success': True, 'path': f'photos/{name}', 'size': path.stat().st_size, 'media_version': version}

    @serialized_control
    def start_recording(self, mode: Optional[str] = None) -> Dict[str, Any]:
        status = self.status()
        if status['recording']:
            return {'success': False, 'code': 'recording_active', 'error': '錄影已在進行中'}
        if not status['frame_fresh']:
            return {'success': False, 'error': '相機尚未就緒'}
        mode = str(mode or self.cfg.record_source).lower()
        if mode == 'result' and not status['inference_ready']:
            return {'success': False, 'error': '檢測尚未就緒'}
        if mode not in ('raw', 'result'):
            mode = 'raw'
        if not self._storage_ok():
            return {'success': False, 'error': f'free space below min_free_mb={self.cfg.min_free_mb} or unavailable'}
        with self.lock:
            backend = self.backend
            if mode == 'result':
                src = self.latest_result if self.latest_result is not None else self.latest_raw
            else:
                src = self.latest_raw
            frame = src.copy() if src is not None else None
        if backend is None:
            return {'success': False, 'error': 'runtime not running'}
        path = EDGE_RECORD_DIR / f'rec_{_timestamp()}.mp4'
        # QTI encodes raw sensor frames in hardware. Result-overlay recording uses
        # the software path because the overlay is produced after CPU inference.
        if backend.hardware_recording and mode == 'raw':
            ok, msg = backend.start_recording(path)
            if ok:
                self.recording_started_at = time.time()
            version = self._bump_media_version() if ok else self.media_version
            return {'success': ok, 'path': f'recordings/{path.name}' if ok else '', 'mode': mode, 'hardware': True, 'error': '' if ok else msg, 'media_version': version}
        if frame is None:
            return {'success': False, 'error': 'no frame available'}
        # Keep a stable output rate even when the camera is still warming up.
        # SoftwareRecorder paces frames by elapsed time, so a transient source
        # FPS estimate must not determine the MP4 timebase.
        ok, actual = self.soft_rec.start(frame, path, mode)
        if ok:
            self.recording_started_at = time.time()
        actual_path = Path(actual) if ok else None
        version = self._bump_media_version() if ok else self.media_version
        return {
            'success': ok,
            'path': f'recordings/{actual_path.name}' if actual_path else '',
            'mode': mode,
            'hardware': False,
            'error': '' if ok else actual,
            'media_version': version,
        }

    @serialized_control
    def stop_recording(self) -> Dict[str, Any]:
        with self.lock:
            backend = self.backend
        if self.soft_rec.active:
            result = self.soft_rec.stop()
        elif backend is not None and backend.hardware_recording:
            result = backend.stop_recording()
        else:
            result = {'success': False, 'error': 'not recording'}
        if result.get('path'):
            try:
                p = Path(result['path'])
                result['relative_path'] = f'recordings/{p.name}'
            except Exception:
                pass
        if result.get('success'):
            result['media_version'] = self._bump_media_version()
        else:
            result['media_version'] = self.media_version
        return result

    def reset_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.reset(reason='EDGE_API_RESET')
            self.verdict = 'WAIT'
            return self.sop

    def finish_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.finish()
            return self.sop

    def acknowledge_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.acknowledge_alarm()
            return self.sop

    def list_media(self) -> Dict[str, Any]:
        with self.lock:
            backend_status = self.backend.status() if self.backend is not None else {}
            active_path = (
                str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path
                else str(backend_status.get('record_path') or '')
            )
            active_resolved = str(Path(active_path).resolve()) if active_path else ''
            version = self.media_version

        def items(root: Path, prefix: str):
            out = []
            if not root.exists():
                return out
            for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                out.append({
                    'path': f'{prefix}/{p.name}', 'name': p.name,
                    'size': st.st_size, 'mtime': st.st_mtime,
                    'active': bool(active_resolved and str(p.resolve()) == active_resolved),
                    'kind': 'photo' if prefix == 'photos' else 'recording',
                })
            return out
        return {
            'version': version,
            'generated_at': time.time(),
            'photos': items(EDGE_PHOTO_DIR, 'photos'),
            'recordings': items(EDGE_RECORD_DIR, 'recordings'),
        }

    def delete_media(self, rel: str) -> Dict[str, Any]:
        try:
            path = self.resolve_media(rel)
        except ValueError as exc:
            return {'success': False, 'error': str(exc), 'code': 'invalid_path'}

        with self.lock:
            backend_status = self.backend.status() if self.backend is not None else {}
            active = self.soft_rec.active or bool(backend_status.get('recording'))
            active_path = (
                str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path
                else str(backend_status.get('record_path') or '')
            )
        if active and active_path:
            try:
                if path.resolve() == Path(active_path).resolve():
                    return {
                        'success': False,
                        'error': 'cannot delete the file that is currently being recorded',
                        'code': 'recording_active',
                    }
            except Exception:
                pass

        if not path.exists() or not path.is_file():
            return {'success': False, 'error': 'not found', 'code': 'not_found'}
        try:
            name = path.name
            path.unlink()
            # Verify the directory entry is actually gone before reporting success.
            if path.exists():
                return {'success': False, 'error': 'delete verification failed', 'code': 'delete_failed'}
        except OSError as exc:
            return {'success': False, 'error': f'delete failed: {exc}', 'code': 'delete_failed'}
        version = self._bump_media_version()
        return {'success': True, 'deleted': str(rel), 'name': name, 'media_version': version}

    @staticmethod
    def resolve_media(rel: str) -> Path:
        rel = str(rel or '').replace('\\', '/').lstrip('/')
        root = EDGE_RUNTIME_DIR.resolve()
        target = (EDGE_RUNTIME_DIR / rel).resolve()
        if target == root or root not in target.parents:
            raise ValueError('invalid media path')
        if target.parent not in (EDGE_PHOTO_DIR.resolve(), EDGE_RECORD_DIR.resolve()):
            raise ValueError('media path must be directly under photos/ or recordings/')
        return target
