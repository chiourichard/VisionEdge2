"""Real FFmpeg regression: playable segments, timing, errors and API guards."""
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from edge_runtime import EdgeConfig
from long_recording import H264Recorder


def wait(fn, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(.03)
    raise AssertionError('timeout')


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                               '-show_format', '-of', 'json', str(path)]))


def main():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install FFmpeg with libx264 and ffprobe'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = EdgeConfig(min_free_mb=0)
        cfg.update({'recording_bitrate': 3_000_000, 'recording_fps': 10,
                    'recording_max_height': 720, 'recording_segment_seconds': 300})
        cfg.save(root / 'edge.ini')
        loaded = EdgeConfig.load(root / 'edge.ini')
        assert (loaded.recording_bitrate, loaded.recording_fps, loaded.recording_max_height,
                loaded.recording_segment_seconds) == (3_000_000, 10, 720, 300)
        cfg.update({'recording_segment_seconds': -1, 'recording_max_height': 99999})
        assert cfg.recording_segment_seconds == 60 and cfg.recording_max_height == 2160
        # Accelerated segmentation solely for integration testing.
        cfg.recording_segment_seconds = 2
        cfg.recording_max_height = 240
        cfg.recording_bitrate = 250_000
        frame = np.zeros((480, 640, 3), np.uint8)
        cv2.putText(frame, 'OK 123', (35, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 255, 0), 3)
        rec = H264Recorder(cfg)
        with patch('long_recording.shutil.which', return_value=None):
            ok, error = rec.start(frame, root / 'missing.mp4', 'result')
            assert not ok and 'FFmpeg' in error and not rec.active
        ok, error = rec.start(frame, root / 'test.mp4', 'result')
        assert ok, error
        try:
            start = time.monotonic()
            while time.monotonic() - start < 5.5:
                rec.write(frame, frame)
                time.sleep(.08)
            assert rec.active, rec.error
            assert rec.status(50_000, 10_240)['recording_remaining_sec'] is not None
            completed = sorted(root.glob('*.mp4'))
            assert len(completed) >= 2, completed
            assert rec.path.suffix == '.part' and rec.owns_active(rec.path)
            for p in completed:
                assert probe(p)['streams'][0]['codec_name'] == 'h264'
            result = rec.stop()
            assert result['success'], result
        finally:
            rec.stop()
        files = sorted(root.glob('*.mp4'))
        assert len(files) >= 3 and len({p.name for p in files}) == len(files)
        assert not list(root.glob('*.part')) and not list(root.glob('*.csv'))
        total = 0
        for p in files:
            info = probe(p)
            stream = info['streams'][0]
            assert (stream['width'], stream['height']) == (320, 240)
            total += float(info['format']['duration'])
            cap = cv2.VideoCapture(str(p))
            ok, image = cap.read(); cap.release()
            assert ok and image[:, :, 1].max() > 150, 'annotated pixels lost'
        assert abs(total - rec.frames / cfg.recording_fps) < .25, (total, rec.frames)
        assert abs(total - (time.time() - rec.started_at)) < 1., 'playback duration differs from wall clock'

        # Failed encoder startup must not claim success or silently use MJPEG.
        real_popen = subprocess.Popen
        def broken_encoder(command, **kwargs):
            command = [v if v != 'libx264' else 'nonexistent_encoder_for_test' for v in command]
            return real_popen(command, **kwargs)
        with patch('long_recording.subprocess.Popen', side_effect=broken_encoder):
            ok, error = rec.start(frame, root / 'bad.mp4', 'result')
            assert not ok and error and not rec.active

        ok, error = rec.start(frame, root / 'lowspace.mp4', 'raw')
        assert ok, error
        with patch('long_recording.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)):
            cfg.min_free_mb = 1
            rec.write(frame, frame)
            wait(lambda: not rec.active)
        assert rec.error and not rec.stop()['success']
        assert not list(root.glob('*.part')), 'low-space stop should finalize playable segments'
        cfg.min_free_mb = 0

        ok, error = rec.start(frame, root / 'stale.mp4', 'result')
        assert ok, error
        wait(lambda: not rec.active)
        assert '3 秒' in rec.error
        assert not list(root.glob('*.part'))
    print('LONG_RECORDING PASS: H.264, segmented decode, wall-clock duration, config, encoder failure, low space, stale input')


if __name__ == '__main__':
    main()
