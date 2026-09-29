"""Annotated recording against the real runtime and HTTP routes; no camera."""
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import edge_runtime as er
import visionedge_server as web
from edge_smoke_test import BASE_INFER_STUB, make_video


def wait(fn, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(.05)
    raise AssertionError('timeout')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.multiple(er, EDGE_RUNTIME_DIR=root, EDGE_PHOTO_DIR=root/'photos',
                            EDGE_RECORD_DIR=root/'recordings', EDGE_LOG_DIR=root/'logs'):
            source = root/'source.avi'; make_video(source)
            rt = er.EdgeRuntime(BASE_INFER_STUB, root/'edge.ini')
            with patch.object(web, 'EDGE', rt):
                client = web.app.test_client()
                try:
                    rt.update_config({'backend':'opencv','source':str(source),'rotation':0,
                                      'product_id':1,'infer_fps':5,'recording_fps':10,'min_free_mb':0})
                    rt.cfg.recording_segment_seconds = 2  # accelerate only in test
                    assert rt.start()['success']
                    wait(lambda: rt.status()['inference_ready'])
                    response = client.post('/api/edge/record/start',json={'mode':'result'})
                    assert response.json['success'], response.json
                    initial = response.json['path']
                    assert client.get('/api/edge/media/'+initial).status_code == 409
                    assert client.post('/api/edge/media/delete',json={'path':initial}).status_code == 409
                    assert client.put('/api/edge/config',json={'recording_fps':5}).status_code == 409
                    wait(lambda: len(rt.soft_rec.completed) >= 1)
                    items = client.get('/api/edge/media').json['recordings']
                    completed = next(i for i in items if i['path'].endswith('.mp4'))
                    pending = next(i for i in items if i['path'].endswith('.part'))
                    assert not completed['active'] and pending['active']
                    assert client.get('/api/edge/media/'+completed['path']).status_code == 200
                    assert client.get('/api/edge/media/'+pending['path']).status_code == 409
                    before_bytes = rt.soft_rec.total_bytes
                    assert client.post('/api/edge/media/delete',json={'path':completed['path']}).json['success']
                    time.sleep(.4)
                    assert rt.soft_rec.total_bytes >= before_bytes
                    status = client.get('/api/edge/status').json
                    assert status['recording'] and status['recording_encoder'] == 'libx264'
                    result = client.post('/api/edge/record/stop').json
                    assert result['success'],result
                    assert client.get('/api/edge/media/'+result['relative_path']).status_code == 200
                    assert not any(i['active'] for i in rt.list_media()['recordings'])

                    # An unexpected process death must surface, unlock controls,
                    # update media revision, and preserve incomplete files safely.
                    assert client.post('/api/edge/record/start',json={'mode':'result'}).json['success']
                    version = rt.media_version
                    rt.soft_rec.process.kill()
                    wait(lambda:not rt.soft_rec.active)
                    wait(lambda:rt.media_version > version)
                    assert rt.status()['recording_error']
                    for i in rt.list_media()['recordings']:
                        if i['incomplete']:
                            assert not i['active']
                            assert client.get('/api/edge/media/'+i['path']).status_code == 409
                            assert client.post('/api/edge/media/delete',json={'path':i['path']}).json['success']
                finally:
                    rt.soft_rec.stop()
                    rt.stop(force=True)
    print('LONG_RECORDING_API PASS: annotated runtime, download/delete guards, segmentation, encoder death, media revisions')


if __name__ == '__main__':
    main()
