#!/usr/bin/env python3
"""Regression checks for VisionEdge media lifecycle and TLS files."""
import ssl
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from edge_runtime import EdgeRuntime, EdgeConfig, SoftwareRecorder, EDGE_PHOTO_DIR

BASE = SimpleNamespace(METHOD_MAP_PC={'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED})


def make_video(path: Path):
    w, h, fps = 320, 180, 12.0
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (w, h))
    assert wr.isOpened()
    for i in range(48):
        frame = np.zeros((h, w, 3), np.uint8)
        cv2.putText(frame, str(i), (30, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        wr.write(frame)
    wr.release()


def wait_until(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(.03)
    return False


def main():
    root = Path(__file__).resolve().parent
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(root/'config/certs/CRS0000000616.cert', root/'config/certs/CRS0000000616.key')

    with tempfile.TemporaryDirectory(prefix='visionedge-reg-') as td:
        td = Path(td)
        cfg_file = td/'edge.ini'
        cfg_file.write_text('[server]\nport = 8443\ntls = on\n\n[edge]\nbackend = opencv\n', encoding='utf-8')
        video = td/'source.avi'
        make_video(video)
        rt = EdgeRuntime(BASE, config_path=cfg_file)
        rt.update_config({'backend':'opencv','source':str(video),'rotation':0,'product_id':0,'loop_source':True,'min_free_mb':0,'recording_fps':12})
        saved = cfg_file.read_text(encoding='utf-8')
        assert '[server]' in saved and 'port = 8443' in saved and 'tls = on' in saved, saved
        assert rt.start()['success']
        assert wait_until(lambda: rt.status()['frame_seq'] >= 3)

        snap = rt.snapshot('raw')
        assert snap['success'] and snap['media_version'] >= 1, snap
        snap_path = rt.resolve_media(snap['path'])
        assert snap_path.exists()
        before = rt.list_media()['version']
        deleted = rt.delete_media(snap['path'])
        assert deleted['success'], deleted
        assert not snap_path.exists()
        assert rt.list_media()['version'] > before
        assert all(x['path'] != snap['path'] for x in rt.list_media()['photos'])

        rec = rt.start_recording('raw')
        assert rec['success'], rec
        assert wait_until(lambda: any(x.get('active') for x in rt.list_media()['recordings']))
        blocked = rt.delete_media(rec['path'])
        assert not blocked['success'] and blocked.get('code') == 'recording_active', blocked
        stopped = rt.stop_recording()
        assert stopped['success'], stopped
        rel = stopped.get('relative_path') or rec['path']
        assert rt.delete_media(rel)['success']
        assert all(x['path'] != rel for x in rt.list_media()['recordings'])
        assert rt.stop()['success']

    # Default/runtime regression checks for the edge.5 interaction model.
    defaults = EdgeConfig()
    assert defaults.framerate == '30/1'
    assert defaults.infer_fps == 5.0
    assert defaults.recording_bitrate == 0
    assert defaults.min_free_mb == 10240

    # The recorder honors an explicit FPS override when used directly.
    with tempfile.TemporaryDirectory(prefix='visionedge-rec-fps-') as rd:
        rec = SoftwareRecorder(defaults)
        frame = np.zeros((120, 160, 3), np.uint8)
        ok, out = rec.start(frame, Path(rd)/'fps.mp4', 'result', fps=24.0)
        assert ok, out
        for _ in range(8): rec.write(frame, frame)
        stopped = rec.stop()
        assert stopped['success'] and abs(float(stopped.get('fps', 0)) - 24.0) < 0.01, stopped

    # UI regression checks for the edge.5 interaction model.
    ui = (root/'static/edge_dashboard.html').read_text(encoding='utf-8')
    assert 'View & snapshot' not in ui
    assert 'id="resolution"' in ui
    assert 'value="1920x1080"' in ui and 'value="3840x2160"' in ui
    assert 'id="width"' not in ui and 'id="height"' not in ui
    assert 'id="pageLive"' in ui and 'id="pageMedia"' in ui and 'id="pageSettings"' in ui
    assert 'snapshotCurrent()' in ui
    assert 'id="framerate"' not in ui
    assert '檢測速度' in ui
    assert '標準 · 每秒 5 次（建議）' in ui
    assert '自動（建議）' in ui
    assert '保留 10 GB（建議）' in ui
    assert '影像更新率' in ui and '檢測更新率' in ui
    assert 'Frame Seq</span>' not in ui
    assert '不選產品，僅相機預覽' in ui

    # Clean any old zero-byte test artifacts if the test was interrupted.
    for p in EDGE_PHOTO_DIR.glob('raw_*'):
        if p.is_file() and p.stat().st_size == 0:
            p.unlink()
    print('VISIONEDGE_REGRESSION_TEST PASS')
    print('TLS cert/key: PASS')
    print('Delete + delete verification: PASS')
    print('Active recording delete guard: PASS')
    print('Media revision refresh signal: PASS')
    print('Config save preserves HTTPS [server]: PASS')
    print('UI navigation + operator-safe settings: PASS')
    print('Edge defaults (30 camera / 5 AI / auto bitrate / 10 GB reserve): PASS')
    print('Software recording FPS override: PASS')


if __name__ == '__main__':
    main()
