"""Jetson settings survive recorder migration; no hardware encoder selection."""
import tempfile
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from edge_runtime import EdgeConfig
from long_recording import H264Recorder
import check_recording_env


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        ini = root/'edge.ini'
        ini.write_text('''[server]
port = 8080
tls = off
[edge]
backend = opencv
capture_mode = gstreamer
source = /dev/v4l/by-id/test-camera
inference_device = cuda
preview_max_width = 1280
recording_width = 320
recording_height = 180
recording_fps = 15
min_free_mb = 0
''', encoding='utf-8')
        cfg = EdgeConfig.load(ini)
        assert cfg.recording_max_height == 0 and cfg.recording_segment_seconds == 600
        cfg.update({'recording_bitrate':2500000,'recording_segment_seconds':300})
        cfg.save(ini)
        loaded = EdgeConfig.load(ini)
        assert loaded.inference_device=='cuda' and loaded.capture_mode=='gstreamer'
        assert loaded.preview_max_width==1280 and loaded.source=='/dev/v4l/by-id/test-camera'
        assert loaded.recording_width==320 and loaded.recording_height==180
        assert 'tls = off' in ini.read_text()
        frame=np.zeros((480,640,3),np.uint8)
        rec=H264Recorder(loaded)
        for max_height,expected in [(0,(240,180)),(240,(320,240))]:
            loaded.recording_max_height=max_height
            ok,error=rec.start(frame,root/'test.mp4','raw')
            assert ok,error
            try:
                rec.write(frame,None);time.sleep(.2)
                result=rec.stop();assert result['success'],result
                cap=cv2.VideoCapture(result['path'])
                assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))==expected
                cap.release()
            finally:rec.stop()
        # Even a Linux host with GStreamer must not select QTI/NVENC.
        with patch('long_recording.shutil.which',return_value='/usr/bin/ffmpeg') as which,patch('long_recording.subprocess.Popen',side_effect=OSError('test startup failure')) as spawn:
            ok,error=rec.start(frame,root/'fail.mp4','raw')
            assert not ok and 'test startup failure' in error
            which.assert_called_once_with('ffmpeg')
            command=spawn.call_args.args[0]
            assert 'libx264' in command and 'v4l2h264enc' not in str(command) and 'nvenc' not in str(command)
            assert '-sc_threshold' not in command
            assert '-force_key_frames' in command and '-g' in command
        real_popen = subprocess.Popen
        def compatible_encoder(command, **kwargs):
            assert '-sc_threshold' not in command, 'Unsupported optional FFmpeg argument'
            return real_popen(command, **kwargs)
        with patch('check_recording_env.subprocess.Popen', side_effect=compatible_encoder):
            assert check_recording_env.main() == 0
    print('JETSON_RECORDING_COMPAT PASS: CUDA/BRIO/preview settings, legacy dimensions, aspect ratio, explicit size override, libx264 selection')


if __name__=='__main__':main()
