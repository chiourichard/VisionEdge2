"""Private selection works independently of the service working directory/PATH."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import recording_ffmpeg as module


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    with patch.object(module, '__file__', str(root / 'recording_ffmpeg.py')), \
            patch.dict(os.environ, {}, clear=True), \
            patch.object(module.shutil, 'which', return_value='/usr/bin/ffmpeg') as lookup:
        assert module.recording_ffmpeg() == '/usr/bin/ffmpeg'
        local = root / 'tools/recording-ffmpeg/bin/ffmpeg'
        local.parent.mkdir(parents=True)
        local.touch()
        lookup.reset_mock()
        assert module.recording_ffmpeg() == str(local)
        lookup.assert_not_called()
        with patch.dict(os.environ, {'VISIONEDGE_RECORDING_FFMPEG': '/invalid/explicit/ffmpeg'}):
            assert module.recording_ffmpeg() == '/invalid/explicit/ffmpeg'
        assert module.recording_ffmpeg() == str(local)
print('RECORDING_FFMPEG PASS: PATH fallback, private priority, explicit selection, no silent fallback')
