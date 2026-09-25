#!/usr/bin/env python3
"""Static/database tests for Flow Designer changes. Does not require Flask."""
from pathlib import Path
import ast
import sqlite3

ROOT = Path(__file__).resolve().parent


def extract_functions(path, names):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
    return ns


def test_database_save():
    ns = extract_functions(ROOT / 'server.py', {
        '_clean_logic_mode', '_clean_sample_role', '_ensure_rule_group_settings_schema',
        '_get_product_final_logic_mode', '_set_product_final_logic_mode', '_save_inspection_items_data',
    })
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
    CREATE TABLE products(id INTEGER PRIMARY KEY, serial TEXT, name TEXT);
    CREATE TABLE regions(id INTEGER PRIMARY KEY, product_id INTEGER, label TEXT,
      x INTEGER,y INTEGER,w INTEGER,h INTEGER,threshold REAL,search_margin INTEGER,template_b64 TEXT,
      source_width INTEGER DEFAULT 0,source_height INTEGER DEFAULT 0);
    CREATE TABLE inspection_items(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
      name TEXT,logic_mode TEXT,enabled INTEGER,sort_order INTEGER,step_no INTEGER,required INTEGER,
      min_consecutive_hits INTEGER,hold_ms INTEGER,timeout_sec REAL,allow_out_of_order INTEGER,
      latch_when_done INTEGER,alarm_if_missing INTEGER,ui_color TEXT);
    CREATE TABLE inspection_item_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER,
      sample_name TEXT,sample_role TEXT,source_product_id INTEGER,source_region_id INTEGER,
      x INTEGER,y INTEGER,w INTEGER,h INTEGER,threshold REAL,search_margin INTEGER,
      template_b64 TEXT,source_width INTEGER DEFAULT 0,source_height INTEGER DEFAULT 0,
      enabled INTEGER,sort_order INTEGER);
    INSERT INTO products VALUES(1,'P1','Phone');
    INSERT INTO regions(id,product_id,label,x,y,w,h,threshold,search_margin,template_b64,source_width,source_height)
      VALUES(10,1,'App Home',1,2,30,40,.8,5,'AAA',100,80);
    INSERT INTO regions(id,product_id,label,x,y,w,h,threshold,search_margin,template_b64,source_width,source_height)
      VALUES(11,1,'App Pass',5,6,30,40,.85,2,'BBB',100,80);
    INSERT INTO regions(id,product_id,label,x,y,w,h,threshold,search_margin,template_b64,source_width,source_height)
      VALUES(12,1,'App Error',9,10,30,40,.9,1,'CCC',100,80);
    ''')
    payload = {'steps': [
        {'name': 'Open App', 'enabled': True, 'logic_mode': 'ANY', 'samples': [
            {'source_region_id': 10, 'sample_role': 'OK'},
            {'source_region_id': 11, 'sample_role': 'OK'},
            {'source_region_id': 12, 'sample_role': 'NG'},
        ]},
        {'name': 'Finish', 'enabled': True, 'logic_mode': 'ALL', 'samples': [
            {'source_region_id': 10, 'sample_role': 'OK'},
            {'source_region_id': 11, 'sample_role': 'OK'},
        ]},
        {'name': 'Draft', 'enabled': False, 'samples': []},
    ]}
    result = ns['_save_inspection_items_data'](conn, 1, payload)
    conn.commit()
    assert result['count'] == 3 and result['samples'] == 5
    rows = conn.execute('SELECT step_no,sort_order,enabled FROM inspection_items ORDER BY sort_order').fetchall()
    assert [r['step_no'] for r in rows] == [1, 2, 3]
    assert rows[-1]['enabled'] == 0

    # Deleted source Region must fall back to the local sample copy.
    local = conn.execute('SELECT id FROM inspection_item_templates WHERE source_region_id=10 LIMIT 1').fetchone()
    conn.execute('DELETE FROM regions WHERE id=10')
    conn.commit()
    result = ns['_save_inspection_items_data'](conn, 1, {'steps': [{
        'name': 'Preserved', 'enabled': True,
        'samples': [{'source_region_id': 10, 'existing_sample_id': local['id'], 'sample_role': 'OK'}],
    }]})
    conn.commit()
    assert result['samples'] == 1

    try:
        ns['_save_inspection_items_data'](conn, 1, {'steps': [{
            'name': 'NG only', 'enabled': True,
            'samples': [{'source_region_id': 12, 'sample_role': 'NG'}],
        }]})
    except ValueError:
        conn.rollback()
    else:
        raise AssertionError('enabled NG-only Step must be rejected')


def test_ui_contract():
    html = (ROOT / 'static' / 'sop_config.html').read_text(encoding='utf-8')
    for required in (
        'draggable="true"', 'dragStart(event', 'dropStep(event', 'moveStep(',
        '設定多樣板', 'OK ANY / 替代畫面', 'OK ALL / 同幀必要條件',
        '/sop-definition', 'existing_sample_id',
    ):
        assert required in html, required


if __name__ == '__main__':
    test_database_save()
    test_ui_contract()
    print('Flow Designer tests: PASS')
