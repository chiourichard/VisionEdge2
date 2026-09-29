#!/usr/bin/env python3
"""Regression tests for Video Label Workspace ↔ Flow/SOP data consistency.

No Flask runtime is required. The test extracts the database helpers directly
from server.py so it can run in the offline camera/host validation environment.
"""
from pathlib import Path
import ast
import base64
import sqlite3
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent


def extract_functions(names):
    tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {'cv2': cv2, 'np': np, 'base64': base64}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'server.py', 'exec'), ns)
    return ns


def make_frame_b64():
    img = np.zeros((120, 160, 3), dtype=np.uint8)
    img[20:80, 30:110] = 180
    ok, buf = cv2.imencode('.png', img)
    assert ok
    return 'data:image/png;base64,' + base64.b64encode(buf).decode()


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
    CREATE TABLE products(id INTEGER PRIMARY KEY, serial TEXT, name TEXT, reference_img_b64 TEXT);
    CREATE TABLE capture_groups(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
      label TEXT, thumb_b64 TEXT, created_at TEXT);
    CREATE TABLE regions(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, label TEXT,
      x INTEGER NOT NULL,y INTEGER NOT NULL,w INTEGER NOT NULL,h INTEGER NOT NULL,threshold REAL,
      search_margin INTEGER,sample_hint TEXT,template_b64 TEXT,source_width INTEGER DEFAULT 0,
      source_height INTEGER DEFAULT 0,capture_group_id INTEGER);
    CREATE TABLE inspection_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
      name TEXT, logic_mode TEXT, enabled INTEGER, sort_order INTEGER);
    CREATE TABLE inspection_rule_items(id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL,
      region_id INTEGER NOT NULL, enabled INTEGER, sort_order INTEGER);
    CREATE TABLE inspection_items(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
      name TEXT,logic_mode TEXT,enabled INTEGER,sort_order INTEGER,step_no INTEGER,required INTEGER,
      min_consecutive_hits INTEGER,hold_ms INTEGER,timeout_sec REAL,allow_out_of_order INTEGER,
      latch_when_done INTEGER,alarm_if_missing INTEGER,ui_color TEXT);
    CREATE TABLE inspection_item_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER,
      sample_name TEXT,sample_role TEXT,source_product_id INTEGER,source_region_id INTEGER,
      x INTEGER,y INTEGER,w INTEGER,h INTEGER,threshold REAL,search_margin INTEGER,
      template_b64 TEXT,source_width INTEGER DEFAULT 0,source_height INTEGER DEFAULT 0,
      enabled INTEGER,sort_order INTEGER);
    INSERT INTO products VALUES(1,'P1','Test Product',NULL);
    INSERT INTO capture_groups(id,product_id,label,thumb_b64) VALUES(1,1,'0:01','A');
    INSERT INTO capture_groups(id,product_id,label,thumb_b64) VALUES(2,1,'0:02','B');
    INSERT INTO capture_groups(id,product_id,label,thumb_b64) VALUES(3,1,'orphan','C');
    INSERT INTO regions(id,product_id,label,x,y,w,h,threshold,search_margin,sample_hint,template_b64,source_width,source_height,capture_group_id)
      VALUES(10,1,'KEEP',10,10,20,20,.80,5,'OK','OLD_KEEP',160,120,1);
    INSERT INTO regions(id,product_id,label,x,y,w,h,threshold,search_margin,sample_hint,template_b64,source_width,source_height,capture_group_id)
      VALUES(11,1,'DELETE',40,40,20,20,.85,5,'OK','OLD_DELETE',160,120,2);
    INSERT INTO inspection_rules(id,product_id,name,logic_mode,enabled,sort_order) VALUES(1,1,'Legacy','ANY',1,0);
    INSERT INTO inspection_rule_items(rule_id,region_id,enabled,sort_order) VALUES(1,10,1,0);
    INSERT INTO inspection_rule_items(rule_id,region_id,enabled,sort_order) VALUES(1,11,1,1);
    ''')
    return conn


def main():
    names = {
        'b64_to_cv2','cv2_to_b64','_clean_sample_hint','_clean_logic_mode','_clean_sample_role',
        '_region_crop_b64','_resolve_capture_group_for_region','_prune_orphan_capture_groups',
        '_sync_product_regions','_append_product_region_data','_ensure_rule_group_settings_schema','_get_product_final_logic_mode','_set_product_final_logic_mode',
        '_save_inspection_items_data',
    }
    ns = extract_functions(names)
    conn = make_db()
    frame = make_frame_b64()

    # 1) Delete one old label. Its Workspace frame and any pre-existing orphan
    # frame must disappear, while the retained template keeps the SAME region ID.
    r1 = ns['_sync_product_regions'](conn, 1, {
        'image_b64': frame,
        'regions': [{
            'id': 10, 'label': 'KEEP', 'x': 10, 'y': 10, 'w': 20, 'h': 20,
            'threshold': .82, 'search_margin': 5, 'sample_hint': 'OK', 'capture_group_id': 1,
        }],
    })
    conn.commit()
    ids = [r['id'] for r in conn.execute('SELECT id FROM regions ORDER BY id')]
    groups = [r['id'] for r in conn.execute('SELECT id FROM capture_groups ORDER BY id')]
    assert ids == [10], ids
    assert groups == [1], groups
    assert r1['stable_ids'] is True and r1['deleted_regions'] == 1
    assert r1['capture_groups_pruned'] == 2, r1
    assert conn.execute('SELECT threshold FROM regions WHERE id=10').fetchone()['threshold'] == .82

    # 2) Create a new label on a new Workspace frame. It receives a new ID while
    # the retained region 10 stays 10.
    r2 = ns['_sync_product_regions'](conn, 1, {
        'image_b64': frame,
        'new_capture_groups': {'cap_new': {'label': '0:03', 'thumb_b64': frame}},
        'regions': [
            {'id': 10, 'label': 'KEEP', 'threshold': .82, 'search_margin': 5,
             'sample_hint': 'OK', 'capture_group_id': 1},
            {'label': 'NEW', 'x': 30, 'y': 20, 'w': 40, 'h': 30, 'threshold': .9,
             'search_margin': 8, 'sample_hint': 'OK', 'capture_group_temp_key': 'cap_new',
             'source_image_b64': frame},
        ],
    })
    conn.commit()
    rows = conn.execute('SELECT id,label,capture_group_id FROM regions ORDER BY id').fetchall()
    assert rows[0]['id'] == 10 and rows[0]['label'] == 'KEEP'
    new_row = next(r for r in rows if r['label'] == 'NEW')
    new_id = new_row['id']
    assert new_id != 10 and new_row['capture_group_id'] is not None
    assert tuple(conn.execute('SELECT source_width,source_height FROM regions WHERE id=?',
                              (new_id,)).fetchone()) == (160, 120)

    # 3) Save metadata again: IDs must not churn. This is the core Flow/SOP fix.
    before = {r['label']: r['id'] for r in rows}
    ns['_sync_product_regions'](conn, 1, {
        'image_b64': frame,
        'regions': [dict(r) for r in conn.execute(
            'SELECT id,label,x,y,w,h,threshold,search_margin,sample_hint,capture_group_id FROM regions ORDER BY id'
        ).fetchall()],
    })
    conn.commit()
    after = {r['label']: r['id'] for r in conn.execute('SELECT id,label FROM regions')}
    assert after == before, (before, after)

    # 4) The freshly created label must be saveable into SOP immediately.
    result = ns['_save_inspection_items_data'](conn, 1, {'steps': [{
        'name': 'New Step', 'enabled': True, 'samples': [
            {'source_region_id': new_id, 'sample_role': 'OK', 'sample_name': 'NEW'}
        ],
    }]})
    conn.commit()
    assert result['count'] == 1 and result['samples'] == 1, result
    saved_sample = conn.execute('SELECT source_region_id,source_width,source_height FROM inspection_item_templates').fetchone()
    saved_source = saved_sample['source_region_id']
    assert saved_source == new_id
    assert (saved_sample['source_width'], saved_sample['source_height']) == (160, 120)

    # 5) Additive scratch-label save must preserve every existing template.
    before_ids = [r['id'] for r in conn.execute('SELECT id FROM regions ORDER BY id')]
    added = ns['_append_product_region_data'](conn, 1, {
        'label': 'SCRATCH', 'x': 5, 'y': 5, 'w': 20, 'h': 20, 'threshold': .8,
        'search_margin': 0, 'sample_hint': 'OK', 'source_image_b64': frame,
    })
    conn.commit()
    after_ids = [r['id'] for r in conn.execute('SELECT id FROM regions ORDER BY id')]
    assert set(before_ids).issubset(set(after_ids)), (before_ids, after_ids)
    assert added['id'] in after_ids and len(after_ids) == len(before_ids) + 1

    # 6) Flow Designer's scratch-label path must use the additive endpoint, never
    # one-element bulk /regions replacement.
    flow = (ROOT / 'static' / 'flow_studio.html').read_text(encoding='utf-8')
    assert '/regions/append' in flow
    assert "body: JSON.stringify({ regions: [{" not in flow

    video = (ROOT / 'static' / 'video_label.html').read_text(encoding='utf-8')
    assert 'function removeCapture(id, ev)' in video

    print('Label/Flow consistency regression: PASS')


if __name__ == '__main__':
    main()
