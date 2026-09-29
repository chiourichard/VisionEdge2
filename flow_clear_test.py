"""Clearing a flow keeps the product's sample library intact."""

import tempfile
from pathlib import Path

import numpy as np

import server as base
import vision_core as vc


def main():
    original_db = base.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            base.DB_PATH = str(Path(temp_dir) / 'inspection.db')
            base.init_db()
            base.init_stream_schema()
            vc.ensure_runtime_revision(base.DB_PATH)
            client = base.app.test_client()
            pid = client.post('/api/products', json={'serial': 'CLEAR-TEST'}).json['id']
            frame = np.random.default_rng(22).integers(0, 255, (80, 120, 3), dtype=np.uint8)
            label = client.post(f'/api/products/{pid}/regions/append', json={
                'label': 'Keep me', 'x': 10, 'y': 10, 'w': 25, 'h': 25,
                'threshold': .8, 'search_margin': 4,
                'source_image_b64': base.cv2_to_b64(frame),
            })
            assert label.status_code == 200, label.json
            rid = label.json['region']['id']
            url = f'/api/products/{pid}/sop-definition'
            created = client.post(url, json={
                'config': {'enabled': True}, 'packaging': {'enabled': False},
                'steps': [{'name': 'First step', 'samples': [
                    {'source_region_id': rid, 'sample_role': 'OK'}]}],
            })
            assert created.status_code == 200, created.json
            cleared = client.post(url, json={
                'config': {'enabled': False}, 'packaging': {'enabled': False},
                'steps': [],
            })
            assert cleared.status_code == 200, cleared.json
            definition = client.get(url).json
            assert definition['steps'] == []
            assert not definition['config']['enabled']
            assert not definition['packaging']['enabled']
            assert client.get(f'/api/products/{pid}/regions').json[0]['id'] == rid
    finally:
        base.DB_PATH = original_db
    print('FLOW CLEAR PASS: empty steps and disabled SOP preserve the Label')


if __name__ == '__main__':
    main()
