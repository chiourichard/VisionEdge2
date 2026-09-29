"""Product deletion and imported-sample isolation with a real temporary SQLite DB."""

import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import server as base
import vision_core as vc


def main():
    original_db = base.DB_PATH
    original_edge = base.app.extensions.get('visionedge_runtime')
    try:
        with tempfile.TemporaryDirectory() as td:
            base.DB_PATH = str(Path(td) / 'inspection.db')
            base.init_db()
            base.init_stream_schema()
            vc.ensure_runtime_revision(base.DB_PATH)
            client = base.app.test_client()
            old = client.post('/api/products', json={'serial': 'OLD'}).json['id']
            current = client.post('/api/products', json={'serial': 'CURRENT'}).json['id']
            runtime_state = {'running': True}
            edge = SimpleNamespace(cfg=SimpleNamespace(product_id=current),
                                   control_lock=threading.RLock(), packaging=None,
                                   inspection_active=lambda: False,
                                   status=lambda: {'running': runtime_state['running'], 'recording': False})
            def update_config(data):
                edge.cfg.product_id = data['product_id']
                return {'success': True}
            edge.update_config = update_config
            base.app.extensions['visionedge_runtime'] = edge
            frame = np.random.default_rng(17).integers(0, 255, (80, 120, 3), dtype=np.uint8)
            image = base.cv2_to_b64(frame)
            for pid in (old, current):
                response = client.post(
                    f'/api/products/{pid}/regions',
                    json={'image_b64': image, 'regions': [
                        {'label': f'label-{pid}', 'x': 10 * pid, 'y': 12,
                         'w': 20, 'h': 20, 'threshold': 0.8, 'search_margin': 2}
                    ]},
                )
                assert response.status_code == 200, response.json
            old_region = client.get(f'/api/products/{old}/regions').json[0]['id']
            current_region = client.get(f'/api/products/{current}/regions').json[0]['id']
            url = f'/api/products/{current}/sop-definition'
            response = client.post(url, json={'config': {'enabled': False}, 'steps': [
                {'name': 'current inspection', 'samples': [
                    {'source_region_id': old_region, 'sample_role': 'OK'},
                    {'source_region_id': current_region, 'sample_role': 'OK'},
                ]}
            ]})
            assert response.status_code == 200, response.json
            assert client.delete(f'/api/products/{current}').status_code == 409
            db = base.get_db()
            rule_id = db.execute("INSERT INTO inspection_rules(product_id,name) VALUES(?,?)",
                                 (current, 'Legacy reference')).lastrowid
            db.execute('INSERT INTO inspection_rule_items(rule_id,region_id) VALUES(?,?)',
                       (rule_id, old_region))
            db.commit()
            db.close()
            assert client.delete(f'/api/products/{old}').status_code == 409
            db = base.get_db()
            db.execute('DELETE FROM inspection_rules WHERE id=?', (rule_id,))
            db.commit()
            db.close()
            assert client.delete(f'/api/products/{old}').status_code == 200
            assert client.delete(f'/api/products/{old}').status_code == 404

            definition = client.get(url).json
            samples = definition['steps'][0]['samples']
            assert {sample['source_product_id'] for sample in samples} == {old, current}
            # Deleting a source product preserves intentional copies in other products.
            kept = [sample for sample in samples if sample['source_product_id'] == current]
            definition['steps'][0]['samples'] = [
                {'existing_sample_id': sample['id'],
                 'source_region_id': sample['source_region_id'],
                 'sample_name': sample['sample_name'],
                 'sample_role': sample['sample_role']}
                for sample in kept
            ]
            response = client.post(url, json=definition)
            assert response.status_code == 200, response.json
            cache = vc.CacheManager(base.DB_PATH, current)
            assert cache.initial_load()
            assert len(cache.get().inspection_items) == 1
            assert len(cache.get().inspection_items[0]['samples']) == 1
            _, results, _ = vc.run_inference(frame, cache.get(), draw_vis=False)
            assert len(results) == 1
            assert kept[0]['sample_name'] in results[0]['label']
            runtime_state['running'] = False
            assert client.delete(f'/api/products/{current}').status_code == 200
            assert edge.cfg.product_id == 0
            assert client.delete(f'/api/products/{current}').status_code == 404
    finally:
        base.DB_PATH = original_db
        if original_edge is None:
            base.app.extensions.pop('visionedge_runtime', None)
        else:
            base.app.extensions['visionedge_runtime'] = original_edge
    print('PRODUCT SWITCH PASS: running guard, stopped selected delete, imported-copy isolation')


if __name__ == '__main__':
    main()
