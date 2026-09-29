"""Select an isolated recording encoder without changing the system PATH."""
import os
import shutil
from pathlib import Path


def recording_ffmpeg():
    override = os.environ.get('VISIONEDGE_RECORDING_FFMPEG')
    if override:
        # An invalid explicit selection must fail rather than use a different build.
        return override
    local = Path(__file__).resolve().parent / 'tools' / 'recording-ffmpeg' / 'bin' / 'ffmpeg'
    if local.exists():
        return str(local)
    return shutil.which('ffmpeg')
