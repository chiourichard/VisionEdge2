#!/usr/bin/env python3
"""
Template Matching Inspection App
Backend: Flask + SQLite + OpenCV
"""

import os
import argparse
import json
import sqlite3
import base64
import uuid
import time as _time
import threading
import traceback
import atexit
from datetime import datetime
import cv2
import numpy as np
from flask import Flask, request, jsonify, send_from_directory, send_file, Response, stream_with_context, redirect
from flask_cors import CORS

import vision_core as vc

app = Flask(__name__, static_folder='static')
# Same-origin is the safe default. Set TM_CORS_ORIGINS to a comma-separated allowlist
# only when a separate trusted frontend host must call this server.
_cors_origins = [x.strip() for x in os.environ.get('TM_CORS_ORIGINS', '').split(',') if x.strip()]
if _cors_origins:
    CORS(app, resources={r'/api/*': {'origins': _cors_origins}})
# Bound upload size; override for larger production videos when storage is sized for it.
app.config['MAX_CONTENT_LENGTH'] = int(float(os.environ.get('TM_MAX_UPLOAD_GB', '4')) * 1024 * 1024 * 1024)

DB_PATH    = os.path.join(os.path.dirname(__file__), 'db', 'inspection.db')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

VIDEO_UPLOAD_TTL_SEC = int(os.environ.get('TM_VIDEO_UPLOAD_TTL_SEC', str(24 * 3600)))
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.ts'}


def _safe_positive_float(value, default):
    try:
        v = float(value)
        return v if np.isfinite(v) and v > 0 else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _safe_nonnegative_int(value, default=0):
    try:
        v = float(value)
        return max(0, int(v)) if np.isfinite(v) else int(default)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _cleanup_stale_video_uploads():
    cutoff = _time.time() - max(3600, VIDEO_UPLOAD_TTL_SEC)
    for name in os.listdir(UPLOAD_DIR):
        if not name.startswith('infer_video_'):
            continue
        path = os.path.join(UPLOAD_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except Exception:
            pass

_cleanup_stale_video_uploads()

# Log rotate: keep recent N days (0=off, override with env LOG_RETAIN_DAYS)
LOG_RETAIN_DAYS = int(os.environ.get('LOG_RETAIN_DAYS', '7'))

# Monitor connection config: persist the two URLs shown on monitor.html
MONITOR_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'monitor_config.json')
DEFAULT_MONITOR_CONFIG = {
    # Firmware nginx mounts TM-Inspect under /tm-app and proxies the inference
    # service under /tm-app/infer, keeping browser traffic on HTTPS port 443.
    # Override with TM_INSPECT_INFER_URL / TM_INSPECT_APP_URL for standalone use.
    'infer_url': os.environ.get('TM_INSPECT_INFER_URL', ''),
    'app_url':   os.environ.get('TM_INSPECT_APP_URL',   ''),
}



# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn



def rotate_logs(conn=None, retain_days=None):
    """Delete inference_logs older than retain_days. Returns deleted count."""
    if retain_days is None:
        retain_days = LOG_RETAIN_DAYS
    if retain_days <= 0:
        return 0
    close_after = conn is None
    if conn is None:
        conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM inference_logs WHERE created_at < datetime('now','localtime',? || ' days')",
            (f'-{retain_days}',)
        )
        deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('VACUUM')
            conn.commit()
        return deleted
    finally:
        if close_after:
            conn.close()

def init_db():
    conn = get_db()
    import packaging_cycle as pc
    pc.ensure_schema(conn)
    # WAL reduces reader/writer contention when several stream workers reload templates.
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            serial           TEXT NOT NULL UNIQUE,
            name             TEXT,
            reference_img_b64 TEXT,
            created_at       TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')
    # Migration: add column if it doesn't exist (for existing DBs)
    try:
        c.execute('ALTER TABLE products ADD COLUMN reference_img_b64 TEXT')
        conn.commit()
    except Exception:
        pass  # column already exists

    c.execute('''
        CREATE TABLE IF NOT EXISTS regions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id    INTEGER NOT NULL,
            label         TEXT,
            x             INTEGER NOT NULL,
            y             INTEGER NOT NULL,
            w             INTEGER NOT NULL,
            h             INTEGER NOT NULL,
            threshold     REAL DEFAULT 0.8,
            search_margin INTEGER DEFAULT 0,
            sample_hint   TEXT NOT NULL DEFAULT 'OK',
            template_b64  TEXT,
            source_width  INTEGER NOT NULL DEFAULT 0,
            source_height INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')
    # Migration: add search_margin / sample_hint if upgrading from older DB
    try:
        c.execute('ALTER TABLE regions ADD COLUMN search_margin INTEGER DEFAULT 0')
        conn.commit()
    except Exception:
        pass  # column already exists
    # Template library hint: OK / NG / NEUTRAL candidate. Runtime final role is still stored
    # in inspection_item_templates.sample_role, not here.
    try:
        c.execute("ALTER TABLE regions ADD COLUMN sample_hint TEXT NOT NULL DEFAULT 'OK'")
        conn.commit()
    except Exception:
        pass  # column already exists
    try:
        c.execute('ALTER TABLE regions ADD COLUMN template_b64 TEXT')
        conn.commit()
    except Exception:
        pass  # column already exists
    for col in ('source_width', 'source_height'):
        if col not in {row['name'] for row in c.execute('PRAGMA table_info(regions)')}:
            c.execute(f'ALTER TABLE regions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0')
    try:
        c.execute("UPDATE regions SET sample_hint='OK' WHERE sample_hint IS NULL OR sample_hint=''")
        conn.commit()
    except Exception:
        pass
    # Capture groups: persists "which source frame was this region drawn on" across
    # save/reload, so browsing a product's regions later can still tell apart boxes
    # that came from genuinely different frames/moments instead of showing every
    # region ever saved overlaid on one static reference image. Nullable FK on
    # regions — existing regions saved before this feature stay ungrouped and keep
    # behaving exactly as before (shown together, no group filter applied to them).
    c.execute('''
        CREATE TABLE IF NOT EXISTS capture_groups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            label      TEXT,
            thumb_b64  TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')
    try:
        c.execute('ALTER TABLE regions ADD COLUMN capture_group_id INTEGER REFERENCES capture_groups(id)')
        conn.commit()
    except Exception:
        pass  # column already exists
    c.execute('''
        CREATE TABLE IF NOT EXISTS inference_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id        INTEGER,
            result_json       TEXT,
            pass_fail         TEXT,
            raw_image_path    TEXT,
            result_image_path TEXT,
            storage_status    TEXT DEFAULT 'NONE',
            created_at        TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')
    # Migration: historical-image archive columns for existing DBs
    for col_def in [
        'raw_image_path TEXT',
        'result_image_path TEXT',
        "storage_status TEXT DEFAULT 'NONE'",
    ]:
        try:
            c.execute(f'ALTER TABLE inference_logs ADD COLUMN {col_def}')
            conn.commit()
        except Exception:
            pass

    # Rule Group v1: let existing templates be grouped by ANY/ALL logic.
    c.execute('''
        CREATE TABLE IF NOT EXISTS inspection_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            logic_mode  TEXT NOT NULL DEFAULT 'ANY',
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS inspection_rule_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     INTEGER NOT NULL,
            region_id   INTEGER NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (rule_id) REFERENCES inspection_rules(id) ON DELETE CASCADE,
            FOREIGN KEY (region_id) REFERENCES regions(id) ON DELETE CASCADE
        )
    ''')


    # Multi-Sample Template Formal V2:
    # A product owns inspection items. Each item can contain multiple sample templates,
    # copied from any existing region/template. Runtime uses these local copies so later
    # edits/deletes in source products do not silently change PASS/FAIL behavior.
    c.execute('''
        CREATE TABLE IF NOT EXISTS inspection_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            logic_mode  TEXT NOT NULL DEFAULT 'ANY',
            enabled     INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS inspection_item_templates (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id           INTEGER NOT NULL,
            sample_name       TEXT NOT NULL,
            sample_role       TEXT NOT NULL DEFAULT 'OK',
            source_product_id INTEGER,
            source_region_id  INTEGER,
            x                 INTEGER NOT NULL,
            y                 INTEGER NOT NULL,
            w                 INTEGER NOT NULL,
            h                 INTEGER NOT NULL,
            threshold         REAL DEFAULT 0.8,
            search_margin     INTEGER DEFAULT 0,
            template_b64      TEXT,
            source_width      INTEGER NOT NULL DEFAULT 0,
            source_height     INTEGER NOT NULL DEFAULT 0,
            enabled           INTEGER NOT NULL DEFAULT 1,
            sort_order        INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (item_id) REFERENCES inspection_items(id) ON DELETE CASCADE
        )
    ''')
    # Migration: OK / NG sample role for Rule Groups B+.
    try:
        c.execute("ALTER TABLE inspection_item_templates ADD COLUMN sample_role TEXT NOT NULL DEFAULT 'OK'")
        conn.commit()
    except Exception:
        pass  # column already exists
    try:
        c.execute("UPDATE inspection_item_templates SET sample_role='OK' WHERE sample_role IS NULL OR sample_role=''")
        conn.commit()
    except Exception:
        pass
    for col in ('source_width', 'source_height'):
        if col not in {row['name'] for row in c.execute('PRAGMA table_info(inspection_item_templates)')}:
            c.execute(f'ALTER TABLE inspection_item_templates ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0')

    # SOP Flow v1: reuse inspection_items as ordered, time-latched SOP steps.
    # Existing rule-group behavior remains compatible because every added column has a safe default.
    for col_def in [
        'step_no INTEGER NOT NULL DEFAULT 0',
        'required INTEGER NOT NULL DEFAULT 1',
        'min_consecutive_hits INTEGER NOT NULL DEFAULT 2',
        'hold_ms INTEGER NOT NULL DEFAULT 400',
        'timeout_sec REAL NOT NULL DEFAULT 0',
        'allow_out_of_order INTEGER NOT NULL DEFAULT 0',
        'latch_when_done INTEGER NOT NULL DEFAULT 1',
        'alarm_if_missing INTEGER NOT NULL DEFAULT 1',
        "ui_color TEXT DEFAULT ''",
    ]:
        try:
            c.execute(f'ALTER TABLE inspection_items ADD COLUMN {col_def}')
            conn.commit()
        except Exception:
            pass

    c.execute('''
        CREATE TABLE IF NOT EXISTS product_sop_config (
            product_id              INTEGER PRIMARY KEY,
            enabled                 INTEGER NOT NULL DEFAULT 0,
            strict_order            INTEGER NOT NULL DEFAULT 1,
            completion_mode         TEXT NOT NULL DEFAULT 'ALL_REQUIRED',
            auto_reset_mode         TEXT NOT NULL DEFAULT 'MANUAL',
            idle_reset_sec          REAL NOT NULL DEFAULT 0,
            alarm_on_skip           INTEGER NOT NULL DEFAULT 1,
            alarm_on_timeout        INTEGER NOT NULL DEFAULT 1,
            alarm_latch             INTEGER NOT NULL DEFAULT 1,
            web_alarm_enabled       INTEGER NOT NULL DEFAULT 1,
            tower_light_enabled     INTEGER NOT NULL DEFAULT 0,
            tower_green_channel     INTEGER NOT NULL DEFAULT 1,
            tower_yellow_channel    INTEGER NOT NULL DEFAULT 2,
            tower_red_channel       INTEGER NOT NULL DEFAULT 3,
            station_name            TEXT DEFAULT '',
            updated_at              TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sop_run_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id      INTEGER NOT NULL,
            session_id      TEXT NOT NULL,
            result          TEXT NOT NULL,
            alarm_code      TEXT DEFAULT '',
            alarm_message   TEXT DEFAULT '',
            step_state_json TEXT,
            started_at      TEXT,
            ended_at        TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    ''')

    conn.commit()
    conn.close()

    # Rotate old logs on startup
    if LOG_RETAIN_DAYS > 0:
        deleted = rotate_logs()
        if deleted > 0:
            import logging as _log
            _log.getLogger('app').info(f'[Log Rotate] Startup: deleted {deleted} records older than {LOG_RETAIN_DAYS} days')


init_db()
vc.ensure_runtime_revision(DB_PATH)
# ──────────────────────────────────────────────
# PC / Server build: multi-stream source schema
# ──────────────────────────────────────────────
def init_stream_schema():
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS stream_sources (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                source_uri     TEXT NOT NULL,
                source_type    TEXT NOT NULL DEFAULT 'RTSP',
                product_id     INTEGER NOT NULL,
                enabled        INTEGER NOT NULL DEFAULT 1,
                infer_fps      REAL NOT NULL DEFAULT 2.0,
                reconnect_sec  REAL NOT NULL DEFAULT 3.0,
                loop_video     INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT DEFAULT (datetime('now','localtime')),
                updated_at     TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
    finally:
        conn.close()

init_stream_schema()


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────
def b64_to_cv2(b64str):
    if ',' in b64str:
        b64str = b64str.split(',', 1)[1]
    img_bytes = base64.b64decode(b64str)
    arr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def cv2_to_b64(img):
    _, buf = cv2.imencode('.png', img)
    return 'data:image/png;base64,' + base64.b64encode(buf).decode()



# ──────────────────────────────────────────────
# Shared vision transform config (label / monitor / inference)
# ──────────────────────────────────────────────
VISION_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'vision_config.json')
VISION_CONFIG_DEFAULT = {
    'digital_zoom_enabled': False,
    'digital_zoom': 1.0,
    'zoom_center_x': 0.5,
    'zoom_center_y': 0.5,
}

def _clamp_float(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return default

def normalize_vision_config(data=None):
    data = data or {}
    return {
        'digital_zoom_enabled': bool(data.get('digital_zoom_enabled', VISION_CONFIG_DEFAULT['digital_zoom_enabled'])),
        'digital_zoom': round(_clamp_float(data.get('digital_zoom', VISION_CONFIG_DEFAULT['digital_zoom']), 1.0, 4.0, 1.0), 2),
        'zoom_center_x': round(_clamp_float(data.get('zoom_center_x', VISION_CONFIG_DEFAULT['zoom_center_x']), 0.05, 0.95, 0.5), 3),
        'zoom_center_y': round(_clamp_float(data.get('zoom_center_y', VISION_CONFIG_DEFAULT['zoom_center_y']), 0.05, 0.95, 0.5), 3),
    }

def load_vision_config():
    try:
        if os.path.exists(VISION_CONFIG_PATH):
            with open(VISION_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return normalize_vision_config(json.load(f) or {})
    except Exception as e:
        print(f'[Vision Config] read failed: {e}')
    return dict(VISION_CONFIG_DEFAULT)

def save_vision_config(data):
    cfg = normalize_vision_config(data)
    tmp = VISION_CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VISION_CONFIG_PATH)
    return cfg

# ──────────────────────────────────────────────
# Monitor connection config helpers
# ──────────────────────────────────────────────
def _clean_url(v, default):
    v = (v or '').strip().rstrip('/')
    if not v:
        return default
    # Firmware nginx uses same-origin relative endpoints such as /tm-app/infer.
    # Standalone deployments may still use explicit http(s) URLs.
    if v.startswith('/'):
        return v
    if not (v.startswith('http://') or v.startswith('https://')):
        raise ValueError('URL 必須以 /、http:// 或 https:// 開頭')
    return v


def load_monitor_config():
    cfg = dict(DEFAULT_MONITOR_CONFIG)
    try:
        if os.path.exists(MONITOR_CONFIG_PATH):
            with open(MONITOR_CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            cfg['infer_url'] = _clean_url(data.get('infer_url'), cfg['infer_url'])
            cfg['app_url']   = _clean_url(data.get('app_url'),   cfg['app_url'])
    except Exception as e:
        print(f'[Monitor Config] read failed: {e}')
    return cfg


def save_monitor_config(cfg):
    clean = {
        'infer_url': _clean_url(cfg.get('infer_url'), DEFAULT_MONITOR_CONFIG['infer_url']),
        'app_url':   _clean_url(cfg.get('app_url'),   DEFAULT_MONITOR_CONFIG['app_url']),
    }
    tmp = MONITOR_CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MONITOR_CONFIG_PATH)
    return clean


# ──────────────────────────────────────────────
# Product API
# ──────────────────────────────────────────────
@app.route('/api/products', methods=['GET'])
def list_products():
    conn = get_db()
    rows = conn.execute(
        'SELECT id, serial, name, created_at, (reference_img_b64 IS NOT NULL AND reference_img_b64 != "") as has_ref_img FROM products ORDER BY id'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/products', methods=['POST'])
def create_product():
    data   = request.json
    serial = data.get('serial', '').strip()
    name   = data.get('name', '').strip()
    if not serial:
        return jsonify({'error': '序號不得為空'}), 400
    try:
        conn = get_db()
        cur  = conn.execute(
            'INSERT INTO products (serial, name) VALUES (?,?)', (serial, name))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return jsonify({'id': pid, 'serial': serial, 'name': name}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': '序號已存在'}), 409


@app.route('/api/products/<int:pid>', methods=['PUT'])
def update_product(pid):
    data   = request.json
    serial = data.get('serial', '').strip()
    name   = data.get('name', '').strip()
    if not serial:
        return jsonify({'error': '序號不得為空'}), 400
    conn = get_db()
    conn.execute('UPDATE products SET serial=?, name=? WHERE id=?', (serial, name, pid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/products/<int:pid>', methods=['DELETE'])
def delete_product(pid):
    """Delete an idle product while preserving independent imported sample copies."""
    from contextlib import nullcontext

    edge = app.extensions.get('visionedge_runtime')
    control = getattr(edge, 'control_lock', nullcontext())
    with control:
        conn = get_db()
        previous_product_id = None
        try:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM products WHERE id=?', (pid,)).fetchone():
                return jsonify({'error': '產品不存在'}), 404
            linked = conn.execute('''
                SELECT 1 FROM inspection_rule_items t
                JOIN inspection_rules i ON i.id=t.rule_id
                JOIN regions r ON r.id=t.region_id
                WHERE r.product_id=? AND i.product_id<>? LIMIT 1
            ''', (pid, pid)).fetchone()
            if linked:
                return jsonify({'error': '其他產品的規則仍引用此產品的 Label，請先移除引用再刪除'}), 409

            if edge is not None and int(edge.cfg.product_id or 0) == pid:
                status = edge.status()
                if (status.get('running') or status.get('recording')
                        or getattr(edge, 'inspection_active', lambda: False)()
                        or (getattr(edge, 'packaging', None) and edge.packaging.cycle_id)):
                    return jsonify({'error': '此產品正在使用中，請先結束檢測並停止相機，或切換檢測產品，再刪除'}), 409
                result = edge.update_config({'product_id': 0})
                if not result.get('success'):
                    return jsonify({'error': result.get('error', '無法解除目前選用產品')}), 409
                previous_product_id = pid

            conn.execute('DELETE FROM products WHERE id=?', (pid,))
            conn.commit()
            return jsonify({'ok': True})
        except Exception:
            conn.rollback()
            if previous_product_id is not None:
                try:
                    restore = edge.update_config({'product_id': previous_product_id})
                    if not restore.get('success'):
                        app.logger.error('failed to restore selected product after delete failure: %s', restore)
                except Exception:
                    app.logger.exception('failed to restore selected product after delete failure')
            app.logger.exception('delete product failed')
            return jsonify({'error': '刪除產品失敗，請重新載入後確認狀態'}), 500
        finally:
            conn.close()


# ──────────────────────────────────────────────
# Reference Image API  (BUG FIX)
# ──────────────────────────────────────────────
@app.route('/api/products/<int:pid>/refimg', methods=['GET'])
def get_refimg(pid):
    """Return the saved reference image for a product."""
    conn = get_db()
    row  = conn.execute(
        'SELECT reference_img_b64 FROM products WHERE id=?', (pid,)
    ).fetchone()
    conn.close()
    if not row or not row['reference_img_b64']:
        return jsonify({'image_b64': None})
    return jsonify({'image_b64': row['reference_img_b64']})


# ──────────────────────────────────────────────
# Region API
# ──────────────────────────────────────────────
@app.route('/api/products/<int:pid>/regions', methods=['GET'])
def list_regions(pid):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,product_id,label,x,y,w,h,threshold,search_margin,source_width,source_height,capture_group_id,"
        "COALESCE(sample_hint, 'OK') AS sample_hint FROM regions WHERE product_id=?", (pid,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['sample_hint'] = _clean_sample_hint(d.get('sample_hint'))
        out.append(d)
    return jsonify(out)


@app.route('/api/products/<int:pid>/regions/thumbs', methods=['GET'])
def list_region_thumbs(pid):
    """輕量縮圖端點：回傳這個產品所有 region 已存的樣板小圖（template_b64），
    id 對 base64 字串。跟 /regions 分開是因為那支列表端點刻意省略
    template_b64 讓編輯器主列表輕量；瀏覽既有 region 要顯示縮圖時才需要
    真的載入圖片內容，所以獨立一支、一次把整個產品的縮圖都拿回來，
    避免每一列各自打一次 API。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, template_b64 FROM regions WHERE product_id=?", (pid,)
    ).fetchall()
    conn.close()
    return jsonify({str(r['id']): r['template_b64'] for r in rows if r['template_b64']})


@app.route('/api/products/<int:pid>/capture-groups', methods=['GET'])
def list_capture_groups(pid):
    """列出這個產品所有已持久化的「畫面分組」——每一組代表一次擷取/釘選的來源畫面，
    存檔後仍然記得住，重新載入頁面／重新選這個產品時，前端可以據此把 region 正確分開
    顯示，不會把來自不同畫面/時刻的框全部疊在一起。
    """
    conn = get_db()
    rows = conn.execute(
        '''SELECT g.id, g.label, g.thumb_b64,
                  (SELECT COUNT(*) FROM regions r WHERE r.capture_group_id = g.id) AS region_count
           FROM capture_groups g
           WHERE g.product_id=?
             AND EXISTS (SELECT 1 FROM regions r2 WHERE r2.capture_group_id=g.id)
           ORDER BY g.id ASC''', (pid,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/products/<int:pid>/regions', methods=['POST'])
def save_regions(pid):
    '''Synchronize a product's template regions without churning stable row IDs.

    The Video Label Studio sends the complete desired region set. Older versions
    implemented this as DELETE-all + INSERT-all. That made every saved template
    receive a new regions.id on every edit, invalidating Flow/SOP
    source_region_id references and leaving orphaned capture_groups behind.

    Current behavior is a diff-style sync:
      * rows that still exist are UPDATEd in place (stable IDs),
      * brand-new regions are INSERTed,
      * rows omitted by the client are DELETEd,
      * empty workspace/capture groups are pruned after the sync.
    '''
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Invalid JSON body'}), 400
    conn = get_db()
    try:
        result = _sync_product_regions(conn, pid, data)
        conn.commit()
        return jsonify(result)
    except (ValueError, TypeError, OverflowError) as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        app.logger.exception('save regions failed')
        return jsonify({'ok': False, 'error': f'儲存樣板失敗：{e}'}), 500
    finally:
        conn.close()


def _region_crop_b64(src_img, x, y, w, h):
    if src_img is None or w <= 0 or h <= 0:
        return None
    sh, sw = src_img.shape[:2]
    if sh <= 0 or sw <= 0:
        return None
    x1 = max(0, min(int(x), sw - 1))
    y1 = max(0, min(int(y), sh - 1))
    x2 = max(x1 + 1, min(int(x) + int(w), sw))
    y2 = max(y1 + 1, min(int(y) + int(h), sh))
    crop = src_img[y1:y2, x1:x2]
    return cv2_to_b64(crop) if crop.size > 0 else None


def _resolve_capture_group_for_region(r, existing, temp_key_to_group_id):
    raw_gid = r.get('capture_group_id')
    if raw_gid not in (None, '', 0, '0'):
        try:
            return int(raw_gid)
        except Exception:
            pass
    temp_key = r.get('capture_group_temp_key')
    if temp_key and str(temp_key) in temp_key_to_group_id:
        return temp_key_to_group_id[str(temp_key)]
    if existing is not None and not (r.get('source_image_b64') or ''):
        return existing.get('capture_group_id')
    return None


def _prune_orphan_capture_groups(conn, pid):
    cur = conn.execute('''
        DELETE FROM capture_groups
        WHERE product_id=?
          AND NOT EXISTS (
              SELECT 1 FROM regions r WHERE r.capture_group_id=capture_groups.id
          )
    ''', (pid,))
    return max(0, cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)


def _sync_product_regions(conn, pid, data):
    '''Database implementation used by the route and regression tests.'''
    regions = data.get('regions', [])
    img_b64 = data.get('image_b64', '') or ''
    new_groups = data.get('new_capture_groups') or {}
    if not isinstance(regions, list):
        raise ValueError('regions 必須是陣列')
    if not isinstance(new_groups, dict):
        raise ValueError('new_capture_groups 必須是物件')

    product = conn.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        raise ValueError('找不到指定產品')

    if img_b64:
        conn.execute('UPDATE products SET reference_img_b64=? WHERE id=?', (img_b64, pid))

    referenced_temp_keys = {
        str(r.get('capture_group_temp_key')) for r in regions
        if isinstance(r, dict) and r.get('capture_group_temp_key')
    }
    temp_key_to_group_id = {}
    for temp_key, g in new_groups.items():
        if str(temp_key) not in referenced_temp_keys:
            continue
        cur = conn.execute(
            'INSERT INTO capture_groups (product_id, label, thumb_b64) VALUES (?,?,?)',
            (pid, str((g or {}).get('label') or ''), (g or {}).get('thumb_b64') or None)
        )
        temp_key_to_group_id[str(temp_key)] = cur.lastrowid

    existing_by_id = {
        int(row['id']): dict(row)
        for row in conn.execute('SELECT * FROM regions WHERE product_id=?', (pid,)).fetchall()
    }
    # Legacy monitor Rule Groups refer directly to region IDs. Preserve their
    # label mapping so a deliberate delete+redraw using the same label can be
    # reattached without bringing back the old global ID-churn behavior.
    old_rule_links = [dict(row) for row in conn.execute('''
        SELECT i.rule_id, i.region_id, i.enabled, i.sort_order, r.label
        FROM inspection_rule_items i
        JOIN inspection_rules g ON g.id=i.rule_id
        JOIN regions r ON r.id=i.region_id
        WHERE g.product_id=?
    ''', (pid,)).fetchall()]
    fallback_img = b64_to_cv2(img_b64) if img_b64 else None
    retained_ids = set()
    saved_rows = []
    label_to_saved_ids = {}
    warnings = []

    for idx, raw in enumerate(regions):
        if not isinstance(raw, dict):
            warnings.append(f'第 {idx + 1} 個樣板格式錯誤，已略過')
            continue
        r = raw
        label = str(r.get('label') or '').strip()
        try:
            threshold = float(r.get('threshold', 0.8))
        except Exception:
            threshold = 0.8
        threshold = max(0.0, min(1.0, threshold))
        try:
            search_margin = max(0, int(r.get('search_margin', 0) or 0))
        except Exception:
            search_margin = 0
        sample_hint = _clean_sample_hint(r.get('sample_hint') or r.get('sample_role_hint') or r.get('hint'))

        rid = None
        try:
            if r.get('id') not in (None, ''):
                rid = int(r.get('id'))
        except Exception:
            rid = None
        existing = existing_by_id.get(rid) if rid is not None else None
        own_image_b64 = r.get('source_image_b64') or ''
        capture_group_id = _resolve_capture_group_for_region(r, existing, temp_key_to_group_id)

        if capture_group_id is not None:
            group_ok = conn.execute(
                'SELECT 1 FROM capture_groups WHERE id=? AND product_id=?',
                (capture_group_id, pid)
            ).fetchone()
            if not group_ok:
                capture_group_id = None

        if existing is not None and not own_image_b64:
            x, y, w, h = existing['x'], existing['y'], existing['w'], existing['h']
            tpl_b64 = existing['template_b64']
            source_width, source_height = existing['source_width'], existing['source_height']
        else:
            try:
                x, y, w, h = int(r['x']), int(r['y']), int(r['w']), int(r['h'])
            except Exception:
                if existing is not None:
                    retained_ids.add(existing['id'])
                    warnings.append(f'第 {idx + 1} 個樣板座標無效，保留舊樣板')
                    continue
                warnings.append(f'第 {idx + 1} 個樣板座標無效，已略過')
                continue
            own_img = b64_to_cv2(own_image_b64) if own_image_b64 else fallback_img
            tpl_b64 = _region_crop_b64(own_img, x, y, w, h)
            if tpl_b64 is None:
                if existing is not None:
                    retained_ids.add(existing['id'])
                    warnings.append(f'第 {idx + 1} 個樣板沒有可用來源畫面，已保留舊樣板')
                    continue
                warnings.append(f'第 {idx + 1} 個樣板沒有可用來源畫面，已略過')
                continue
            source_height, source_width = own_img.shape[:2]

        if existing is not None:
            conn.execute('''
                UPDATE regions
                SET label=?, x=?, y=?, w=?, h=?, threshold=?, search_margin=?,
                    sample_hint=?, template_b64=?, source_width=?, source_height=?, capture_group_id=?
                WHERE id=? AND product_id=?
            ''', (label, x, y, w, h, threshold, search_margin, sample_hint,
                  tpl_b64, source_width, source_height, capture_group_id, existing['id'], pid))
            saved_id = int(existing['id'])
            retained_ids.add(saved_id)
        else:
            cur = conn.execute('''
                INSERT INTO regions
                (product_id,label,x,y,w,h,threshold,search_margin,sample_hint,template_b64,source_width,source_height,capture_group_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (pid, label, x, y, w, h, threshold, search_margin,
                  sample_hint, tpl_b64, source_width, source_height, capture_group_id))
            saved_id = int(cur.lastrowid)

        saved_rows.append({'id': saved_id, 'label': label})
        if label:
            label_to_saved_ids.setdefault(label, []).append(saved_id)

    deleted_ids = sorted(set(existing_by_id) - retained_ids)
    if deleted_ids:
        marks = ','.join('?' for _ in deleted_ids)
        conn.execute(f'DELETE FROM inspection_rule_items WHERE region_id IN ({marks})', deleted_ids)
        conn.execute(f'DELETE FROM regions WHERE product_id=? AND id IN ({marks})', [pid, *deleted_ids])
        deleted_set = set(deleted_ids)
        for link in old_rule_links:
            if int(link['region_id']) not in deleted_set:
                continue
            replacements = label_to_saved_ids.get(link.get('label') or '') or []
            if not replacements:
                continue
            conn.execute('''
                INSERT INTO inspection_rule_items (rule_id, region_id, enabled, sort_order)
                VALUES (?,?,?,?)
            ''', (link['rule_id'], replacements[0], link['enabled'], link['sort_order']))
        conn.execute('''
            DELETE FROM inspection_rules
            WHERE product_id=?
              AND NOT EXISTS (SELECT 1 FROM inspection_rule_items i WHERE i.rule_id=inspection_rules.id)
        ''', (pid,))

    pruned_groups = _prune_orphan_capture_groups(conn, pid)
    return {
        'ok': True,
        'count': len(saved_rows),
        'deleted_regions': len(deleted_ids),
        'capture_groups_pruned': pruned_groups,
        'warnings': warnings,
        'stable_ids': True,
    }


def _append_product_region_data(conn, pid, r, data=None):
    data = data or {}
    product = conn.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        raise LookupError('找不到指定產品')
    label = str(r.get('label') or '').strip()
    x, y, w, h = int(r['x']), int(r['y']), int(r['w']), int(r['h'])
    if w <= 0 or h <= 0:
        raise ValueError('樣板寬高必須大於 0')
    threshold = max(0.0, min(1.0, float(r.get('threshold', 0.8))))
    search_margin = max(0, int(r.get('search_margin', 0) or 0))
    sample_hint = _clean_sample_hint(r.get('sample_hint') or r.get('sample_role_hint') or r.get('hint'))
    source_b64 = r.get('source_image_b64') or data.get('image_b64') or ''
    src_img = b64_to_cv2(source_b64) if source_b64 else None
    tpl_b64 = _region_crop_b64(src_img, x, y, w, h)
    if tpl_b64 is None:
        raise ValueError('沒有可用的來源畫面，無法建立樣板')
    source_height, source_width = src_img.shape[:2]
    cur = conn.execute('''
        INSERT INTO regions
        (product_id,label,x,y,w,h,threshold,search_margin,sample_hint,template_b64,source_width,source_height,capture_group_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    ''', (pid, label, x, y, w, h, threshold, search_margin, sample_hint, tpl_b64, source_width, source_height))
    rid = int(cur.lastrowid)
    return {
        'id': rid, 'product_id': pid, 'label': label,
        'x': x, 'y': y, 'w': w, 'h': h, 'threshold': threshold,
        'search_margin': search_margin, 'sample_hint': sample_hint,
        'source_width': source_width, 'source_height': source_height,
    }


@app.route('/api/products/<int:pid>/regions/append', methods=['POST'])
def append_region(pid):
    '''Append exactly one template without replacing the product's region library.'''
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Invalid JSON body'}), 400
    r = data.get('region') if isinstance(data.get('region'), dict) else data
    conn = get_db()
    try:
        region = _append_product_region_data(conn, pid, r, data)
        conn.commit()
        return jsonify({'ok': True, 'region': region})
    except LookupError as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 404
    except (ValueError, TypeError, OverflowError, KeyError) as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        app.logger.exception('append region failed')
        return jsonify({'ok': False, 'error': f'新增樣板失敗：{e}'}), 500
    finally:
        conn.close()

# ──────────────────────────────────────────────
# Rule Group API
# ──────────────────────────────────────────────
def _clean_logic_mode(v):
    v = (v or 'ANY').strip().upper()
    return 'ALL' if v == 'ALL' else 'ANY'


def _clean_sample_role(v):
    """Rule Groups B+: OK samples are acceptance templates; NG samples are hard-reject templates."""
    return 'NG' if str(v or '').strip().upper() in ('NG', 'REJECT', 'FAIL') else 'OK'


def _clean_sample_hint(v):
    """Template library hint only. Final runtime meaning is decided by sample_role in rule groups."""
    vv = str(v or '').strip().upper()
    if vv in ('NG', 'REJECT', 'FAIL'):
        return 'NG'
    if vv in ('NEUTRAL', 'NONE', 'UNSPECIFIED', 'GENERAL'):
        return 'NEUTRAL'
    return 'OK'


# ──────────────────────────────────────────────
# Rule Groups P0/P1 settings
# ──────────────────────────────────────────────
def _ensure_rule_group_settings_schema(conn):
    # Persist only final group-combination logic; group contents stay in inspection_items.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS product_inference_settings (
            product_id        INTEGER PRIMARY KEY,
            final_logic_mode  TEXT NOT NULL DEFAULT 'ALL',
            updated_at        TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')


def _get_product_final_logic_mode(conn, product_id):
    try:
        _ensure_rule_group_settings_schema(conn)
        row = conn.execute(
            'SELECT final_logic_mode FROM product_inference_settings WHERE product_id=?',
            (product_id,)
        ).fetchone()
        return _clean_logic_mode(row['final_logic_mode'] if row else 'ALL')
    except Exception:
        return 'ALL'


def _set_product_final_logic_mode(conn, product_id, logic_mode):
    logic = _clean_logic_mode(logic_mode or 'ALL')
    _ensure_rule_group_settings_schema(conn)
    cur = conn.execute(
        '''UPDATE product_inference_settings
           SET final_logic_mode=?, updated_at=datetime('now','localtime')
           WHERE product_id=?''',
        (logic, product_id)
    )
    if cur.rowcount == 0:
        conn.execute(
            '''INSERT INTO product_inference_settings (product_id, final_logic_mode)
               VALUES (?, ?)''',
            (product_id, logic)
        )
    return logic


@app.route('/api/products/<int:pid>/rules', methods=['GET'])
def list_rules(pid):
    conn = get_db()
    rules = conn.execute(
        '''SELECT id, product_id, name, logic_mode, enabled, sort_order
           FROM inspection_rules WHERE product_id=?
           ORDER BY sort_order, id''', (pid,)
    ).fetchall()
    out = []
    for rule in rules:
        items = conn.execute(
            '''SELECT i.id, i.region_id, i.enabled, i.sort_order, r.label
               FROM inspection_rule_items i
               LEFT JOIN regions r ON r.id=i.region_id
               WHERE i.rule_id=?
               ORDER BY i.sort_order, i.id''', (rule['id'],)
        ).fetchall()
        d = dict(rule)
        d['enabled'] = bool(d.get('enabled'))
        d['items'] = [dict(x) for x in items if x['region_id'] is not None]
        out.append(d)
    conn.close()
    return jsonify(out)


@app.route('/api/products/<int:pid>/rules', methods=['POST'])
def save_rules(pid):
    data = request.json or {}
    rules = data.get('rules', [])
    conn = get_db()
    try:
        old_ids = [row['id'] for row in conn.execute(
            'SELECT id FROM inspection_rules WHERE product_id=?', (pid,)
        ).fetchall()]
        for rid_rule in old_ids:
            conn.execute('DELETE FROM inspection_rule_items WHERE rule_id=?', (rid_rule,))
        conn.execute('DELETE FROM inspection_rules WHERE product_id=?', (pid,))

        valid_region_ids = {row['id'] for row in conn.execute(
            'SELECT id FROM regions WHERE product_id=?', (pid,)
        ).fetchall()}

        saved = 0
        for idx, rule in enumerate(rules):
            name = (rule.get('name') or f'Rule-{idx+1}').strip()
            logic = _clean_logic_mode(rule.get('logic_mode'))
            enabled = 1 if rule.get('enabled', True) else 0
            item_ids = []
            for x in rule.get('region_ids', rule.get('items', [])):
                if isinstance(x, dict):
                    x = x.get('region_id')
                try:
                    region_id = int(x)
                except Exception:
                    continue
                if region_id in valid_region_ids and region_id not in item_ids:
                    item_ids.append(region_id)
            if not name or not item_ids:
                continue
            cur = conn.execute(
                '''INSERT INTO inspection_rules (product_id, name, logic_mode, enabled, sort_order)
                   VALUES (?,?,?,?,?)''',
                (pid, name, logic, enabled, idx)
            )
            rule_id = cur.lastrowid
            for j, region_id in enumerate(item_ids):
                conn.execute(
                    '''INSERT INTO inspection_rule_items (rule_id, region_id, enabled, sort_order)
                       VALUES (?,?,1,?)''',
                    (rule_id, region_id, j)
                )
            saved += 1
        conn.commit()
        return jsonify({'ok': True, 'count': saved})
    finally:
        conn.close()



# ──────────────────────────────────────────────
# Multi-Sample Template Formal V2 API
# ──────────────────────────────────────────────
@app.route('/api/templates', methods=['GET'])
def list_all_templates():
    # Return reusable template library from all products/regions.
    conn = get_db()
    rows = conn.execute('''
        SELECT r.id, r.product_id, r.label, r.x, r.y, r.w, r.h, r.threshold, r.search_margin,
               COALESCE(r.sample_hint, 'OK') AS sample_hint,
               p.serial AS product_serial, p.name AS product_name
        FROM regions r
        JOIN products p ON p.id = r.product_id
        WHERE r.template_b64 IS NOT NULL AND r.template_b64 != ''
        ORDER BY p.serial, r.label, r.id
    ''').fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['sample_hint'] = _clean_sample_hint(d.get('sample_hint'))
        out.append(d)
    return jsonify(out)


def _load_inspection_items(conn, product_id, enabled_only=False):
    where_enabled = 'AND enabled=1' if enabled_only else ''
    items = conn.execute(f'''
        SELECT id, product_id, name, logic_mode, enabled, sort_order,
               COALESCE(step_no, sort_order + 1) AS step_no,
               COALESCE(required, 1) AS required,
               COALESCE(min_consecutive_hits, 2) AS min_consecutive_hits,
               COALESCE(hold_ms, 400) AS hold_ms,
               COALESCE(timeout_sec, 0) AS timeout_sec,
               COALESCE(allow_out_of_order, 0) AS allow_out_of_order,
               COALESCE(latch_when_done, 1) AS latch_when_done,
               COALESCE(alarm_if_missing, 1) AS alarm_if_missing,
               COALESCE(ui_color, '') AS ui_color
        FROM inspection_items
        WHERE product_id=? {where_enabled}
        ORDER BY sort_order, id
    ''', (product_id,)).fetchall()
    out = []
    for item in items:
        sample_enabled = 'AND t.enabled=1' if enabled_only else ''
        samples = conn.execute(f'''
            SELECT t.id, t.item_id, t.sample_name, COALESCE(t.sample_role, 'OK') AS sample_role, t.source_product_id, t.source_region_id,
                   t.x, t.y, t.w, t.h, t.threshold, t.search_margin,
                   t.source_width, t.source_height, t.enabled, t.sort_order,
                   p.serial AS source_product_serial, p.name AS source_product_name,
                   r.label AS source_region_label,
                   COALESCE(r.sample_hint, 'OK') AS source_sample_hint
            FROM inspection_item_templates t
            LEFT JOIN products p ON p.id=t.source_product_id
            LEFT JOIN regions r ON r.id=t.source_region_id
            WHERE t.item_id=? {sample_enabled}
            ORDER BY t.sort_order, t.id
        ''', (item['id'],)).fetchall()
        d = dict(item)
        for key in ('enabled', 'required', 'allow_out_of_order', 'latch_when_done', 'alarm_if_missing'):
            d[key] = bool(d.get(key))
        sample_rows = []
        for x in samples:
            sx = dict(x)
            sx['sample_role'] = _clean_sample_role(sx.get('sample_role'))
            sx['source_sample_hint'] = _clean_sample_hint(sx.get('source_sample_hint'))
            sample_rows.append(sx)
        d['samples'] = sample_rows
        out.append(d)
    return out


@app.route('/api/products/<int:pid>/inspection-items', methods=['GET'])
def list_inspection_items(pid):
    conn = get_db()
    try:
        return jsonify(_load_inspection_items(conn, pid, enabled_only=False))
    finally:
        conn.close()


@app.route('/api/products/<int:pid>/regions/<int:rid>/attach-to-step', methods=['POST'])
def attach_region_to_step(pid, rid):
    """Additive alternative to the destructive /inspection-items bulk-replace.

    Lets the labeling page turn a freshly-drawn region straight into a Step
    sample in one action, without a full round trip through the separate SOP
    Designer page. Only touches the one Step being targeted — every other
    Step's samples are left completely alone.

    body: {
      step_id: int | null,      // existing Step, OR
      step_name: str,           // create a new Step with this name (used when step_id is absent)
      sample_role: 'OK' | 'NG',
      sample_name: str          // optional, defaults to the region's label
    }
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        region = conn.execute(
            'SELECT * FROM regions WHERE id=? AND product_id=?', (rid, pid)
        ).fetchone()
        if not region:
            return jsonify({'ok': False, 'error': '找不到這個樣板，可能已被刪除'}), 404
        if not region['template_b64']:
            return jsonify({'ok': False, 'error': '這個樣板還沒有裁切出畫面內容'}), 400

        sample_role = _clean_sample_role(data.get('sample_role'))
        step_id = data.get('step_id')

        if step_id:
            step = conn.execute(
                'SELECT * FROM inspection_items WHERE id=? AND product_id=?', (step_id, pid)
            ).fetchone()
            if not step:
                return jsonify({'ok': False, 'error': '找不到指定的 Step'}), 404
        else:
            step_name = str(data.get('step_name') or '').strip()
            if not step_name:
                return jsonify({'ok': False, 'error': '請提供 Step 名稱或已存在的 step_id'}), 400
            next_sort = conn.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM inspection_items WHERE product_id=?', (pid,)
            ).fetchone()[0]
            next_step_no = conn.execute(
                'SELECT COALESCE(MAX(step_no), 0) + 1 FROM inspection_items WHERE product_id=?', (pid,)
            ).fetchone()[0]
            cur = conn.execute('''
                INSERT INTO inspection_items
                (product_id, name, logic_mode, enabled, sort_order, step_no, required,
                 min_consecutive_hits, hold_ms, timeout_sec, allow_out_of_order,
                 latch_when_done, alarm_if_missing, ui_color)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (pid, step_name, 'ANY', 1, next_sort, next_step_no, 1, 2, 400, 0.0, 0, 1, 1, ''))
            step_id = cur.lastrowid
            step = conn.execute('SELECT * FROM inspection_items WHERE id=?', (step_id,)).fetchone()

        next_sample_order = conn.execute(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM inspection_item_templates WHERE item_id=?', (step_id,)
        ).fetchone()[0]
        sample_name = str(data.get('sample_name') or region['label'] or 'Sample').strip()[:240]
        conn.execute('''
            INSERT INTO inspection_item_templates
            (item_id, sample_name, sample_role, source_product_id, source_region_id,
             x, y, w, h, threshold, search_margin, template_b64, source_width, source_height, enabled, sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        ''', (step_id, sample_name, sample_role, pid, rid,
              region['x'], region['y'], region['w'], region['h'], region['threshold'],
              region['search_margin'] or 0, region['template_b64'],
              region['source_width'], region['source_height'], next_sample_order))
        conn.commit()

        items = _load_inspection_items(conn, pid, enabled_only=False)
        attached_step = next((it for it in items if it['id'] == step_id), None)
        return jsonify({'ok': True, 'step': attached_step, 'steps': items})
    except Exception as e:
        conn.rollback()
        app.logger.exception('attach region to step failed')
        return jsonify({'ok': False, 'error': f'附加樣板失敗：{e}'}), 500
    finally:
        conn.close()




@app.route('/api/products/<int:pid>/rule-groups', methods=['GET'])
def list_inference_rule_groups(pid):
    """Rule Groups v2: returns group list plus final group-combination logic."""
    conn = get_db()
    try:
        groups = _load_inspection_items(conn, pid, enabled_only=False)
        return jsonify({
            'ok': True,
            'final_logic_mode': _get_product_final_logic_mode(conn, pid),
            'groups': groups,
            'items': groups,  # backward-friendly alias
        })
    finally:
        conn.close()


@app.route('/api/products/<int:pid>/rule-groups', methods=['POST'])
def save_inference_rule_groups(pid):
    """Alias for inspection-items POST; accepts {final_logic_mode, groups/items}."""
    return save_inspection_items(pid)

def _save_inspection_items_data(conn, pid, data):
    """Atomically replace ordered SOP steps and their local template copies.

    Enabled steps must contain at least one valid OK sample. Disabled steps may be
    kept as drafts without samples. A source-region reference and a local sample
    id may both be provided; the local copy is used as fallback when the source
    template was later deleted or replaced.
    """
    items = data.get('steps', data.get('groups', data.get('items', [])))
    if not isinstance(items, list):
        raise ValueError('steps/groups/items 必須是陣列')
    if len(items) > 300:
        raise ValueError('單一產品最多支援 300 個 Step')
    final_logic_mode = _get_product_final_logic_mode(conn, pid)
    if 'final_logic_mode' in data:
        if data['final_logic_mode'] not in ('ANY', 'ALL'):
            raise ValueError('final_logic_mode must be ANY or ALL')
        final_logic_mode = data['final_logic_mode']

    product = conn.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        raise LookupError('找不到指定產品')

    normalized = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f'Step {idx + 1} 格式錯誤')
        item = dict(raw)
        name = str(item.get('name') or '').strip()
        if not name:
            raise ValueError(f'Step {idx + 1} 尚未命名')
        enabled = bool(item.get('enabled', True))
        samples_in = item.get('samples', item.get('template_refs', [])) or []
        if not isinstance(samples_in, list):
            raise ValueError(f'Step {idx + 1} 的 samples 必須是陣列')
        declared_ok = sum(
            1 for x in samples_in
            if _clean_sample_role((x or {}).get('sample_role') if isinstance(x, dict) else 'OK') == 'OK'
        )
        if enabled and declared_ok == 0:
            raise ValueError(f'Step {idx + 1}「{name}」至少需要一個 OK 樣板')
        normalized.append((item, name, enabled, samples_in))

    _set_product_final_logic_mode(conn, pid, final_logic_mode)
    existing_sample_copies = {
        row['id']: dict(row) for row in conn.execute('''
            SELECT t.id, t.sample_name, COALESCE(t.sample_role, 'OK') AS sample_role,
                   t.source_product_id, t.source_region_id, t.x, t.y, t.w, t.h,
                   t.threshold, t.search_margin, t.template_b64, t.source_width, t.source_height
            FROM inspection_item_templates t
            JOIN inspection_items i ON i.id=t.item_id
            WHERE i.product_id=?
        ''', (pid,)).fetchall()
    }

    old_item_ids = [row['id'] for row in conn.execute(
        'SELECT id FROM inspection_items WHERE product_id=?', (pid,)
    ).fetchall()]
    for item_id in old_item_ids:
        conn.execute('DELETE FROM inspection_item_templates WHERE item_id=?', (item_id,))
    conn.execute('DELETE FROM inspection_items WHERE product_id=?', (pid,))

    saved_items = 0
    saved_samples = 0
    warnings = []
    for idx, (item, name, enabled_bool, samples_in) in enumerate(normalized):
        logic = _clean_logic_mode(item.get('logic_mode'))
        enabled = 1 if enabled_bool else 0
        step_no = idx + 1
        required = 1 if item.get('required', True) else 0
        min_hits = max(1, min(120, int(item.get('min_consecutive_hits') or 2)))
        hold_ms = max(0, min(600000, int(item.get('hold_ms') or 400)))
        timeout_sec = max(0.0, min(86400.0, float(item.get('timeout_sec') or 0)))
        allow_out = 1 if item.get('allow_out_of_order', False) else 0
        latch_done = 1 if item.get('latch_when_done', True) else 0
        alarm_missing = 1 if item.get('alarm_if_missing', True) else 0
        ui_color = str(item.get('ui_color') or '').strip()[:32]
        cur = conn.execute('''
            INSERT INTO inspection_items
            (product_id, name, logic_mode, enabled, sort_order, step_no, required,
             min_consecutive_hits, hold_ms, timeout_sec, allow_out_of_order,
             latch_when_done, alarm_if_missing, ui_color)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (pid, name, logic, enabled, idx, step_no, required, min_hits, hold_ms,
              timeout_sec, allow_out, latch_done, alarm_missing, ui_color))
        item_id = cur.lastrowid
        added_source_keys = set()
        sample_order = 0
        actual_ok = 0

        for sample_cfg in samples_in:
            if not isinstance(sample_cfg, dict):
                sample_cfg = {'source_region_id': sample_cfg}
            sample_role = _clean_sample_role(sample_cfg.get('sample_role') or sample_cfg.get('role'))
            source_region_id = None
            existing_sample_id = None
            try:
                raw_source_id = sample_cfg.get('source_region_id') or sample_cfg.get('region_id')
                if raw_source_id not in (None, ''):
                    source_region_id = int(raw_source_id)
            except Exception:
                source_region_id = None
            try:
                raw_existing_id = sample_cfg.get('existing_sample_id') or sample_cfg.get('local_sample_id') or sample_cfg.get('id')
                if raw_existing_id not in (None, ''):
                    existing_sample_id = int(raw_existing_id)
            except Exception:
                existing_sample_id = None

            src = None
            source_key_id = source_region_id
            # Imported samples remain snapshots until explicitly refreshed.
            old_copy = existing_sample_copies.get(existing_sample_id)
            if (old_copy and old_copy.get('source_product_id') != pid
                    and old_copy.get('source_region_id') == source_region_id
                    and old_copy.get('template_b64')):
                src = dict(old_copy)
                src['default_sample_name'] = src.get('sample_name') or 'Imported sample'
            if src is None and source_region_id is not None and source_region_id > 0:
                src_row = conn.execute('''
                    SELECT r.*, p.serial AS product_serial, p.name AS product_name
                    FROM regions r JOIN products p ON p.id=r.product_id
                    WHERE r.id=?
                ''', (source_region_id,)).fetchone()
                if src_row and src_row['template_b64']:
                    src = dict(src_row)
                    src['source_product_id'] = src['product_id']
                    src['source_region_id'] = src['id']
                    src['default_sample_name'] = f"{src.get('product_serial') or ''} / {src.get('label') or ('Region-'+str(source_region_id))}".strip(' /')

            if src is None and existing_sample_id is not None:
                old_sample = existing_sample_copies.get(existing_sample_id)
                if old_sample and old_sample.get('template_b64'):
                    src = dict(old_sample)
                    source_key_id = -existing_sample_id
                    src['default_sample_name'] = src.get('sample_name') or f'Copied Sample {existing_sample_id}'

            if not src or not src.get('template_b64'):
                warnings.append(f'Step {idx + 1}：略過一個已不存在的樣板')
                continue
            sample_key = (source_key_id, sample_role)
            if sample_key in added_source_keys:
                continue
            sample_name = str(sample_cfg.get('sample_name') or sample_cfg.get('name') or src.get('default_sample_name') or 'Sample').strip()[:240]
            conn.execute('''
                INSERT INTO inspection_item_templates
                (item_id, sample_name, sample_role, source_product_id, source_region_id,
                 x, y, w, h, threshold, search_margin, template_b64, source_width, source_height, enabled, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (item_id, sample_name, sample_role, src.get('source_product_id'), src.get('source_region_id'),
                  src['x'], src['y'], src['w'], src['h'], src['threshold'],
                  src['search_margin'] if src.get('search_margin') is not None else 0,
                  src['template_b64'], src.get('source_width') or 0,
                  src.get('source_height') or 0, 1, sample_order))
            added_source_keys.add(sample_key)
            saved_samples += 1
            sample_order += 1
            if sample_role == 'OK':
                actual_ok += 1

        if enabled and actual_ok == 0:
            raise ValueError(f'Step {idx + 1}「{name}」沒有可用的 OK 樣板；來源可能已被刪除')
        saved_items += 1

    return {
        'ok': True,
        'count': saved_items,
        'samples': saved_samples,
        'warnings': warnings,
        'final_logic_mode': final_logic_mode,
    }


@app.route('/api/products/<int:pid>/inspection-items', methods=['POST'])
def save_inspection_items(pid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        result = _save_inspection_items_data(conn, pid, data)
        conn.commit()
        return jsonify(result)
    except LookupError as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 404
    except (ValueError, TypeError, OverflowError) as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        app.logger.exception('save inspection items failed')
        return jsonify({'ok': False, 'error': f'儲存 Step 失敗：{e}'}), 500
    finally:
        conn.close()

def _load_runtime_inspection_items(conn, product_id):
    items = conn.execute('''
        SELECT id, name, logic_mode, enabled, sort_order
        FROM inspection_items
        WHERE product_id=? AND enabled=1
        ORDER BY sort_order, id
    ''', (product_id,)).fetchall()
    out = []
    for item in items:
        samples = conn.execute('''
            SELECT id, item_id, sample_name, COALESCE(sample_role, 'OK') AS sample_role, source_product_id, source_region_id, x, y, w, h, threshold, search_margin, template_b64, sort_order
            FROM inspection_item_templates
            WHERE item_id=? AND enabled=1
            ORDER BY sort_order, id
        ''', (item['id'],)).fetchall()
        if not samples:
            continue
        sample_rows = []
        for x in samples:
            sx = dict(x)
            sx['sample_role'] = _clean_sample_role(sx.get('sample_role'))
            sample_rows.append(sx)
        out.append({
            'id': item['id'],
            'name': item['name'],
            'logic_mode': _clean_logic_mode(item['logic_mode']),
            'samples': sample_rows
        })
    return out


def _match_multi_sample_items(src, inspection_items, method, draw_vis=True, final_logic_mode='ALL'):
    # Rule Groups B+: OK samples are positive acceptance templates; NG samples are hard-reject templates.
    sample_results = []
    item_results = []
    vis = src.copy() if draw_vis else None
    hard_reject = False

    for item in inspection_items:
        pseudo_regions = []
        sample_meta = {}
        for sample in item.get('samples', []):
            role = _clean_sample_role(sample.get('sample_role'))
            label_prefix = 'NG' if role == 'NG' else 'OK'
            pseudo_regions.append({
                'id': sample['id'],
                'label': f"{item['name']} / [{label_prefix}] {sample.get('sample_name') or ('Sample-'+str(sample['id']))}",
                'x': sample['x'], 'y': sample['y'], 'w': sample['w'], 'h': sample['h'],
                'threshold': sample['threshold'],
                'search_margin': sample.get('search_margin') or 0,
                'template_b64': sample.get('template_b64'),
            })
            sample_meta[int(sample['id'])] = {'sample_role': role}

        results, _ = _match_templates(src, pseudo_regions, method, draw_vis=False)
        ok_results = []
        ng_results = []
        local_items = []
        matched_ok = []
        matched_ng = []

        for r in results:
            rr = dict(r)
            sid = int(rr.get('id')) if rr.get('id') is not None else None
            role = sample_meta.get(sid, {}).get('sample_role', 'OK')
            raw_match = bool(rr.get('pass'))
            rr['sample_id'] = rr.pop('id')
            rr['item_id'] = item['id']
            rr['sample_role'] = role
            rr['matched'] = raw_match
            rr['reject'] = bool(role == 'NG' and raw_match)
            # For NG samples, pass=True means "not triggered". If triggered, it is a red hard reject.
            if role == 'NG':
                rr['pass'] = not raw_match
                ng_results.append(rr)
                if raw_match:
                    matched_ng.append(rr)
            else:
                rr['pass'] = raw_match
                ok_results.append(rr)
                if raw_match:
                    matched_ok.append(rr)
            sample_results.append(rr)
            local_items.append({
                'sample_id': rr.get('sample_id'),
                'sample_role': role,
                'label': rr.get('label'),
                'score': rr.get('score'),
                'threshold': rr.get('threshold'),
                'matched': raw_match,
                'reject': rr.get('reject'),
                'pass': bool(rr.get('pass')),
                'error': rr.get('error')
            })

        ng_hit = any(bool(r.get('reject')) for r in ng_results)
        if ok_results:
            ok_pass = any(bool(r.get('pass')) for r in ok_results) if item['logic_mode'] == 'ANY' else all(bool(r.get('pass')) for r in ok_results)
        else:
            # NG-only groups are valid: pass when no NG sample is matched.
            ok_pass = True
        item_pass = (not ng_hit) and ok_pass
        if ng_hit:
            hard_reject = True

        # Draw ALL sample boxes in multi-sample mode.
        # v6 fix: show both configured/source ROI and actual best-match box for every sample,
        # so multi-sample groups no longer look like they only have one visible bounding box.
        if vis is not None:
            for draw_i, r in enumerate(sample_results[-len(results):] if results else []):
                if not r:
                    continue
                role = _clean_sample_role(r.get('sample_role'))
                if role == 'NG' and r.get('reject'):
                    color = (0, 0, 255)
                elif role == 'NG':
                    color = (0, 180, 180)
                else:
                    color = (0, 255, 80) if bool(r.get('pass')) else (0, 60, 255)

                ex, ey = int(r.get('x') or 0), int(r.get('y') or 0)
                ew, eh = int(r.get('w') or 0), int(r.get('h') or 0)
                if ew > 0 and eh > 0:
                    margin = int(r.get('search_margin') or 0)
                    if margin > 0:
                        sx1 = max(0, ex - margin); sy1 = max(0, ey - margin)
                        sx2 = min(src.shape[1], ex + ew + margin); sy2 = min(src.shape[0], ey + eh + margin)
                        cv2.rectangle(vis, (sx1, sy1), (sx2, sy2), (180, 180, 60), 1)
                    cv2.rectangle(vis, (ex, ey), (ex + ew, ey + eh), (255, 200, 0), 1)

                if r.get('match_loc') and r.get('match_size'):
                    tl = tuple(r['match_loc']); tw, th = r['match_size']
                    cv2.rectangle(vis, tl, (tl[0]+tw, tl[1]+th), color, 2)
                    text_x, text_y_base = tl[0], tl[1]
                else:
                    tl = (ex, ey); tw, th = max(ew, 1), max(eh, 1)
                    text_x, text_y_base = ex, ey

                score_txt = r.get('score') if r.get('score') is not None else 0
                raw_label = str(r.get('label') or item['name'])
                sample_label = raw_label.split('/', 1)[1].strip() if '/' in raw_label else raw_label
                tag = 'NG' if role == 'NG' else 'OK'
                text = f"#{draw_i+1} {item['name']}:{sample_label} [{tag}] {float(score_txt):.3f}"
                if len(text) > 56:
                    text = text[:53] + '...'
                label_y = text_y_base - 6 - (draw_i % 6) * 14
                if label_y < 12:
                    label_y = text_y_base + th + 14 + (draw_i % 6) * 14
                cv2.putText(vis, text,
                            (text_x, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)

        item_results.append({
            'id': item['id'],
            'name': item['name'],
            'logic_mode': item['logic_mode'],
            'pass': bool(item_pass),
            'hard_reject': bool(ng_hit),
            'matched': matched_ng[0]['label'] if matched_ng else (matched_ok[0]['label'] if matched_ok else None),
            'items': local_items
        })

    final_logic_mode = _clean_logic_mode(final_logic_mode)
    if hard_reject:
        final_pass = False
    elif item_results:
        final_pass = all(bool(x.get('pass')) for x in item_results) if final_logic_mode == 'ALL' else any(bool(x.get('pass')) for x in item_results)
    else:
        final_pass = False
    for _g in item_results:
        _g.setdefault('group_type', 'inspection_item')
    final_name = 'Final Logic'
    if hard_reject:
        final_name = 'Final Logic / NG override'
    rule_results = [{'name': final_name, 'logic_mode': final_logic_mode, 'pass': bool(final_pass), 'hard_reject': bool(hard_reject), 'items': []}] + item_results
    return final_pass, sample_results, rule_results, vis

def _load_rule_groups(conn, product_id):
    rules = conn.execute(
        '''SELECT id, name, logic_mode, enabled, sort_order
           FROM inspection_rules
           WHERE product_id=? AND enabled=1
           ORDER BY sort_order, id''', (product_id,)
    ).fetchall()
    out = []
    for rule in rules:
        items = conn.execute(
            '''SELECT i.region_id, r.label
               FROM inspection_rule_items i
               LEFT JOIN regions r ON r.id=i.region_id
               WHERE i.rule_id=? AND i.enabled=1
               ORDER BY i.sort_order, i.id''', (rule['id'],)
        ).fetchall()
        out.append({
            'id': rule['id'],
            'name': rule['name'],
            'logic_mode': _clean_logic_mode(rule['logic_mode']),
            'items': [dict(x) for x in items],
        })
    return out


def _match_templates(src, regions, method, draw_vis=True):
    src_gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    sh, sw = src_gray.shape[:2]
    results = []
    vis = src.copy() if draw_vis else None

    for reg in regions:
        rid = reg['id']
        label = reg['label'] or f'Region-{rid}'
        threshold = reg['threshold']
        search_margin = reg['search_margin'] if reg['search_margin'] is not None else 0
        tpl_b64 = reg['template_b64']

        base = {'id': rid, 'label': label, 'threshold': threshold, 'search_margin': search_margin}
        if not tpl_b64:
            results.append({**base, 'score': None, 'pass': False, 'error': '無樣板影像'})
            continue

        tpl = b64_to_cv2(tpl_b64)
        tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
        th, tw = tpl_gray.shape[:2]

        if th > sh or tw > sw:
            results.append({**base, 'score': None, 'pass': False, 'error': '樣板大於來源影像'})
            continue

        ex, ey, ew, eh = reg['x'], reg['y'], reg['w'], reg['h']
        if search_margin > 0:
            roi_x1 = max(0, ex - search_margin); roi_y1 = max(0, ey - search_margin)
            roi_x2 = min(sw, ex + ew + search_margin); roi_y2 = min(sh, ey + eh + search_margin)
            search_region = src_gray[roi_y1:roi_y2, roi_x1:roi_x2]
            offset = (roi_x1, roi_y1)
            if vis is not None:
                cv2.rectangle(vis, (roi_x1, roi_y1), (roi_x2, roi_y2), (180, 180, 60), 1)
        else:
            search_region = src_gray
            offset = (0, 0)

        if search_region.shape[0] < th or search_region.shape[1] < tw:
            search_region = src_gray
            offset = (0, 0)

        res = cv2.matchTemplate(search_region, tpl_gray, method)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        if method == cv2.TM_SQDIFF_NORMED:
            score, local_tl = 1.0 - float(min_val), min_loc
        else:
            score, local_tl = float(max_val), max_loc

        top_left = (local_tl[0] + offset[0], local_tl[1] + offset[1])
        passed = score >= threshold

        if vis is not None:
            color = (0, 255, 80) if passed else (0, 60, 255)
            cv2.rectangle(vis, top_left, (top_left[0]+tw, top_left[1]+th), color, 2)
            cv2.putText(vis, f'{label} {score:.3f}',
                        (top_left[0], max(top_left[1]-6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
            cv2.rectangle(vis, (ex, ey), (ex+ew, ey+eh), (255, 200, 0), 1)

        results.append({
            **base, 'score': round(score, 4), 'pass': passed,
            'match_loc': list(top_left), 'match_size': [tw, th],
        })
    return results, vis


def _evaluate_rule_groups(template_results, rule_groups):
    by_id = {r['id']: r for r in template_results}
    if not rule_groups:
        return all(r.get('pass') for r in template_results), []

    rule_results = []
    for rule in rule_groups:
        items = []
        for item in rule.get('items', []):
            region_id = item.get('region_id')
            tr = by_id.get(region_id)
            if tr is None:
                items.append({
                    'region_id': region_id, 'label': item.get('label') or f'Region-{region_id}',
                    'score': None, 'threshold': None, 'pass': False, 'error': 'template 不存在或已被刪除'
                })
            else:
                items.append({
                    'region_id': region_id, 'label': tr.get('label'), 'score': tr.get('score'),
                    'threshold': tr.get('threshold'), 'pass': bool(tr.get('pass')),
                    'error': tr.get('error')
                })
        if not items:
            rule_pass = False
        elif rule['logic_mode'] == 'ALL':
            rule_pass = all(x.get('pass') for x in items)
        else:
            rule_pass = any(x.get('pass') for x in items)
        matched = [x for x in items if x.get('pass')]
        rule_results.append({
            'id': rule['id'], 'name': rule['name'], 'logic_mode': rule['logic_mode'],
            'pass': rule_pass, 'matched': matched[0]['label'] if matched else None,
            'items': items,
        })
    return all(r['pass'] for r in rule_results), rule_results


def run_inspection_frame(src, product_id, method, draw_vis=True):
    """Use the same matcher as offline SOP and multi-stream runtime."""
    product = _load_product_dict(product_id) if '_load_product_dict' in globals() else None
    src = _prepare_frame(src, product) if '_prepare_frame' in globals() else src
    regions = vc.load_regions(DB_PATH, product_id)
    rule_groups = vc.load_rule_groups(DB_PATH, product_id)
    steps = vc.load_runtime_inspection_items(DB_PATH, product_id)
    cache = vc.TemplateCache(
        regions,
        rule_groups,
        steps,
        vc.load_product_final_logic_mode(DB_PATH, product_id),
    )
    all_pass, results, vis = vc.run_inference(
        src,
        cache,
        method=method,
        draw_vis=draw_vis,
    )
    return all_pass, results, getattr(cache, 'last_rule_results', []) or [], vis


# ──────────────────────────────────────────────
# Inference API
# ──────────────────────────────────────────────
@app.route('/api/infer', methods=['POST'])
def infer():
    data       = request.json
    pid        = int(data.get('product_id'))
    img_b64    = data.get('image_b64', '')
    method_str = data.get('method', 'TM_CCOEFF_NORMED')

    method_map = {
        'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED,
        'TM_CCORR_NORMED':  cv2.TM_CCORR_NORMED,
        'TM_SQDIFF_NORMED': cv2.TM_SQDIFF_NORMED,
    }
    method = method_map.get(method_str, cv2.TM_CCOEFF_NORMED)

    if not img_b64:
        return jsonify({'error': '未提供影像'}), 400

    src = b64_to_cv2(img_b64)
    all_pass, results, rule_results, vis = run_inspection_frame(src, pid, method, draw_vis=True)
    stamp_text = 'PASS' if all_pass else 'FAIL'
    result_b64 = cv2_to_b64(vis)

    conn = get_db()
    conn.execute(
        'INSERT INTO inference_logs (product_id, result_json, pass_fail, storage_status) VALUES (?,?,?,?)',
        (pid, json.dumps(results, ensure_ascii=False), stamp_text, 'NONE')
    )
    conn.commit()
    conn.close()

    return jsonify({'pass': all_pass, 'results': results, 'rules': rule_results, 'result_img': result_b64})


# ──────────────────────────────────────────────
# Video Upload & Frame Extraction API
# 前端上傳影片 → 後端用 OpenCV 擷幀，完全繞過瀏覽器 canvas
# 避免 ARM/QCS5430 NV12 硬體解碼時 canvas drawImage 產生的影像錯位問題
# ──────────────────────────────────────────────

# 暫存上傳的影片路徑（session 級別，重啟後清空）
_video_sessions = {}   # token → filepath
_video_sessions_lock = threading.RLock()


def _video_session_get(token):
    with _video_sessions_lock:
        return _video_sessions.get(str(token or ''))


def _video_session_set(token, path):
    with _video_sessions_lock:
        _video_sessions[str(token)] = path


def _video_session_pop(token):
    with _video_sessions_lock:
        return _video_sessions.pop(str(token or ''), None)


@app.route('/api/video/upload', methods=['POST'])
def video_upload():
    """接收前端上傳的影片，存到 UPLOAD_DIR，回傳 token 和影片資訊。"""
    if 'file' not in request.files:
        return jsonify({'error': '未收到影片檔案'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '檔名為空'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        return jsonify({'error': f'不支援的影片副檔名：{ext or "(無)"}'}), 400
    _cleanup_stale_video_uploads()
    token = uuid.uuid4().hex
    path  = os.path.join(UPLOAD_DIR, f'infer_video_{token}{ext}')
    f.save(path)

    # 讀取影片基本資訊
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        os.remove(path)
        return jsonify({'error': '無法開啟影片（格式不支援）'}), 400

    fps       = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0)
    frame_cnt = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    rotate_code = vc.get_video_rotate_code(cap)
    raw_width   = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    raw_height  = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    width, height = vc.rotated_frame_size(raw_width, raw_height, rotate_code)
    duration  = frame_cnt / fps if fps > 0 else 0
    cap.release()

    _video_session_set(token, path)
    return jsonify({
        'token':    token,
        'fps':      fps,
        'frames':   frame_cnt,
        'width':    width,
        'height':   height,
        'duration': duration,
    })


@app.route('/api/video/frame', methods=['GET'])
def video_frame():
    """用 OpenCV 擷取指定時間點的幀，回傳 base64 PNG。
    Query params: token, time (秒，浮點)
    """
    token = request.args.get('token', '')
    try:
        time_s = max(0.0, float(request.args.get('time', 0) or 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'time 必須是數值'}), 400

    path = _video_session_get(token)
    if not path or not os.path.exists(path):
        return jsonify({'error': '影片 token 無效或已過期'}), 404

    cap = cv2.VideoCapture(path)
    fps = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0)
    total_frames = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    rotate_code = vc.get_video_rotate_code(cap)
    target_frame = int(round(time_s * fps))
    if total_frames > 0:
        target_frame = max(0, min(total_frames - 1, target_frame))
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    ok, frame = cap.read()
    cap.release()

    if not ok or frame is None:
        return jsonify({'error': f'無法讀取第 {target_frame} 幀'}), 400
    frame = vc.apply_rotate_code(frame, rotate_code)

    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    b64 = 'data:image/jpeg;base64,' + base64.b64encode(buf).decode()
    return jsonify({'image_b64': b64, 'frame': target_frame})


@app.route('/api/video/infer', methods=['POST'])
def video_infer():
    """對影片指定時間點進行推論（後端擷幀，不經過瀏覽器 canvas）。
    Body: { token, time, product_id, method }
    """
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    try:
        time_s = max(0.0, float(data.get('time', 0) or 0))
        pid = int(data.get('product_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'time／product_id 格式錯誤'}), 400
    method_str = data.get('method', 'TM_CCOEFF_NORMED')

    path = _video_session_get(token)
    if not path or not os.path.exists(path):
        return jsonify({'error': '影片 token 無效或已過期'}), 404

    cap = cv2.VideoCapture(path)
    fps = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0)
    total_frames = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    rotate_code = vc.get_video_rotate_code(cap)
    target_frame = int(round(time_s * fps))
    if total_frames > 0:
        target_frame = max(0, min(total_frames - 1, target_frame))
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    ok, src = cap.read()
    cap.release()

    if not ok or src is None:
        return jsonify({'error': '無法擷取影片幀'}), 400
    src = vc.apply_rotate_code(src, rotate_code)

    method_map = {
        'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED,
        'TM_CCORR_NORMED':  cv2.TM_CCORR_NORMED,
        'TM_SQDIFF_NORMED': cv2.TM_SQDIFF_NORMED,
    }
    method = method_map.get(method_str, cv2.TM_CCOEFF_NORMED)
    all_pass, results, rule_results, vis = run_inspection_frame(src, pid, method, draw_vis=True)
    stamp_text = 'PASS' if all_pass else 'FAIL'
    result_b64 = cv2_to_b64(vis)

    conn = get_db()
    conn.execute('INSERT INTO inference_logs (product_id, result_json, pass_fail) VALUES (?,?,?)',
                 (pid, json.dumps(results, ensure_ascii=False), stamp_text))
    conn.commit()
    conn.close()

    return jsonify({'pass': all_pass, 'results': results, 'rules': rule_results, 'result_img': result_b64})


@app.route('/api/video/delete', methods=['POST'])
def video_delete():
    """清理上傳影片；Windows 解碼器尚未釋放時保留 token 供重試。"""
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    path = _video_session_get(token)
    if not path:
        return jsonify({'ok': True, 'deleted': False})
    try:
        if os.path.exists(path):
            os.remove(path)
        _video_session_pop(token)
        return jsonify({'ok': True, 'deleted': True})
    except PermissionError:
        return jsonify({'ok': False, 'error': '影片仍由解碼器使用中，請稍後重試'}), 409
    except OSError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/video/stream_infer')
def video_stream_infer():
    """Server-Sent Events：從 start_time 開始，對影片每一幀做推論，
    結果即時串流回前端。
    Query params: token, start (秒), product_id, method, infer_fps (推論幀率，預設原始fps)
    """
    token      = request.args.get('token', '')
    start_time = float(request.args.get('start', 0))
    pid        = int(request.args.get('product_id', 0))
    method_str = request.args.get('method', 'TM_CCOEFF_NORMED')
    # infer_fps: 推論速率，0 = 跟隨影片原始 fps；>0 = 降頻（例如 2 = 每秒 2 幀）
    infer_fps  = float(request.args.get('infer_fps', 0))

    path = _video_session_get(token)
    if not path or not os.path.exists(path):
        def err():
            yield 'data: ' + json.dumps({'error': '影片 token 無效'}) + '\n\n'
        return Response(stream_with_context(err()), mimetype='text/event-stream')

    method_map = {
        'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED,
        'TM_CCORR_NORMED':  cv2.TM_CCORR_NORMED,
        'TM_SQDIFF_NORMED': cv2.TM_SQDIFF_NORMED,
    }
    method = method_map.get(method_str, cv2.TM_CCOEFF_NORMED)

    conn    = get_db()
    regions = conn.execute('SELECT * FROM regions WHERE product_id=?', (pid,)).fetchall()
    conn.close()

    def generate():
        cap = cv2.VideoCapture(path)
        vid_fps = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0)
        total_frames = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        rotate_code = vc.get_video_rotate_code(cap)
        duration  = total_frames / vid_fps

        # 決定實際推論間隔（幀數）
        if infer_fps > 0 and infer_fps < vid_fps:
            frame_step = max(1, int(round(vid_fps / infer_fps)))
        else:
            frame_step = 1   # 每幀都推論

        start_frame = int(round(start_time * vid_fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        # 送出影片基本資訊
        yield 'data: ' + json.dumps({
            'type': 'info',
            'fps': vid_fps, 'total_frames': total_frames,
            'duration': duration, 'frame_step': frame_step,
        }) + '\n\n'

        frame_idx = start_frame
        while True:
            ok, src = cap.read()
            if not ok or src is None:
                break
            src = vc.apply_rotate_code(src, rotate_code)

            cur_time = frame_idx / vid_fps
            all_pass, results, rule_results, vis = run_inspection_frame(src, pid, method, draw_vis=True)

            # JPEG 品質 85，比 PNG 小很多，串流更快
            # JPEG 品質 85，比 PNG 小很多，串流更快
            _, buf = cv2.imencode('.jpg', vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
            result_b64 = 'data:image/jpeg;base64,' + base64.b64encode(buf).decode()

            payload = {
                'type':       'frame',
                'frame':      frame_idx,
                'time':       round(cur_time, 3),
                'pass':       all_pass,
                'results':    results,
                'rules':      rule_results,
                'result_img': result_b64,
            }
            yield 'data: ' + json.dumps(payload) + '\n\n'

            # 跳到下一個要推論的幀
            frame_idx += frame_step
            if frame_idx >= total_frames:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

        cap.release()

        # 串流結束
        yield 'data: ' + json.dumps({'type': 'done'}) + '\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',   # 關閉 nginx 緩衝（若有反向代理）
        }
    )


# ──────────────────────────────────────────────
# Logs API
# ──────────────────────────────────────────────
@app.route('/api/logs', methods=['GET'])
def get_logs():
    limit = int(request.args.get('limit', 50))
    conn  = get_db()
    rows  = conn.execute(
        '''SELECT l.id, l.product_id, p.serial, p.name,
                  l.pass_fail, l.result_json, l.raw_image_path, l.result_image_path,
                  COALESCE(l.storage_status,'NONE') AS storage_status, l.created_at
           FROM inference_logs l
           LEFT JOIN products p ON l.product_id=p.id
           ORDER BY l.id DESC LIMIT ?''', (limit,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/logs/<int:lid>', methods=['GET'])
def get_log_detail(lid):
    """單筆 log 詳情，含 result_json 完整解析。"""
    conn = get_db()
    row  = conn.execute(
        '''SELECT l.*, p.serial, p.name
           FROM inference_logs l
           LEFT JOIN products p ON l.product_id=p.id
           WHERE l.id=?''', (lid,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '找不到此記錄'}), 404
    d = dict(row)
    try:
        d['results'] = json.loads(d.get('result_json') or '[]')
    except Exception:
        d['results'] = []
    return jsonify(d)


@app.route('/api/logs/<int:lid>/image', methods=['GET'])
def get_log_image(lid):
    """Return archived raw/result image file for a log."""
    img_type = (request.args.get('type') or 'result').lower()
    col = 'raw_image_path' if img_type == 'raw' else 'result_image_path'
    conn = get_db()
    row = conn.execute(f'SELECT {col} AS image_path FROM inference_logs WHERE id=?', (lid,)).fetchone()
    conn.close()
    if not row or not row['image_path']:
        return jsonify({'error': '此記錄沒有保存影像'}), 404
    raw_path = str(row['image_path'] or '')
    # v4: HTTP storage server 會回傳 http://.../files/...，管理端直接 redirect 給瀏覽器讀取。
    if raw_path.startswith('http://') or raw_path.startswith('https://'):
        return redirect(raw_path, code=302)
    path = os.path.abspath(raw_path)
    if not os.path.exists(path) or not os.path.isfile(path):
        return jsonify({'error': '影像檔不存在或已被清除'}), 404
    return send_file(path, mimetype='image/jpeg')


@app.route('/api/logs/rotate', methods=['POST'])
def manual_rotate_logs():
    data = request.json or {}
    days = int(data.get('retain_days', LOG_RETAIN_DAYS))
    if days < 0:
        return jsonify({'error': 'retain_days 不得為負數'}), 400
    deleted = rotate_logs(retain_days=days)
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM inference_logs').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted, 'retain_days': days, 'remaining': total})


@app.route('/api/logs/stats', methods=['GET'])
def log_stats():
    conn = get_db()
    total  = conn.execute('SELECT COUNT(*) FROM inference_logs').fetchone()[0]
    oldest = conn.execute('SELECT MIN(created_at) FROM inference_logs').fetchone()[0]
    newest = conn.execute('SELECT MAX(created_at) FROM inference_logs').fetchone()[0]
    rows   = conn.execute(
        'SELECT p.serial, p.name, COUNT(*) as cnt,'
        ' SUM(CASE WHEN l.pass_fail="PASS" THEN 1 ELSE 0 END) as pass_cnt,'
        ' SUM(CASE WHEN l.pass_fail="FAIL" THEN 1 ELSE 0 END) as fail_cnt'
        ' FROM inference_logs l LEFT JOIN products p ON l.product_id=p.id'
        ' GROUP BY l.product_id ORDER BY cnt DESC'
    ).fetchall()
    conn.close()
    return jsonify({
        'total': total, 'oldest': oldest, 'newest': newest,
        'retain_days': LOG_RETAIN_DAYS,
        'by_product': [dict(r) for r in rows],
    })


# ──────────────────────────────────────────────
# SOP Flow configuration API
# ──────────────────────────────────────────────
SOP_DEFAULT_CONFIG = {
    'enabled': False, 'strict_order': True, 'completion_mode': 'ALL_REQUIRED',
    'auto_reset_mode': 'MANUAL', 'idle_reset_sec': 0.0,
    'alarm_on_skip': True, 'alarm_on_timeout': True, 'alarm_latch': True,
    'web_alarm_enabled': True, 'tower_light_enabled': False,
    'tower_green_channel': 1, 'tower_yellow_channel': 2, 'tower_red_channel': 3,
    'station_name': '',
}

def _normalize_sop_config(data=None):
    data = data or {}
    def _b(k): return bool(data.get(k, SOP_DEFAULT_CONFIG[k]))
    def _ch(k):
        try: return max(1, min(4, int(data.get(k, SOP_DEFAULT_CONFIG[k]))))
        except Exception: return SOP_DEFAULT_CONFIG[k]
    try: idle = max(0.0, min(86400.0, float(data.get('idle_reset_sec', 0) or 0)))
    except Exception: idle = 0.0
    mode = str(data.get('auto_reset_mode', 'MANUAL') or 'MANUAL').upper()
    if mode not in ('MANUAL', 'ON_PASS_DELAY', 'ON_EXTERNAL_TRIGGER'): mode = 'MANUAL'
    channels = [_ch('tower_green_channel'), _ch('tower_yellow_channel'), _ch('tower_red_channel')]
    if len(set(channels)) != 3:
        channels = [1, 2, 3]
    return {
        'enabled': _b('enabled'), 'strict_order': _b('strict_order'),
        'completion_mode': 'ALL_REQUIRED', 'auto_reset_mode': mode,
        'idle_reset_sec': idle, 'alarm_on_skip': _b('alarm_on_skip'),
        'alarm_on_timeout': _b('alarm_on_timeout'), 'alarm_latch': _b('alarm_latch'),
        'web_alarm_enabled': _b('web_alarm_enabled'),
        'tower_light_enabled': _b('tower_light_enabled'),
        'tower_green_channel': channels[0],
        'tower_yellow_channel': channels[1],
        'tower_red_channel': channels[2],
        'station_name': str(data.get('station_name', '') or '').strip()[:80],
    }

def _load_sop_config(conn, pid):
    row = conn.execute('SELECT * FROM product_sop_config WHERE product_id=?', (pid,)).fetchone()
    cfg = dict(SOP_DEFAULT_CONFIG)
    if row:
        cfg.update(dict(row))
    return _normalize_sop_config(cfg)

def _save_sop_config(conn, pid, data):
    cfg = _normalize_sop_config(data)
    conn.execute('''
        INSERT INTO product_sop_config
        (product_id, enabled, strict_order, completion_mode, auto_reset_mode, idle_reset_sec,
         alarm_on_skip, alarm_on_timeout, alarm_latch, web_alarm_enabled, tower_light_enabled,
         tower_green_channel, tower_yellow_channel, tower_red_channel, station_name, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
        ON CONFLICT(product_id) DO UPDATE SET
          enabled=excluded.enabled, strict_order=excluded.strict_order,
          completion_mode=excluded.completion_mode, auto_reset_mode=excluded.auto_reset_mode,
          idle_reset_sec=excluded.idle_reset_sec, alarm_on_skip=excluded.alarm_on_skip,
          alarm_on_timeout=excluded.alarm_on_timeout, alarm_latch=excluded.alarm_latch,
          web_alarm_enabled=excluded.web_alarm_enabled, tower_light_enabled=excluded.tower_light_enabled,
          tower_green_channel=excluded.tower_green_channel, tower_yellow_channel=excluded.tower_yellow_channel,
          tower_red_channel=excluded.tower_red_channel, station_name=excluded.station_name,
          updated_at=datetime('now','localtime')
    ''', (pid, int(cfg['enabled']), int(cfg['strict_order']), cfg['completion_mode'],
          cfg['auto_reset_mode'], cfg['idle_reset_sec'], int(cfg['alarm_on_skip']),
          int(cfg['alarm_on_timeout']), int(cfg['alarm_latch']), int(cfg['web_alarm_enabled']),
          int(cfg['tower_light_enabled']), cfg['tower_green_channel'], cfg['tower_yellow_channel'],
          cfg['tower_red_channel'], cfg['station_name']))
    return cfg

@app.route('/api/products/<int:pid>/sop-config', methods=['GET'])
def get_product_sop_config(pid):
    conn = get_db()
    try:
        return jsonify({'ok': True, **_load_sop_config(conn, pid)})
    finally:
        conn.close()

@app.route('/api/products/<int:pid>/sop-config', methods=['POST'])
def save_product_sop_config(pid):
    conn = get_db()
    try:
        cfg = _save_sop_config(conn, pid, request.get_json(silent=True) or {})
        conn.commit()
        return jsonify({'ok': True, **cfg})
    finally:
        conn.close()

@app.route('/api/products/<int:pid>/sop-definition', methods=['GET'])
def get_sop_definition(pid):
    conn = get_db()
    try:
        import packaging_cycle as pc
        return jsonify({'ok': True, 'packaging': pc.load_settings(conn, pid), 'final_logic_mode': _get_product_final_logic_mode(conn, pid),
                        'config': _load_sop_config(conn, pid),
                        'steps': _load_inspection_items(conn, pid, enabled_only=False)})
    finally:
        conn.close()

@app.route('/api/products/<int:pid>/sop-definition', methods=['POST'])
def save_sop_definition(pid):
    """Atomically save SOP steps/templates and product-level flow settings."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        import packaging_cycle as pc
        previous = pc.load_settings(conn, pid)
        if previous['enabled'] and 'packaging' not in data:
            raise ValueError('影像包裝已啟用，請使用新版流程設定一併儲存循環條件')
        pack = pc.validate_settings(conn, pid, data.get('packaging', previous), data.get('steps', []),
                                    bool((data.get('config') or {}).get('enabled')))
        result = _save_inspection_items_data(conn, pid, data)
        pc.save_settings(conn, pid, pack)
        cfg = _save_sop_config(conn, pid, data.get('config') or {})
        conn.commit()
        result.update({'ok': True, 'config': cfg})
        return jsonify(result)
    except LookupError as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 404
    except (ValueError, TypeError, OverflowError) as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        app.logger.exception('save SOP definition failed')
        return jsonify({'ok': False, 'error': f'儲存 SOP Definition 失敗：{e}'}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────
# Monitor connection config API
# ──────────────────────────────────────────────
@app.route('/api/monitor-config', methods=['GET'])
def get_monitor_config():
    return jsonify(load_monitor_config())


@app.route('/api/monitor-config', methods=['POST'])
def update_monitor_config():
    data = request.json or {}
    try:
        cfg = save_monitor_config(data)
        return jsonify({'ok': True, **cfg})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400




@app.route('/api/vision-config', methods=['GET'])
def get_vision_config():
    return jsonify(load_vision_config())


@app.route('/api/vision-config', methods=['POST'])
def update_vision_config():
    try:
        cfg = save_vision_config(request.json or {})
        return jsonify({'ok': True, **cfg})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


# ═══════════════════════════════════════════════════════════════════
# PC Video / Multi-Stream Runtime
# ═══════════════════════════════════════════════════════════════════
METHOD_MAP_PC = {
    'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED,
    'TM_CCORR_NORMED': cv2.TM_CCORR_NORMED,
    'TM_SQDIFF_NORMED': cv2.TM_SQDIFF_NORMED,
}


def _source_value(uri, source_type='RTSP'):
    value = str(uri or '').strip()
    if str(source_type or '').upper() == 'USB' or value.isdigit():
        try:
            return int(value)
        except Exception:
            return value
    return value


def _jpeg_data_url(frame, quality=75):
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        return ''
    return 'data:image/jpeg;base64,' + base64.b64encode(buf).decode()


def _load_product_dict(pid):
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT id, serial, name, reference_img_b64 FROM products WHERE id=?',
            (int(pid),),
        ).fetchone()
        if not row:
            return None
        product = dict(row)
        ref_b64 = product.pop('reference_img_b64', None)
        product['reference_width'] = 0
        product['reference_height'] = 0
        if ref_b64:
            try:
                ref = b64_to_cv2(ref_b64)
                if ref is not None and ref.size:
                    product['reference_height'], product['reference_width'] = ref.shape[:2]
            except Exception:
                pass
        return product
    finally:
        conn.close()


def _prepare_frame(frame, product):
    """Normalize a source frame to the labeling coordinate system."""
    if frame is None:
        return frame
    target_w = int((product or {}).get('reference_width') or 0)
    target_h = int((product or {}).get('reference_height') or 0)
    if target_w > 0 and target_h > 0 and (frame.shape[1] != target_w or frame.shape[0] != target_h):
        frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
    # Keep PC runtime consistent with the transform used when the video frame was labeled.
    return vc.apply_digital_zoom(frame, load_vision_config())


def _prepare_edge_frame(frame, product):
    """Keep the camera's real resolution; Edge scales saved templates instead."""
    del product
    return vc.apply_digital_zoom(frame, load_vision_config())


def _open_capture(source, source_type='RTSP'):
    """Open capture with bounded FFmpeg network timeouts when supported."""
    stype = str(source_type or '').upper()
    if stype in ('RTSP', 'HTTP') and isinstance(source, str):
        params = []
        if hasattr(cv2, 'CAP_PROP_OPEN_TIMEOUT_MSEC'):
            params += [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000]
        if hasattr(cv2, 'CAP_PROP_READ_TIMEOUT_MSEC'):
            params += [cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000]
        if params:
            try:
                return cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
            except Exception:
                pass
    return cv2.VideoCapture(source)


class StreamWorker:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.id = int(self.cfg['id'])
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._result = {
            'stream_id': self.id, 'name': self.cfg.get('name'), 'status': 'STOPPED',
            'error': '', 'ts': '', 'fps': 0.0, 'frame_seq': 0, 'result_img': '',
            'sop': None, 'product': None,
        }
        self.cache_mgr = None
        self.engine = None
        self._logged_sessions = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f'stream-{self.id}')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6.0)
        alive = bool(self._thread and self._thread.is_alive())
        if self.cache_mgr:
            try:
                self.cache_mgr.stop_watcher()
            except Exception:
                pass
        self.cache_mgr = None
        if alive:
            self._set(status='STOPPING', error='等待影像讀取逾時結束')
        else:
            self._set(status='STOPPED')
        return not alive

    def _persist_sop_summary(self, summary, result=None):
        if not summary or not summary.get('session_id'):
            return
        session_id = str(summary['session_id'])
        if session_id in self._logged_sessions:
            return
        result = result or ('PASS' if summary.get('complete') else 'FAIL')
        conn = get_db()
        try:
            payload = {
                'stream_id': self.id,
                'stream_name': self.cfg.get('name'),
                'sop': summary,
            }
            alarm = summary.get('alarm') or {}
            conn.execute('''
                INSERT INTO sop_run_logs
                (product_id, session_id, result, alarm_code, alarm_message,
                 step_state_json, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ''', (
                int(self.cfg['product_id']), session_id, result,
                str(alarm.get('code') or ''), str(alarm.get('message') or ''),
                json.dumps(payload, ensure_ascii=False),
                str(summary.get('started_at') or ''),
            ))
            conn.commit()
            self._logged_sessions.add(session_id)
        finally:
            conn.close()

    def reset_sop(self):
        with self._lock:
            if self.engine:
                previous = self.engine.summary()
                if previous.get('started_at') and not previous.get('complete'):
                    self._persist_sop_summary(previous, result='ABORTED')
                summary = self.engine.reset(reason='API_RESET')
                self._result['sop'] = summary
                return summary
        return None

    def finish_sop(self):
        with self._lock:
            if self.engine:
                summary = self.engine.finish()
                self._result['sop'] = summary
                self._persist_sop_summary(summary, result='PASS' if summary.get('complete') else 'FAIL')
                return summary
        return None

    def acknowledge_sop(self):
        with self._lock:
            if self.engine:
                summary = self.engine.acknowledge_alarm()
                self._result['sop'] = summary
                return summary
        return None

    def get(self, include_image=True):
        with self._lock:
            result = json.loads(json.dumps(self._result, ensure_ascii=False))
        if not include_image:
            result['has_image'] = bool(result.get('result_img'))
            result.pop('result_img', None)
        return result

    def _set(self, **kwargs):
        with self._lock:
            self._result.update(kwargs)

    def _prepare_runtime(self):
        pid = int(self.cfg['product_id'])
        product = _load_product_dict(pid)
        if not product:
            raise RuntimeError(f'product_id={pid} 不存在')
        mgr = vc.CacheManager(DB_PATH, pid, reload_interval=5.0)
        if not mgr.initial_load():
            raise RuntimeError('產品尚未建立有效 template / SOP step')
        mgr.start_watcher()
        steps = vc.load_runtime_inspection_items(DB_PATH, pid)
        sop_cfg = vc.load_product_sop_config(DB_PATH, pid)
        engine = vc.SopFlowEngine(steps, sop_cfg) if sop_cfg.get('enabled') and steps else None
        self.cache_mgr = mgr
        self.engine = engine
        self._set(product=product, sop=engine.summary() if engine else None)

    def _run(self):
        try:
            self._prepare_runtime()
        except Exception as exc:
            self._set(status='ERROR', error=str(exc))
            return

        source_type = str(self.cfg.get('source_type') or 'RTSP').upper()
        source = _source_value(self.cfg.get('source_uri'), source_type)
        infer_fps = max(0.1, float(self.cfg.get('infer_fps') or 2.0))
        reconnect_sec = max(0.5, float(self.cfg.get('reconnect_sec') or 3.0))
        loop_video = bool(self.cfg.get('loop_video'))
        infer_interval = 1.0 / infer_fps
        is_video_file = source_type == 'VIDEO'
        frame_seq = 0
        method = cv2.TM_CCOEFF_NORMED
        cap = None
        rotate_code = None
        next_infer = 0.0
        video_next_deadline = 0.0
        video_fps = 0.0
        fps_count = 0
        fps_start = _time.monotonic()
        shown_fps = 0.0

        while not self._stop.is_set():
            try:
                if cap is None or not cap.isOpened():
                    self._set(status='CONNECTING', error='')
                    cap = _open_capture(source, source_type)
                    if not cap.isOpened():
                        self._set(status='RECONNECTING', error='無法開啟串流')
                        if self.engine:
                            self.engine.interrupt(reason='OPEN_FAILED')
                        try:
                            cap.release()
                        except Exception:
                            pass
                        cap = None
                        self._stop.wait(reconnect_sec)
                        continue
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass
                    video_fps = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0) if is_video_file else 0.0
                    rotate_code = vc.get_video_rotate_code(cap)
                    video_next_deadline = _time.monotonic()
                    self._set(status='LIVE', error='')
                    next_infer = 0.0

                ok, frame = cap.read()
                if not ok or frame is None:
                    if is_video_file:
                        if loop_video:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            video_next_deadline = _time.monotonic()
                            if self.engine:
                                self.engine.reset(reason='VIDEO_LOOP')
                            continue
                        # A file reaching EOF is not a network failure and must not be reopened.
                        final_sop = self.engine.finish() if self.engine else None
                        if final_sop:
                            self._persist_sop_summary(
                                final_sop,
                                result='PASS' if final_sop.get('complete') else 'FAIL',
                            )
                            self._set(sop=final_sop)
                        self._set(status='ENDED', error='影片播放完成')
                        break
                    self._set(status='RECONNECTING', error='影像中斷，等待重新連線')
                    if self.engine:
                        self.engine.interrupt(reason='SOURCE_INTERRUPTED')
                    cap.release()
                    cap = None
                    self._stop.wait(reconnect_sec)
                    continue

                frame = vc.apply_rotate_code(frame, rotate_code)

                # A VIDEO source is presented at its media rate instead of being consumed
                # as fast as OpenCV can decode it.
                if is_video_file and video_fps > 0:
                    now_mono = _time.monotonic()
                    wait_sec = video_next_deadline - now_mono
                    if wait_sec > 0 and self._stop.wait(wait_sec):
                        break
                    video_next_deadline = max(video_next_deadline + 1.0 / video_fps, _time.monotonic())

                now_mono = _time.monotonic()
                if now_mono < next_infer:
                    continue
                next_infer = now_mono + infer_interval
                cache = self.cache_mgr.get() if self.cache_mgr else None
                if cache is None:
                    raise RuntimeError('template cache unavailable')

                product = self._result.get('product') or {}
                prepared = _prepare_frame(frame, product)
                frame_pass, results, vis = vc.run_inference(
                    prepared,
                    cache,
                    method=method,
                    draw_vis=True,
                )
                rules = getattr(cache, 'last_rule_results', []) or []
                sop = self.engine.update(rules) if self.engine else None
                if sop and sop.get('complete'):
                    self._persist_sop_summary(sop, result='PASS')
                if vis is None:
                    vis = prepared.copy()
                state = (sop or {}).get('state') or ('FRAME_PASS' if frame_pass else 'FRAME_WAIT')
                cv2.putText(
                    vis,
                    f"{self.cfg.get('name', 'STREAM')} | {product.get('serial', '')} | {state}",
                    (12, max(24, vis.shape[0] - 18)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 230, 120) if state == 'COMPLETE' else ((0, 40, 255) if state == 'ALARM' else (0, 190, 255)),
                    2,
                    cv2.LINE_AA,
                )
                frame_seq += 1
                fps_count += 1
                if now_mono - fps_start >= 2.0:
                    shown_fps = fps_count / max(0.001, now_mono - fps_start)
                    fps_start, fps_count = now_mono, 0
                verdict = (
                    'COMPLETE' if sop and sop.get('complete')
                    else 'ALARM' if sop and sop.get('alarm', {}).get('active')
                    else 'PASS' if frame_pass else 'RUNNING'
                )
                self._set(
                    status='LIVE',
                    error='',
                    ts=datetime.now().isoformat(timespec='milliseconds'),
                    fps=round(shown_fps, 2),
                    frame_seq=frame_seq,
                    frame_pass=bool(frame_pass),
                    verdict=verdict,
                    results=results,
                    rules=rules,
                    sop=sop,
                    result_img=_jpeg_data_url(vis, 75),
                )
            except Exception as exc:
                self._set(status='ERROR', error=str(exc))
                traceback.print_exc()
                if self.engine:
                    self.engine.interrupt(reason='RUNTIME_ERROR')
                if cap:
                    try:
                        cap.release()
                    except Exception:
                        pass
                cap = None
                self._stop.wait(reconnect_sec)

        if cap:
            try:
                cap.release()
            except Exception:
                pass
        if self._result.get('status') != 'ENDED':
            self._set(status='STOPPED')


def _mask_source_uri(uri):
    value = str(uri or '')
    # Hide credentials in dashboard/status payloads while keeping the edit API unchanged.
    try:
        import urllib.parse
        parsed = urllib.parse.urlsplit(value)
        if parsed.username is None:
            return value
        host = parsed.hostname or ''
        if parsed.port:
            host += f':{parsed.port}'
        netloc = f'{parsed.username}:***@{host}'
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return value

class MultiStreamManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.workers = {}

    def configs(self):
        conn = get_db()
        try:
            return [dict(x) for x in conn.execute('''
                SELECT s.*, p.serial AS product_serial, p.name AS product_name
                FROM stream_sources s LEFT JOIN products p ON p.id=s.product_id
                ORDER BY s.id
            ''').fetchall()]
        finally:
            conn.close()

    def reload(self):
        cfgs = self.configs()
        with self._lock:
            old = self.workers
            self.workers = {}
        for worker in old.values():
            worker.stop()
        with self._lock:
            for cfg in cfgs:
                if cfg.get('enabled'):
                    worker = StreamWorker(cfg)
                    self.workers[int(cfg['id'])] = worker
                    worker.start()
        return self.status()

    def reload_one(self, stream_id):
        sid = int(stream_id)
        conn = get_db()
        try:
            row = conn.execute('SELECT * FROM stream_sources WHERE id=?', (sid,)).fetchone()
            cfg = dict(row) if row else None
        finally:
            conn.close()
        with self._lock:
            old = self.workers.pop(sid, None)
        if old and not old.stop():
            # Do not start a duplicate reader while the previous backend call is still blocked.
            with self._lock:
                self.workers[sid] = old
            return old.get()
        if cfg and cfg.get('enabled'):
            worker = StreamWorker(cfg)
            with self._lock:
                self.workers[sid] = worker
            worker.start()
        return self.worker(sid).get() if self.worker(sid) else None

    def reload_product(self, product_id):
        pid = int(product_id)
        conn = get_db()
        try:
            ids = [int(r['id']) for r in conn.execute(
                'SELECT id FROM stream_sources WHERE product_id=?', (pid,)
            ).fetchall()]
        finally:
            conn.close()
        for sid in ids:
            self.reload_one(sid)
        return self.status()

    def remove(self, stream_id):
        sid = int(stream_id)
        with self._lock:
            old = self.workers.pop(sid, None)
        if old:
            old.stop()

    def stop_all(self):
        with self._lock:
            workers = list(self.workers.values())
            self.workers = {}
        for worker in workers:
            worker.stop()

    def status(self, include_images=True):
        cfgs = self.configs()
        with self._lock:
            workers = dict(self.workers)
        output = []
        for cfg in cfgs:
            sid = int(cfg['id'])
            if sid in workers:
                result = workers[sid].get(include_image=include_images)
            else:
                result = {
                    'stream_id': sid, 'name': cfg.get('name'),
                    'status': 'DISABLED' if not cfg.get('enabled') else 'STOPPED',
                    'error': '', 'sop': None, 'result_img': '',
                }
            public_cfg = dict(cfg)
            masked_uri = _mask_source_uri(public_cfg.get('source_uri'))
            # Runtime status is visible on the monitoring dashboard. Never expose
            # RTSP/HTTP credentials there; the configuration endpoint remains the
            # only place that returns the editable URI.
            public_cfg['source_uri'] = masked_uri
            public_cfg['source_uri_masked'] = masked_uri
            result['config'] = public_cfg
            output.append(result)
        return output

    def worker(self, stream_id):
        with self._lock:
            return self.workers.get(int(stream_id))


STREAM_MANAGER = MultiStreamManager()
atexit.register(STREAM_MANAGER.stop_all)


STREAM_SOURCE_TYPES = {'RTSP', 'HTTP', 'USB', 'VIDEO'}


def _parse_stream_config(data, old=None):
    old = dict(old or {})
    name = str(data.get('name', old.get('name', '')) or '').strip()
    uri = str(data.get('source_uri', old.get('source_uri', '')) or '').strip()
    source_type = str(data.get('source_type', old.get('source_type', 'RTSP')) or 'RTSP').upper()
    if not name or not uri:
        raise ValueError('名稱與來源 URI 為必填')
    if source_type not in STREAM_SOURCE_TYPES:
        raise ValueError(f'不支援的來源類型：{source_type}')
    try:
        product_id = int(data.get('product_id', old.get('product_id', 0)) or 0)
    except Exception:
        raise ValueError('product_id 必須是整數')
    if not _load_product_dict(product_id):
        raise ValueError('指定的產品不存在')
    try:
        infer_fps = max(0.1, min(60.0, float(data.get('infer_fps', old.get('infer_fps', 2.0)))))
        reconnect_sec = max(0.5, min(300.0, float(data.get('reconnect_sec', old.get('reconnect_sec', 3.0)))))
    except Exception:
        raise ValueError('infer_fps／reconnect_sec 格式錯誤')
    if source_type == 'USB' and not uri.isdigit():
        raise ValueError('USB 來源 URI 應為攝影機編號，例如 0 或 1')
    if source_type == 'VIDEO' and not os.path.isfile(uri):
        raise ValueError('VIDEO 來源檔案不存在')
    if source_type == 'RTSP' and not uri.lower().startswith(('rtsp://', 'rtsps://')):
        raise ValueError('RTSP URI 必須以 rtsp:// 或 rtsps:// 開頭')
    if source_type == 'HTTP' and not uri.lower().startswith(('http://', 'https://')):
        raise ValueError('HTTP URI 必須以 http:// 或 https:// 開頭')
    return {
        'name': name,
        'source_uri': uri,
        'source_type': source_type,
        'product_id': product_id,
        'enabled': 1 if data.get('enabled', bool(old.get('enabled', 1))) else 0,
        'infer_fps': infer_fps,
        'reconnect_sec': reconnect_sec,
        'loop_video': 1 if data.get('loop_video', bool(old.get('loop_video', 0))) else 0,
    }


@app.route('/api/stream-sources', methods=['GET'])
def list_stream_sources():
    return jsonify(STREAM_MANAGER.configs())


@app.route('/api/stream-sources', methods=['POST'])
def create_stream_source():
    data = request.get_json(silent=True) or {}
    try:
        cfg = _parse_stream_config(data)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    conn = get_db()
    try:
        cur = conn.execute('''
            INSERT INTO stream_sources
            (name, source_uri, source_type, product_id, enabled, infer_fps, reconnect_sec, loop_video, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        ''', (
            cfg['name'], cfg['source_uri'], cfg['source_type'], cfg['product_id'],
            cfg['enabled'], cfg['infer_fps'], cfg['reconnect_sec'], cfg['loop_video'],
        ))
        conn.commit()
        stream_id = cur.lastrowid
    finally:
        conn.close()
    STREAM_MANAGER.reload_one(stream_id)
    return jsonify({'ok': True, 'id': stream_id}), 201


@app.route('/api/stream-sources/<int:stream_id>', methods=['PUT'])
def update_stream_source(stream_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        old_row = conn.execute('SELECT * FROM stream_sources WHERE id=?', (stream_id,)).fetchone()
        if not old_row:
            return jsonify({'ok': False, 'error': '找不到串流'}), 404
        try:
            cfg = _parse_stream_config(data, dict(old_row))
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        conn.execute('''
            UPDATE stream_sources
            SET name=?, source_uri=?, source_type=?, product_id=?, enabled=?, infer_fps=?,
                reconnect_sec=?, loop_video=?, updated_at=datetime('now','localtime')
            WHERE id=?
        ''', (
            cfg['name'], cfg['source_uri'], cfg['source_type'], cfg['product_id'],
            cfg['enabled'], cfg['infer_fps'], cfg['reconnect_sec'], cfg['loop_video'], stream_id,
        ))
        conn.commit()
    finally:
        conn.close()
    STREAM_MANAGER.reload_one(stream_id)
    return jsonify({'ok': True})


@app.route('/api/stream-sources/<int:stream_id>', methods=['DELETE'])
def delete_stream_source(stream_id):
    STREAM_MANAGER.remove(stream_id)
    conn = get_db()
    try:
        conn.execute('DELETE FROM stream_sources WHERE id=?', (stream_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/runtime/streams', methods=['GET'])
def runtime_streams():
    include_images = str(request.args.get('include_images', '1')).lower() not in ('0', 'false', 'no')
    return jsonify({'ok': True, 'streams': STREAM_MANAGER.status(include_images=include_images)})


@app.route('/api/runtime/streams/<int:stream_id>/image', methods=['GET'])
def runtime_stream_image(stream_id):
    worker = STREAM_MANAGER.worker(stream_id)
    if not worker:
        return Response(status=404)
    data_url = worker.get().get('result_img') or ''
    if not data_url or ',' not in data_url:
        return Response(status=204)
    try:
        raw = base64.b64decode(data_url.split(',', 1)[1])
    except Exception:
        return Response(status=500)
    return Response(
        raw,
        mimetype='image/jpeg',
        headers={'Cache-Control': 'no-store, max-age=0'},
    )


@app.route('/api/runtime/reload', methods=['POST'])
def runtime_reload():
    data = request.get_json(silent=True) or {}
    product_id = data.get('product_id')
    streams = STREAM_MANAGER.reload_product(product_id) if product_id else STREAM_MANAGER.reload()
    return jsonify({'ok': True, 'streams': streams})


@app.route('/api/runtime/streams/<int:stream_id>/reset', methods=['POST'])
def runtime_stream_reset(stream_id):
    worker = STREAM_MANAGER.worker(stream_id)
    if not worker:
        return jsonify({'ok': False, 'error': 'stream not running'}), 404
    return jsonify({'ok': True, 'sop': worker.reset_sop()})


@app.route('/api/runtime/streams/<int:stream_id>/finish', methods=['POST'])
def runtime_stream_finish(stream_id):
    worker = STREAM_MANAGER.worker(stream_id)
    if not worker:
        return jsonify({'ok': False, 'error': 'stream not running'}), 404
    return jsonify({'ok': True, 'sop': worker.finish_sop()})


@app.route('/api/runtime/streams/<int:stream_id>/ack', methods=['POST'])
def runtime_stream_ack(stream_id):
    worker = STREAM_MANAGER.worker(stream_id)
    if not worker:
        return jsonify({'ok': False, 'error': 'stream not running'}), 404
    return jsonify({'ok': True, 'sop': worker.acknowledge_sop()})


@app.route('/api/video/sop_stream_infer')
def video_sop_stream_infer():
    token = request.args.get('token', '')
    try:
        product_id = int(request.args.get('product_id', '0') or 0)
        start_time = max(0.0, float(request.args.get('start', '0') or 0))
        infer_fps = max(0.1, min(60.0, float(request.args.get('infer_fps', '2') or 2)))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'product_id／start／infer_fps 格式錯誤'}), 400
    method = METHOD_MAP_PC.get(
        request.args.get('method', 'TM_CCOEFF_NORMED'),
        cv2.TM_CCOEFF_NORMED,
    )
    path = _video_session_get(token)
    if not path or not os.path.exists(path):
        return jsonify({'error': '影片 token 無效或已過期'}), 404

    product = _load_product_dict(product_id)
    if not product:
        return jsonify({'error': '產品不存在'}), 404
    regions = vc.load_regions(DB_PATH, product_id)
    rule_groups = vc.load_rule_groups(DB_PATH, product_id)
    steps = vc.load_runtime_inspection_items(DB_PATH, product_id)
    cache = vc.TemplateCache(
        regions, rule_groups, steps,
        vc.load_product_final_logic_mode(DB_PATH, product_id),
    )
    sop_config = vc.load_product_sop_config(DB_PATH, product_id)
    engine = vc.SopFlowEngine(steps, sop_config) if sop_config.get('enabled') and steps else None

    def generate():
        cap = cv2.VideoCapture(path)
        fps = _safe_positive_float(cap.get(cv2.CAP_PROP_FPS), 25.0)
        total = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        rotate_code = vc.get_video_rotate_code(cap)
        raw_w = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        raw_h = _safe_nonnegative_int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        report_w, report_h = vc.rotated_frame_size(raw_w, raw_h, rotate_code)
        duration = total / fps if fps else 0
        frame_step = max(1, int(round(fps / infer_fps)))
        frame_index = int(round(start_time * fps))
        effective_start_time = start_time
        if total > 0:
            frame_index = max(0, min(total - 1, frame_index))
            effective_start_time = frame_index / fps
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        # The first processed frame must be elapsed=0 for this run even when the
        # user starts from a seek position. Otherwise a 60s seek can instantly
        # trigger every timeout.
        virtual_base = (engine.created_ts - effective_start_time) if engine else (_time.time() - effective_start_time)
        yield 'data: ' + json.dumps({
            'type': 'info', 'fps': fps, 'total_frames': total,
            'duration': duration, 'frame_step': frame_step, 'product': product,
        }, ensure_ascii=False) + '\n\n'
        frame_pass = False
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame = vc.apply_rotate_code(frame, rotate_code)
                current_time = frame_index / fps
                prepared = _prepare_frame(frame, product)
                frame_pass, results, vis = vc.run_inference(
                    prepared, cache, method=method, draw_vis=True,
                )
                rules = getattr(cache, 'last_rule_results', []) or []
                sop = engine.update(rules, now_ts=virtual_base + current_time) if engine else None
                if vis is None:
                    vis = frame
                verdict = (
                    'COMPLETE' if sop and sop.get('complete')
                    else 'ALARM' if sop and sop.get('alarm', {}).get('active')
                    else 'PASS' if frame_pass else 'RUNNING'
                )
                payload = {
                    'type': 'frame', 'frame': frame_index,
                    'time': round(current_time, 3),
                    'progress': round((frame_index / max(1, total)) * 100, 2),
                    'pass': bool((sop or {}).get('complete')) if engine else bool(frame_pass),
                    'frame_pass': bool(frame_pass), 'verdict': verdict,
                    'results': results, 'rules': rules, 'sop': sop,
                    'result_img': _jpeg_data_url(vis, 78),
                }
                yield 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
                next_index = frame_index + frame_step
                if total and next_index >= total:
                    break
                # Sequential grab avoids repeated random seeks on long-GOP H.264,
                # which can be both slow and frame-inaccurate.
                for _ in range(max(0, frame_step - 1)):
                    if not cap.grab():
                        next_index = total or next_index
                        break
                frame_index = next_index
            if engine:
                current_summary = engine.summary()
                final_sop = current_summary if current_summary.get('complete') else engine.finish()
                final_pass = bool(final_sop.get('complete'))
                final_verdict = 'COMPLETE' if final_pass else 'INCOMPLETE'
            else:
                final_sop = None
                final_pass = bool(frame_pass)
                final_verdict = 'PASS' if final_pass else 'FAIL'
            yield 'data: ' + json.dumps({
                'type': 'done', 'sop': final_sop, 'pass': final_pass,
                'verdict': final_verdict, 'progress': 100.0,
            }, ensure_ascii=False) + '\n\n'
        except GeneratorExit:
            pass
        finally:
            cap.release()

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/video-label')
def video_label_page():
    return send_from_directory('static', 'video_label.html')


@app.route('/offline-infer')
def offline_infer_page():
    return send_from_directory('static', 'offline_infer.html')


@app.route('/multi-stream')
def multi_stream_page():
    return send_from_directory('static', 'multi_stream.html')


# ──────────────────────────────────────────────
# Serve frontend
# ──────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/monitor')
def monitor_page():
    return send_from_directory('static', 'monitor.html')

@app.route('/sop-monitor')
def sop_monitor_page():
    return send_from_directory('static', 'sop_monitor.html')

@app.route('/sop-config')
def sop_config_page():
    return send_from_directory('static', 'sop_config.html')


@app.route('/flow-studio')
def flow_studio_page():
    return send_from_directory('static', 'flow_studio.html')


@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TM-Inspect Flask App')
    parser.add_argument('--host', default=os.environ.get('TM_INSPECT_HOST', '0.0.0.0'), help='Bind host')
    parser.add_argument('--port', type=int, default=int(os.environ.get('TM_INSPECT_PORT', '5000')), help='Bind port')
    parser.add_argument('--https', action='store_true', help='Enable HTTPS')
    parser.add_argument('--ssl-cert', default=os.environ.get('TM_INSPECT_SSL_CERT', ''), help='HTTPS certificate file')
    parser.add_argument('--ssl-key', default=os.environ.get('TM_INSPECT_SSL_KEY', ''), help='HTTPS private key file')
    parser.add_argument(
        '--debug',
        action='store_true',
        default=os.environ.get('TM_INSPECT_DEBUG', '').lower() in ('1', 'true', 'yes', 'on'),
        help='Enable Flask debug traceback output temporarily',
    )
    args = parser.parse_args()
    STREAM_MANAGER.reload()

    ssl_context = None
    scheme = 'http'

    if args.https:
        if not args.ssl_cert or not args.ssl_key:
            raise SystemExit('HTTPS 啟動需要 --ssl-cert 與 --ssl-key')
        if not os.path.exists(args.ssl_cert):
            raise SystemExit(f'找不到 SSL 憑證: {args.ssl_cert}')
        if not os.path.exists(args.ssl_key):
            raise SystemExit(f'找不到 SSL 私鑰: {args.ssl_key}')
        ssl_context = (args.ssl_cert, args.ssl_key)
        scheme = 'https'

    print(f'Template Matching Inspection App running on {scheme}://{args.host}:{args.port}')
    if args.debug:
        print('WARNING: Flask debug mode is enabled. Use only for temporary local troubleshooting.')
    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=False,
        threaded=True,
        ssl_context=ssl_context,
    )
