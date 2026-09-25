#!/usr/bin/env python3
"""Saved 4K-coordinate templates follow a same-aspect camera resolution change."""
from types import SimpleNamespace
from pathlib import Path
import base64
import tempfile

import cv2
import numpy as np

import server as base
from edge_runtime import EdgeRuntime
from template_matching import TemplateMatcher
from vision_core import run_inference


def main():
    source = np.random.default_rng(27).integers(0, 256, (216, 384, 3), dtype=np.uint8)
    target = cv2.resize(source, (192, 108), interpolation=cv2.INTER_AREA)
    x, y, w, h = 120, 60, 80, 60
    template = cv2.cvtColor(source[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
    reg = dict(id=1, label='4K label', x=x, y=y, w=w, h=h,
               th=h, tw=w, threshold=.95, search_margin=20,
               source_width=384, source_height=216, tpl_gray=template)
    matcher = TemplateMatcher('cpu')
    cache = SimpleNamespace(regions=[reg], rule_groups=[], inspection_items=[],
                            last_rule_results=[])
    passed, results, annotated = run_inference(
        target, cache, match_frame=matcher.begin(target))
    result = results[0]
    assert passed and result['match_loc'] == [60, 30], result
    assert result['match_size'] == [40, 30] and result['score'] >= .95, result
    assert annotated.shape[:2] == (108, 192)

    odd = {**reg, 'x': 121, 'w': 79, 'tw': 79,
           'tpl_gray': cv2.cvtColor(source[60:120, 121:200], cv2.COLOR_BGR2GRAY)}
    odd_result = matcher.begin(target).match(odd)
    assert odd_result['w'] == odd_result['match_size'][0], odd_result

    sample = {**reg, 'sample_name': 'OK', 'sample_role': 'OK'}
    item = dict(id=1, name='Scaled step', logic_mode='ANY', samples=[sample])
    cache = SimpleNamespace(regions=[], rule_groups=[], inspection_items=[item],
                            final_logic_mode='ALL', last_rule_results=[])
    passed, results, _ = run_inference(
        target, cache, match_frame=matcher.begin(target), draw_vis=False)
    assert passed and results[0]['match_loc'] == [60, 30], results
    assert results[0]['match_size'] == [40, 30], results

    bad = {**reg, 'source_width': 384, 'source_height': 200}
    mismatch = matcher.begin(target).match(bad)
    assert not mismatch['pass'] and '長寬比' in mismatch['error'], mismatch
    ng_sample = {**bad, 'sample_role': 'NG', 'sample_name': 'Bad aspect NG'}
    ng_only = dict(id=2, name='NG-only step', logic_mode='ANY', samples=[ng_sample])
    ng_cache = SimpleNamespace(regions=[], rule_groups=[], inspection_items=[ng_only],
                               final_logic_mode='ALL', last_rule_results=[])
    ng_passed, ng_results, _ = run_inference(
        target, ng_cache, match_frame=matcher.begin(target), draw_vis=False)
    assert not ng_passed and not ng_results[0]['pass'] and ng_results[0]['error'], ng_results
    unknown = {**reg, 'source_width': 0, 'source_height': 0, 'y': 100}
    unsafe = matcher.begin(target).match(unknown)
    assert not unsafe['pass'] and '原始標註解析度' in unsafe['error'], unsafe

    old_db = base.DB_PATH
    with tempfile.TemporaryDirectory(prefix='resolution-edge-') as directory:
        try:
            base.DB_PATH = str(Path(directory) / 'products.sqlite3')
            base.init_db()
            client = base.app.test_client()
            pid = client.post('/api/products', json={'serial': 'RESOLUTION-TEST'}).json['id']
            ok, encoded = cv2.imencode('.png', source)
            assert ok
            image_b64 = 'data:image/png;base64,' + base64.b64encode(encoded).decode()
            response = client.post(f'/api/products/{pid}/regions/append', json={
                'label': '4K label', 'x': x, 'y': y, 'w': w, 'h': h,
                'threshold': .95, 'search_margin': 20, 'source_image_b64': image_b64,
            })
            assert response.status_code == 200, response.json
            rt = EdgeRuntime(base, Path(directory) / 'edge.ini')
            rt.cfg.product_id = pid
            rt._prepare_inference()
            vis, passed, results, _, _ = rt._process_inference(target)
            assert passed and results[0]['match_loc'] == [60, 30], results
            assert vis.shape[:2] == (108, 192), vis.shape
            rt._release_inference()
        finally:
            base.DB_PATH = old_db
    print('RESOLUTION_MATCHING_TEST PASS')


if __name__ == '__main__':
    main()
