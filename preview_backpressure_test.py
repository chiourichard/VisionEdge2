"""Synthetic preview revisions: no hardware, image capture, or live service changes."""
import tempfile
import threading
import time
from pathlib import Path
import visionedge_server as web
from edge_runtime import EdgeRuntime


def main():
    original = web.EDGE
    with tempfile.TemporaryDirectory() as td:
        rt = EdgeRuntime(web.base, Path(td) / 'edge.ini')
        web.EDGE = rt
        try:
            rt.status_text = 'LIVE'
            rt.started_at = 1
            rt.last_frame_at = rt.last_infer_at = time.time()
            rt.frame_seq, rt.infer_seq = 10, 2
            rt.cache_mgr = object()  # Only the existence matters to the preview selector.
            rt.latest_raw_jpeg, rt.latest_result_jpeg = b'raw', b'result'
            client = web.app.test_client()
            response = client.get('/api/edge/preview.jpg?mode=result')
            assert response.status_code == 200 and response.data == b'result'
            token = response.headers['X-Frame-Sequence']
            for _ in range(20):
                rt.frame_seq += 1
                response = client.get('/api/edge/preview.jpg', query_string={'mode': 'result', 'after': token})
                assert response.status_code == 204 and not response.data
            assert rt.wait_jpeg(token, 'result', timeout=.01) == (token, b'')
            # Raw-frame notifications must not end a result wait with duplicate bytes.
            observed = []
            worker = threading.Thread(target=lambda: observed.append(rt.wait_jpeg(token, 'result', timeout=1)))
            worker.start()
            with rt.frame_cond:
                rt.frame_seq += 1
                rt.frame_cond.notify_all()
            with rt.frame_cond:
                rt.infer_seq += 1
                rt.latest_result_jpeg = b'new-result'
                rt.frame_cond.notify_all()
            worker.join(2)
            assert not worker.is_alive() and observed[0][1] == b'new-result'
            response = client.get('/api/edge/preview.jpg', query_string={'mode': 'result', 'after': token})
            assert response.data == b'new-result'
            rt.definition_pending = True
            assert client.get('/api/edge/preview.jpg?mode=result').status_code == 204
            assert client.get('/api/edge/preview.jpg?mode=raw').data == b'raw'
            rt.definition_pending = False
            rt.last_infer_at = time.time() - 10
            assert client.get('/api/edge/preview.jpg?mode=result').status_code == 204
            rt.last_frame_at = time.time() - 4
            assert client.get('/api/edge/preview.jpg?mode=raw').status_code == 204
            rt.last_frame_at = rt.last_infer_at = time.time()
            rt.stop_event.set()
            assert client.get('/api/edge/preview.jpg?mode=raw').status_code == 204
            rt.stop_event.clear()
            rt.started_at = 2
            assert client.get('/api/edge/preview.jpg', query_string={'mode': 'result', 'after': token}).status_code == 200
            rt.cache_mgr = None
            assert client.get('/api/edge/preview.jpg?mode=result').data == b'raw'
            stream = client.get('/api/edge/live.mjpg?mode=raw', buffered=False)
            assert b'raw' in next(iter(stream.response))
            stream.close()
        finally:
            rt.cache_mgr = None
            web.EDGE = original
    print('PREVIEW_BACKPRESSURE PASS: revisions, raw notifications, stale/pending/stopped frames, restart, legacy stream')


if __name__ == '__main__':
    main()
