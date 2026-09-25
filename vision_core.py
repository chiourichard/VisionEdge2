#!/usr/bin/env python3
"""
infer_runner.py — 正式環境即時推論程式（含 ZLAN6042 DO 輸出）
=====================================================================
與 app.py（使用者標注端）共用同一個 SQLite DB，只讀取 templates，
不提供任何標注、編輯功能。

輸入來源（三擇一）：
  1. RTSP / 影片 / USB 攝影機   --source rtsp://... / /path/to.mp4 / 0
  2. Qualcomm GStreamer 攝影機   --source gst --camera 0
     （QCS5430/QCS6490 專用：qtiqmmfsrc → NV12 → appsink）

ZLAN6042 整合（可選）：
  推論結果 PASS → 脈衝 DO1（OK 信號）
  推論結果 FAIL → 脈衝 DO2（NG 信號）
  觸發模式（--zlan-trigger）：
    polling  預設。輪詢 DI1，上升觸發時拍照推論一次，Low 後重新待命
    free     自由運行，每幀直接推論後輸出信號

使用方式：
  # 純推論（無硬體信號）
  python infer_runner.py --product SN-001 --source gst --camera 0 --http

  # 搭配 ZLAN6042，polling 模式（DI1 觸發）
  python infer_runner.py --product SN-001 --source gst --camera 0 \
      --zlan-ip 192.168.1.200 --zlan-port 502 --zlan-trigger polling --http

  # 搭配 ZLAN6042，free 模式（連續推論，每幀輸出 DO）
  python infer_runner.py --product SN-001 --source rtsp://... \
      --zlan-ip 192.168.1.200 --zlan-trigger free --fps 2 --http

  # 使用模擬伺服器測試（搭配 mock_zlan_server.py）
  python infer_runner.py --product SN-001 --source gst \
      --zlan-ip 127.0.0.1 --zlan-port 5020 \
      --zlan-invert-di false --zlan-trigger polling --http

必要參數：
  --product     產品序號（Serial），對應 products.serial
  --source      影像來源：RTSP URL / 影片路徑 / 攝影機 index / 'gst'

選用參數（通用）：
  --db          DB 路徑（預設 ./db/inspection.db）
  --method      TM_CCOEFF_NORMED | TM_CCORR_NORMED | TM_SQDIFF_NORMED
  --fps         推論幀率上限（預設跟隨來源，0 = 不限制）
  --show        顯示即時視窗（需 GUI，僅 OpenCV 來源支援）
  --output      輸出結果影片路徑（.mp4，僅 OpenCV 來源支援）
  --enable-log  寫入 inference_logs（預設不寫入，開啟才存）
  --save-fail   FAIL 截圖儲存目錄
  --http        啟動 API server（預設 HTTP；正式環境請搭配 --https）
  --https       API server 使用 HTTPS（需 --ssl-cert 與 --ssl-key）
  --http-port   API server port（預設 8765）
  --ssl-cert   HTTPS 憑證檔 PEM/CRT
  --ssl-key    HTTPS 私鑰檔 KEY
  --live-fps    待機 live preview 最大更新 FPS（預設 5）
  --live-jpeg-quality 待機 live preview JPEG 品質（預設 70）

GStreamer 模式專用：
  --camera      攝影機 index（預設 0）
  --gst-width   寬度（預設 1920）
  --gst-height  高度（預設 1080）
  --gst-fps     幀率（預設 30）
  --wb-mode     白平衡：1=auto, 6=fluorescent（預設 1 auto）

ZLAN6042 專用：
  --zlan-ip          ZLAN IP，不設則停用硬體輸出
  --zlan-port        Modbus TCP port（預設 502）
  --zlan-unit        Modbus station ID（預設 1）
  --zlan-invert-di   DI 低有效翻轉（預設 True；模擬伺服器用 False）
  --zlan-trigger     polling（DI1 觸發）| free（連續輸出，預設 polling）
  --zlan-do-ok       OK 信號 DO channel（預設 1）
  --zlan-do-ng       NG 信號 DO channel（預設 2）
  --zlan-pulse-ms    DO 脈衝寬度毫秒（預設 500）
  --zlan-debounce    polling 模式：DI1 High 後的去抖延遲秒（預設 2.0）
=====================================================================
"""

import argparse
import base64
import json
import logging
import os
import queue
import signal
import ssl
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.error
import uuid
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from template_matching import TemplateMatcher

# ──────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────
def _setup_logging(log_path: str = '/tmp/infer_runner.log') -> logging.Logger:
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    logger = logging.getLogger('infer')
    logger.setLevel(logging.INFO)
    # stderr
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # 檔案（保留最近 10MB，最多 2 個備份）
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=2, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info(f'[Log] 輸出至 {log_path}')
    except Exception as e:
        logger.warning(f'[Log] 無法開啟 log 檔: {e}')
    return logger

log = _setup_logging()


# ──────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────
def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def load_product(db_path: str, serial: str) -> Optional[Dict]:
    conn = get_db(db_path)
    row = conn.execute('SELECT * FROM products WHERE serial=?', (serial,)).fetchone()
    conn.close()
    return dict(row) if row else None


def load_regions(db_path: str, product_id: int) -> List[Dict]:
    conn = get_db(db_path)
    rows = conn.execute('SELECT * FROM regions WHERE product_id=?', (product_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _clean_logic_mode(v: str) -> str:
    return 'ALL' if str(v or '').strip().upper() == 'ALL' else 'ANY'


def _clean_sample_role(v: str) -> str:
    return 'NG' if str(v or '').strip().upper() in ('NG', 'REJECT', 'FAIL') else 'OK'


def ensure_rule_schema(db_path: str) -> None:
    """Create Rule Group v1 tables when infer_runner runs before app.py migration."""
    conn = get_db(db_path)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inspection_rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                name        TEXT NOT NULL,
                logic_mode  TEXT NOT NULL DEFAULT 'ANY',
                enabled     INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inspection_rule_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id     INTEGER NOT NULL,
                region_id   INTEGER NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS inspection_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                name        TEXT NOT NULL,
                logic_mode  TEXT NOT NULL DEFAULT 'ANY',
                enabled     INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        conn.execute('''
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
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        try:
            conn.execute("ALTER TABLE inspection_item_templates ADD COLUMN sample_role TEXT NOT NULL DEFAULT 'OK'")
        except Exception:
            pass
        try:
            conn.execute("UPDATE inspection_item_templates SET sample_role='OK' WHERE sample_role IS NULL OR sample_role=''")
        except Exception:
            pass
        for col in ('source_width', 'source_height'):
            if col not in {row['name'] for row in conn.execute('PRAGMA table_info(inspection_item_templates)')}:
                conn.execute(f'ALTER TABLE inspection_item_templates ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS product_inference_settings (
                product_id        INTEGER PRIMARY KEY,
                final_logic_mode  TEXT NOT NULL DEFAULT 'ALL',
                updated_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
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
                conn.execute(f'ALTER TABLE inspection_items ADD COLUMN {col_def}')
            except Exception:
                pass
        conn.execute('''
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
                updated_at              TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        conn.commit()
    finally:
        conn.close()


def load_rule_groups(db_path: str, product_id: int) -> List[Dict]:
    ensure_rule_schema(db_path)
    conn = get_db(db_path)
    try:
        rules = conn.execute(
            '''SELECT id, name, logic_mode, enabled, sort_order
               FROM inspection_rules
               WHERE product_id=? AND enabled=1
               ORDER BY sort_order, id''',
            (product_id,)
        ).fetchall()
        out = []
        for rule in rules:
            items = conn.execute(
                '''SELECT i.region_id, r.label
                   FROM inspection_rule_items i
                   LEFT JOIN regions r ON r.id=i.region_id
                   WHERE i.rule_id=? AND i.enabled=1
                   ORDER BY i.sort_order, i.id''',
                (rule['id'],)
            ).fetchall()
            out.append({
                'id': rule['id'],
                'name': rule['name'],
                'logic_mode': _clean_logic_mode(rule['logic_mode']),
                'items': [dict(x) for x in items],
            })
        return out
    finally:
        conn.close()



def load_runtime_inspection_items(db_path: str, product_id: int) -> List[Dict]:
    ensure_rule_schema(db_path)
    conn = get_db(db_path)
    try:
        items = conn.execute('''
            SELECT id, name, logic_mode, enabled, sort_order,
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
            WHERE product_id=? AND enabled=1
            ORDER BY sort_order, id
        ''', (product_id,)).fetchall()
        out = []
        for item in items:
            samples = conn.execute('''
                SELECT id, item_id, sample_name, COALESCE(sample_role, 'OK') AS sample_role, source_product_id, source_region_id, x, y, w, h, threshold, search_margin, template_b64, source_width, source_height, sort_order
                FROM inspection_item_templates
                WHERE item_id=? AND enabled=1
                ORDER BY sort_order, id
            ''', (item['id'],)).fetchall()
            sample_list = []
            for sample in samples:
                d = dict(sample)
                d['sample_role'] = _clean_sample_role(d.get('sample_role'))
                if not d.get('template_b64'):
                    continue
                try:
                    tpl_bgr = b64_to_cv2(d['template_b64'])
                    tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
                    d['tpl_gray'] = tpl_gray
                    d['th'] = tpl_gray.shape[0]
                    d['tw'] = tpl_gray.shape[1]
                    sample_list.append(d)
                except Exception as e:
                    log.warning(f"[MultiSample] sample decode failed: {d.get('sample_name')} {e}")
            if sample_list:
                out.append({
                    'id': item['id'],
                    'name': item['name'],
                    'logic_mode': _clean_logic_mode(item['logic_mode']),
                    'sort_order': int(item['sort_order'] or 0),
                    'step_no': int(item['step_no'] or (item['sort_order'] or 0) + 1),
                    'required': bool(item['required']),
                    'min_consecutive_hits': max(1, int(item['min_consecutive_hits'] or 2)),
                    'hold_ms': max(0, int(item['hold_ms'] or 0)),
                    'timeout_sec': max(0.0, float(item['timeout_sec'] or 0)),
                    'allow_out_of_order': bool(item['allow_out_of_order']),
                    'latch_when_done': bool(item['latch_when_done']),
                    'alarm_if_missing': bool(item['alarm_if_missing']),
                    'ui_color': str(item['ui_color'] or ''),
                    'samples': sample_list,
                })
        return out
    finally:
        conn.close()


def fill_template_source_sizes(db_path: str, product_id: int, regions: List[Dict],
                               inspection_items: List[Dict]) -> None:
    """Recover legacy source dimensions without rewriting saved coordinates."""
    conn = get_db(db_path)
    reference_sizes = {}
    product_has_reference = 'reference_img_b64' in {
        row['name'] for row in conn.execute('PRAGMA table_info(products)')}
    region_has_size = {'source_width', 'source_height'}.issubset({
        row['name'] for row in conn.execute('PRAGMA table_info(regions)')})
    region_sizes = {int(r['id']): (int(r.get('source_width') or 0),
                                  int(r.get('source_height') or 0)) for r in regions}

    def reference_size(pid):
        pid = int(pid or 0)
        if pid not in reference_sizes:
            row = (conn.execute('SELECT reference_img_b64 FROM products WHERE id=?', (pid,)).fetchone()
                   if product_has_reference else None)
            size = (0, 0)
            if row and row['reference_img_b64']:
                try:
                    image = b64_to_cv2(row['reference_img_b64'])
                    size = (image.shape[1], image.shape[0]) if image is not None else size
                except Exception:
                    pass
            reference_sizes[pid] = size
        return reference_sizes[pid]

    try:
        for reg in regions:
            if not all(region_sizes[int(reg['id'])]):
                reg['source_width'], reg['source_height'] = reference_size(product_id)
                region_sizes[int(reg['id'])] = (reg['source_width'], reg['source_height'])
        for item in inspection_items:
            for sample in item.get('samples', []):
                if int(sample.get('source_width') or 0) > 0 and int(sample.get('source_height') or 0) > 0:
                    continue
                source_id = int(sample.get('source_region_id') or 0)
                size = region_sizes.get(source_id, (0, 0))
                if not all(size) and source_id and region_has_size:
                    row = conn.execute('SELECT source_width, source_height FROM regions WHERE id=?',
                                       (source_id,)).fetchone()
                    if row:
                        size = (int(row['source_width'] or 0), int(row['source_height'] or 0))
                if not all(size):
                    size = reference_size(sample.get('source_product_id') or product_id)
                sample['source_width'], sample['source_height'] = size
    finally:
        conn.close()




def load_product_final_logic_mode(db_path: str, product_id: int) -> str:
    """Read final group-combination logic. Runtime stays read-only; missing table => ALL."""
    try:
        conn = get_db(db_path)
        row = conn.execute(
            'SELECT final_logic_mode FROM product_inference_settings WHERE product_id=?',
            (product_id,)
        ).fetchone()
        conn.close()
        return _clean_logic_mode(row['final_logic_mode'] if row else 'ALL')
    except sqlite3.OperationalError:
        return 'ALL'
    except Exception as e:
        log.warning(f'[RuleGroups] final_logic_mode read failed: {e}')
        return 'ALL'

SOP_DEFAULT_CONFIG = {
    'enabled': False, 'strict_order': True, 'completion_mode': 'ALL_REQUIRED',
    'auto_reset_mode': 'MANUAL', 'idle_reset_sec': 0.0,
    'alarm_on_skip': True, 'alarm_on_timeout': True, 'alarm_latch': True,
    'web_alarm_enabled': True, 'tower_light_enabled': False,
    'tower_green_channel': 1, 'tower_yellow_channel': 2, 'tower_red_channel': 3,
    'station_name': '',
}

def load_product_sop_config(db_path: str, product_id: int) -> Dict:
    ensure_rule_schema(db_path)
    cfg = dict(SOP_DEFAULT_CONFIG)
    conn = get_db(db_path)
    try:
        row = conn.execute('SELECT * FROM product_sop_config WHERE product_id=?', (product_id,)).fetchone()
        if row:
            cfg.update(dict(row))
    finally:
        conn.close()
    for key in ('enabled','strict_order','alarm_on_skip','alarm_on_timeout','alarm_latch',
                'web_alarm_enabled','tower_light_enabled'):
        cfg[key] = bool(cfg.get(key))
    for key in ('tower_green_channel','tower_yellow_channel','tower_red_channel'):
        try: cfg[key] = max(1, min(4, int(cfg.get(key) or SOP_DEFAULT_CONFIG[key])))
        except Exception: cfg[key] = SOP_DEFAULT_CONFIG[key]
    if len({cfg['tower_green_channel'], cfg['tower_yellow_channel'], cfg['tower_red_channel']}) != 3:
        cfg['tower_green_channel'], cfg['tower_yellow_channel'], cfg['tower_red_channel'] = 1, 2, 3
    try: cfg['idle_reset_sec'] = max(0.0, float(cfg.get('idle_reset_sec') or 0))
    except Exception: cfg['idle_reset_sec'] = 0.0
    return cfg


class SopFlowEngine:
    """Cross-frame SOP state machine.

    A step is completed only by positive OK-template evidence.  An NG-only rule
    that merely means "no reject was seen" is not accepted as proof that an
    operator performed a step.
    """
    def __init__(self, steps: List[Dict], config: Dict):
        self.config = dict(SOP_DEFAULT_CONFIG)
        self.config.update(config or {})
        self.steps_cfg = sorted(
            [dict(x) for x in steps],
            key=lambda x: (
                int(x.get('step_no') or 9999),
                int(x.get('sort_order') or 9999),
                int(x.get('id') or 0),
            ),
        )
        self._lock = threading.RLock()
        self.reset(reason='INIT')

    def reset(self, reason: str = 'MANUAL', now_ts: Optional[float] = None) -> Dict:
        with getattr(self, '_lock', threading.RLock()):
            now = float(now_ts) if now_ts is not None else time.time()
            self.session_id = uuid.uuid4().hex[:12]
            self.reset_reason = reason
            self.created_ts = now
            self.started_ts: Optional[float] = None
            self.completed_ts: Optional[float] = None
            self.expected_since_ts: Optional[float] = None
            self.last_frame_ts: Optional[float] = None
            self.clock_ts = now
            self.external_clock = now_ts is not None
            self.state = 'WAITING'
            self.state_seq = getattr(self, 'state_seq', 0) + 1
            self.alarm_seq = getattr(self, 'alarm_seq', 0)
            self.alarm = {
                'active': False, 'code': '', 'message': '',
                'step_id': 0, 'step_name': '', 'ts': 0.0,
                'seq': self.alarm_seq,
            }
            self.steps = []
            for idx, cfg in enumerate(self.steps_cfg):
                self.steps.append({
                    'id': int(cfg['id']),
                    'step_no': int(cfg.get('step_no') or idx + 1),
                    'name': str(cfg.get('name') or f'Step {idx+1}'),
                    'required': bool(cfg.get('required', True)),
                    'status': 'PENDING',
                    'consecutive_hits': 0,
                    'hold_ms_acc': 0.0,
                    'out_of_order_hits': 0,
                    'out_of_order_hold_ms': 0.0,
                    'first_seen_ts': 0.0,
                    'last_seen_ts': 0.0,
                    'completed_ts': 0.0,
                    'elapsed_sec': 0.0,
                    'matched_label': '',
                    'last_score': 0.0,
                    'ui_color': str(cfg.get('ui_color') or ''),
                    # Runtime flow metadata: lets every inference UI draw an exact
                    # step graph without loading the configuration through a second API.
                    'logic_mode': str(cfg.get('logic_mode') or 'ANY').upper(),
                    'min_consecutive_hits': max(1, int(cfg.get('min_consecutive_hits') or 1)),
                    'hold_target_ms': max(0, int(cfg.get('hold_ms') or 0)),
                    'timeout_sec': max(0.0, float(cfg.get('timeout_sec') or 0)),
                    'allow_out_of_order': bool(cfg.get('allow_out_of_order')),
                    'latch_when_done': bool(cfg.get('latch_when_done', True)),
                    'alarm_if_missing': bool(cfg.get('alarm_if_missing', True)),
                    'sample_count': len(cfg.get('samples') or []),
                    'ok_sample_count': sum(
                        1 for sample in (cfg.get('samples') or [])
                        if str(sample.get('sample_role') or 'OK').upper() != 'NG'
                    ),
                    'ng_sample_count': sum(
                        1 for sample in (cfg.get('samples') or [])
                        if str(sample.get('sample_role') or 'OK').upper() == 'NG'
                    ),
                })
            return self.summary(now_ts=now)

    def interrupt(self, reason: str = 'SOURCE_INTERRUPTED') -> Dict:
        """Clear partial detections after a stream gap without losing DONE steps."""
        with self._lock:
            self.last_frame_ts = None
            for st in self.steps:
                if st['status'] == 'DETECTING':
                    st['status'] = 'PENDING'
                    st['consecutive_hits'] = 0
                    st['hold_ms_acc'] = 0.0
                    st['first_seen_ts'] = 0.0
                st['out_of_order_hits'] = 0
                st['out_of_order_hold_ms'] = 0.0
            self.reset_reason = reason
            return self.summary()

    def acknowledge_alarm(self) -> Dict:
        with self._lock:
            self.alarm = {
                'active': False, 'code': '', 'message': '',
                'step_id': 0, 'step_name': '', 'ts': 0.0,
                'seq': self.alarm_seq,
            }
            if self._is_complete():
                self._set_state('COMPLETE')
            else:
                self._set_state('RUNNING' if self.started_ts else 'WAITING')
            return self.summary()

    def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.state_seq += 1

    def _raise_alarm(
        self,
        code: str,
        message: str,
        step: Optional[Dict] = None,
        now_ts: Optional[float] = None,
    ):
        if self.alarm.get('active') and self.config.get('alarm_latch', True):
            return
        now = float(now_ts) if now_ts is not None else self.clock_ts
        self.alarm_seq += 1
        self.alarm = {
            'active': True,
            'code': code,
            'message': message,
            'step_id': int(step.get('id') or 0) if step else 0,
            'step_name': str(step.get('name') or '') if step else '',
            'ts': now,
            'seq': self.alarm_seq,
        }
        self._set_state('ALARM')

    def _current_expected_index(self) -> Optional[int]:
        for i, st in enumerate(self.steps):
            if st['required'] and st['status'] != 'DONE':
                return i
        return None

    def _is_complete(self) -> bool:
        return bool(self.steps) and all(
            (not s['required']) or s['status'] == 'DONE'
            for s in self.steps
        )

    @staticmethod
    def _score_from_rule(rule: Dict) -> float:
        vals = []
        for x in rule.get('items', []) or []:
            try:
                if x.get('score') is not None:
                    vals.append(float(x['score']))
            except Exception:
                pass
        return max(vals) if vals else 0.0

    @staticmethod
    def _rule_has_positive_evidence(rule: Dict) -> bool:
        """Return True only when configured OK evidence is actually matched."""
        items = rule.get('items', []) or []
        ok_items = [
            x for x in items
            if str(x.get('sample_role') or 'OK').upper() != 'NG'
        ]
        if ok_items:
            # rule.pass also guarantees that no configured NG override is active.
            return bool(rule.get('pass')) and any(bool(x.get('matched')) for x in ok_items)
        # Legacy rule groups do not expose sample_role/matched.  A non-empty
        # matched label is the safest backward-compatible positive evidence.
        return bool(rule.get('pass')) and bool(rule.get('matched'))

    def update(self, rules: List[Dict], now_ts: Optional[float] = None) -> Dict:
        """Update state from one frame; now_ts uses the source/video timeline."""
        with self._lock:
            external_clock = now_ts is not None
            now = float(now_ts) if external_clock else time.time()
            self.clock_ts = now
            self.external_clock = self.external_clock or external_clock
            if self.last_frame_ts is None:
                dt_ms = 0.0
            else:
                dt_ms = max(0.0, (now - self.last_frame_ts) * 1000.0)
                # A long live-source gap is not continuous visual evidence.
                if not external_clock:
                    dt_ms = min(2000.0, dt_ms)
            self.last_frame_ts = now

            by_id = {
                int(r.get('id')): r
                for r in (rules or [])
                if r.get('id') is not None
            }
            detected_by_id = {
                rid: self._rule_has_positive_evidence(rule)
                for rid, rule in by_id.items()
            }
            hard_reject_rules = [
                r for r in by_id.values() if bool(r.get('hard_reject'))
            ]
            any_evidence = any(detected_by_id.values()) or bool(hard_reject_rules)
            if self.started_ts is None and any_evidence:
                self.started_ts = now
                self.expected_since_ts = now
                self._set_state('RUNNING')

            expected_idx = self._current_expected_index()
            later_hit = None

            # Explicit NG template matches are alarms, not SOP completion evidence.
            if hard_reject_rules:
                bad = hard_reject_rules[0]
                bad_step = next((s for s in self.steps if s['id'] == int(bad.get('id') or 0)), None)
                self._raise_alarm(
                    'VISUAL_NG',
                    f"NG 樣板命中：{bad.get('name') or (bad_step or {}).get('name') or 'Unknown step'}",
                    bad_step,
                    now_ts=now,
                )

            for i, st in enumerate(self.steps):
                cfg = self.steps_cfg[i]
                rule = by_id.get(st['id'], {})
                detected = bool(detected_by_id.get(st['id'], False))

                if st['status'] == 'DONE' and bool(cfg.get('latch_when_done', True)):
                    continue
                if st['status'] == 'DONE' and not bool(cfg.get('latch_when_done', True)) and not detected:
                    st['status'] = 'PENDING'
                    st['completed_ts'] = 0.0

                is_blocked_later_required = (
                    self.config.get('strict_order', True)
                    and expected_idx is not None
                    and i > expected_idx
                    and st['required']
                    and not bool(cfg.get('allow_out_of_order'))
                )

                if detected:
                    st['last_seen_ts'] = now
                    st['matched_label'] = str(rule.get('matched') or '')
                    st['last_score'] = self._score_from_rule(rule)

                    if is_blocked_later_required:
                        first_out_of_order_hit = st['out_of_order_hits'] == 0
                        st['out_of_order_hits'] += 1
                        if not first_out_of_order_hit:
                            st['out_of_order_hold_ms'] += dt_ms
                        min_hits = max(1, int(cfg.get('min_consecutive_hits') or 1))
                        hold_ms = max(0, int(cfg.get('hold_ms') or 0))
                        if (
                            st['out_of_order_hits'] >= min_hits
                            and st['out_of_order_hold_ms'] >= hold_ms
                        ):
                            later_hit = st
                        continue

                    st['out_of_order_hits'] = 0
                    st['out_of_order_hold_ms'] = 0.0
                    first_detection = st['status'] != 'DETECTING'
                    if first_detection:
                        st['status'] = 'DETECTING'
                        st['first_seen_ts'] = now
                        st['consecutive_hits'] = 0
                        st['hold_ms_acc'] = 0.0
                    st['consecutive_hits'] += 1
                    if not first_detection:
                        st['hold_ms_acc'] += dt_ms
                    min_hits = max(1, int(cfg.get('min_consecutive_hits') or 1))
                    hold_ms = max(0, int(cfg.get('hold_ms') or 0))
                    if st['consecutive_hits'] >= min_hits and st['hold_ms_acc'] >= hold_ms:
                        st['status'] = 'DONE'
                        st['completed_ts'] = now
                        st['elapsed_sec'] = max(
                            0.0,
                            now - (self.expected_since_ts or st['first_seen_ts'] or now),
                        )
                        # Only completing the currently expected required step starts
                        # the next required step's timeout clock.
                        if expected_idx is not None and i == expected_idx:
                            self.expected_since_ts = now
                else:
                    st['out_of_order_hits'] = 0
                    st['out_of_order_hold_ms'] = 0.0
                    if st['status'] == 'DETECTING':
                        st['status'] = 'PENDING'
                        st['consecutive_hits'] = 0
                        st['hold_ms_acc'] = 0.0
                        st['first_seen_ts'] = 0.0

            if later_hit and self.config.get('alarm_on_skip', True):
                exp = self.steps[expected_idx] if expected_idx is not None else None
                if exp and exp['status'] != 'DONE' and self.steps_cfg[expected_idx].get('alarm_if_missing', True):
                    self._raise_alarm(
                        'STEP_SKIPPED',
                        f"步驟遺漏：請先完成 [{exp['step_no']}] {exp['name']}",
                        exp,
                        now_ts=now,
                    )

            expected_idx = self._current_expected_index()
            if (
                expected_idx is not None
                and self.started_ts is not None
                and self.config.get('alarm_on_timeout', True)
            ):
                exp = self.steps[expected_idx]
                cfg = self.steps_cfg[expected_idx]
                timeout = max(0.0, float(cfg.get('timeout_sec') or 0))
                if (
                    timeout > 0
                    and now - (self.expected_since_ts or self.started_ts) >= timeout
                    and exp['status'] != 'DONE'
                ):
                    exp['status'] = 'TIMEOUT'
                    if cfg.get('alarm_if_missing', True):
                        self._raise_alarm(
                            'STEP_TIMEOUT',
                            f"步驟逾時：[{exp['step_no']}] {exp['name']}",
                            exp,
                            now_ts=now,
                        )

            if self._is_complete():
                if self.completed_ts is None:
                    self.completed_ts = now
                if self.alarm.get('active') and self.config.get('alarm_latch', True):
                    self._set_state('ALARM')
                else:
                    self.alarm = {
                        'active': False, 'code': '', 'message': '',
                        'step_id': 0, 'step_name': '', 'ts': 0.0,
                        'seq': self.alarm_seq,
                    }
                    self._set_state('COMPLETE')
            elif not self.alarm.get('active'):
                self._set_state('RUNNING' if self.started_ts else 'WAITING')

            delay = float(self.config.get('idle_reset_sec') or 0)
            if (
                self.state == 'COMPLETE'
                and self.config.get('auto_reset_mode') == 'ON_PASS_DELAY'
                and delay > 0
                and now - (self.completed_ts or now) >= delay
            ):
                return self.reset(reason='AUTO_PASS_DELAY', now_ts=now if external_clock else None)
            return self.summary(now_ts=now)

    def finish(self, now_ts: Optional[float] = None) -> Dict:
        """End-of-station hard gate: alarm if any required step is missing."""
        with self._lock:
            now = float(now_ts) if now_ts is not None else (self.last_frame_ts or time.time())
            self.clock_ts = now
            if self._is_complete() and not (
                self.alarm.get('active') and self.config.get('alarm_latch', True)
            ):
                self._set_state('COMPLETE')
                return self.summary(now_ts=now)
            idx = self._current_expected_index()
            missing = self.steps[idx] if idx is not None else None
            if missing is not None:
                self._raise_alarm(
                    'STEP_MISSING',
                    f"流程未完成：缺少 [{missing['step_no']}] {missing['name']}",
                    missing,
                    now_ts=now,
                )
            elif not self.alarm.get('active'):
                self._raise_alarm('SOP_INCOMPLETE', '流程尚未完成', None, now_ts=now)
            return self.summary(now_ts=now)

    def summary(self, now_ts: Optional[float] = None) -> Dict:
        with self._lock:
            if now_ts is not None:
                now = float(now_ts)
            elif self.last_frame_ts is not None:
                now = self.last_frame_ts
            else:
                now = time.time()
            expected_idx = self._current_expected_index()
            done = sum(1 for s in self.steps if s['status'] == 'DONE' and s['required'])
            total = sum(1 for s in self.steps if s['required'])
            steps_complete = self._is_complete()
            gate_complete = steps_complete and not (
                self.alarm.get('active') and self.config.get('alarm_latch', True)
            )
            return {
                'enabled': bool(self.config.get('enabled')),
                'session_id': self.session_id,
                'state': self.state,
                'state_seq': self.state_seq,
                'complete': gate_complete and self.state == 'COMPLETE',
                'steps_complete': steps_complete,
                'done_count': done,
                'total_required': total,
                'progress_pct': round((done / total * 100.0) if total else 0.0, 1),
                'current_step_index': expected_idx,
                'current_step_id': self.steps[expected_idx]['id'] if expected_idx is not None else 0,
                'current_step_name': self.steps[expected_idx]['name'] if expected_idx is not None else '',
                'started_at': (
                    datetime.fromtimestamp(self.started_ts).isoformat(timespec='seconds')
                    if self.started_ts else ''
                ),
                'elapsed_sec': round(max(0.0, now - self.started_ts), 1) if self.started_ts else 0.0,
                'timeline_ts': round(now, 3),
                'alarm': dict(self.alarm),
                'config': dict(self.config),
                'steps': [dict(x) for x in self.steps],
            }

def rotate_logs_db(db_path: str, retain_days: int = None) -> int:
    """刪除超過 retain_days 天的 inference_logs，回傳刪除筆數。"""
    if retain_days is None:
        retain_days = LOG_RETAIN_DAYS
    if retain_days <= 0:
        return 0
    try:
        conn = get_db(db_path)
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
            log.info(f'[Log Rotate] 刪除 {deleted} 筆超過 {retain_days} 天的記錄')
        conn.close()
        return deleted
    except Exception as e:
        log.warning(f'[Log Rotate] 失敗: {e}')
        return 0


def ensure_log_schema(db_path: str) -> None:
    """Ensure inference_logs has v1 archive columns even when DB was created by an older build."""
    try:
        conn = get_db(db_path)
        conn.execute('''
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
        for col_def in [
            'raw_image_path TEXT',
            'result_image_path TEXT',
            "storage_status TEXT DEFAULT 'NONE'",
        ]:
            try:
                conn.execute(f'ALTER TABLE inference_logs ADD COLUMN {col_def}')
                conn.commit()
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f'[DB] inference_logs migration failed: {e}')


def write_log(
    db_path: str, product_id: int, results: List[Dict], pass_fail: str,
    raw_image_path: str = '', result_image_path: str = '', storage_status: str = 'NONE'
) -> Optional[int]:
    try:
        ensure_log_schema(db_path)
        conn = get_db(db_path)
        cur = conn.execute(
            '''INSERT INTO inference_logs
               (product_id, result_json, pass_fail, raw_image_path, result_image_path, storage_status)
               VALUES (?,?,?,?,?,?)''',
            (product_id, json.dumps(results), pass_fail, raw_image_path or None,
             result_image_path or None, storage_status or 'NONE')
        )
        conn.commit()
        lid = cur.lastrowid
        conn.close()
        return lid
    except Exception as e:
        log.warning(f'寫入 log 失敗: {e}')
        return None


# ──────────────────────────────────────────────────────────────────
# 影像解碼
# ──────────────────────────────────────────────────────────────────
def b64_to_cv2(b64str: str) -> np.ndarray:
    if ',' in b64str:
        b64str = b64str.split(',', 1)[1]
    arr = np.frombuffer(base64.b64decode(b64str), np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ──────────────────────────────────────────────────────────────────
# Template 快取
# ──────────────────────────────────────────────────────────────────
class TemplateCache:
    def __init__(self, regions: List[Dict], rule_groups: Optional[List[Dict]] = None, inspection_items: Optional[List[Dict]] = None, final_logic_mode: str = 'ALL'):
        self.regions: List[Dict] = []
        self.rule_groups: List[Dict] = rule_groups or []
        self.inspection_items: List[Dict] = inspection_items or []
        self.final_logic_mode: str = _clean_logic_mode(final_logic_mode)
        for reg in regions:
            tpl_b64 = reg.get('template_b64')
            if not tpl_b64:
                log.warning(f"Region '{reg.get('label')}' 沒有 template，跳過")
                continue
            tpl_bgr  = b64_to_cv2(tpl_b64)
            tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
            self.regions.append({
                'id':            reg['id'],
                'label':         reg.get('label') or f"Region-{reg['id']}",
                'x':             reg['x'],
                'y':             reg['y'],
                'w':             reg['w'],
                'h':             reg['h'],
                'threshold':     reg['threshold'],
                'search_margin': reg.get('search_margin') or 0,
                'source_width':  reg.get('source_width') or 0,
                'source_height': reg.get('source_height') or 0,
                'tpl_gray':      tpl_gray,
                'th':            tpl_gray.shape[0],
                'tw':            tpl_gray.shape[1],
            })
        log.info(f'載入 {len(self.regions)} 個有效 region templates, rule_groups={len(self.rule_groups)}, inspection_items={len(self.inspection_items)}')


# ──────────────────────────────────────────────────────────────────
# CacheManager：熱更新 template 快取
#
# 推論迴圈只呼叫 cache_mgr.get() 取目前快取，reload 時以原子性
# 換指針（_lock 保護），不中斷推論、不 drop frame。
#
# 兩種觸發方式：
#   1. 背景 watcher：每 reload_interval 秒自動輪詢 DB 版本戳記
#   2. force_reload()：立即重載（供 POST /reload 呼叫）
# ──────────────────────────────────────────────────────────────────
def ensure_runtime_revision(db_path):
    """Per-product revisions advance in the same transaction as definition edits."""
    conn = get_db(db_path)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS runtime_revisions (product_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        owners = {
            'products': '{row}.id', 'packaging_settings': '{row}.product_id', 'regions': '{row}.product_id',
            'inspection_rules': '{row}.product_id', 'inspection_items': '{row}.product_id',
            'inspection_rule_items': '(SELECT product_id FROM inspection_rules WHERE id={row}.rule_id)',
            'inspection_item_templates': '(SELECT product_id FROM inspection_items WHERE id={row}.item_id)',
            'product_inference_settings': '{row}.product_id', 'product_sop_config': '{row}.product_id',
        }
        for table, owner in owners.items():
            if table not in tables:
                continue
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                rows = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
                body = ''
                for row in rows:
                    pid = owner.format(row=row)
                    body += f'INSERT INTO runtime_revisions(product_id,revision) SELECT {pid},1 WHERE {pid} IS NOT NULL ON CONFLICT(product_id) DO UPDATE SET revision=revision+1; '
                conn.execute(f'CREATE TRIGGER IF NOT EXISTS ve_revision_{table}_{operation} AFTER {operation} ON {table} BEGIN {body} END')
        conn.commit()
    finally:
        conn.close()


class CacheManager:
    def __init__(self, db_path: str, product_id: int, reload_interval: float = 5.0):
        ensure_runtime_revision(db_path)
        self._db_path         = db_path
        self._product_id      = product_id
        self._reload_interval = reload_interval
        self._lock            = threading.Lock()
        self._cache: Optional[TemplateCache] = None
        self._version         = (-1, -1, -1, -1, -1, -1, -1, -1, -1, -1)   # regions/rules/items/multi-sample/final-logic version
        self._last_reload_ts  = ''
        self._reload_count    = 0
        self._stop_event      = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None

    # ── 推論迴圈取快取（O(1)）────────────────────────────────────
    def get(self) -> Optional[TemplateCache]:
        with self._lock:
            return self._cache

    # ── 查 DB 版本戳記 ────────────────────────────────────────────
    def _db_version(self):
        conn = get_db(self._db_path)
        try:
            row = conn.execute('SELECT revision FROM runtime_revisions WHERE product_id=?', (self._product_id,)).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # ── 實際 reload ───────────────────────────────────────────────
    def _do_reload(self, reason: str) -> bool:
        try:
            before = self._db_version()
            regions          = load_regions(self._db_path, self._product_id)
            rule_groups      = load_rule_groups(self._db_path, self._product_id)
            inspection_items = load_runtime_inspection_items(self._db_path, self._product_id)
            fill_template_source_sizes(self._db_path, self._product_id, regions, inspection_items)
            final_logic_mode = load_product_final_logic_mode(self._db_path, self._product_id)
            new_cache        = TemplateCache(regions, rule_groups, inspection_items, final_logic_mode)
            new_ver = self._db_version()
            if before != new_ver:
                return False  # Never publish a definition assembled across commits.
            with self._lock:
                self._cache          = new_cache
                self._version        = new_ver
                self._last_reload_ts = datetime.now().isoformat(timespec='seconds')
                self._reload_count  += 1
            log.info(f'[Cache] Reload 完成（原因={reason}，第 {self._reload_count} 次，'
                     f'regions={len(new_cache.regions)}，items={len(new_cache.inspection_items)}，版本={new_ver}）')
            return True
        except Exception as e:
            log.error(f'[Cache] Reload 失敗: {e}')
            return False

    # ── 啟動時首次載入 ────────────────────────────────────────────
    def initial_load(self) -> bool:
        ok = self._do_reload('startup')
        return ok

    # ── 手動強制 reload（POST /reload）───────────────────────────
    def force_reload(self) -> bool:
        return self._do_reload('manual')

    # ── 狀態資訊（供 /health 附帶回傳）──────────────────────────
    def info(self) -> Dict:
        with self._lock:
            n = len(self._cache.regions) if self._cache else 0
            rn = len(self._cache.rule_groups) if self._cache else 0
            mn = len(self._cache.inspection_items) if self._cache else 0
            fl = self._cache.final_logic_mode if self._cache else 'ALL'
        return {
            'regions':      n,
            'rule_groups':  rn,
            'inspection_items': mn,
            'final_logic_mode': fl,
            'version':      list(self._version),
            'reload_count': self._reload_count,
            'last_reload':  self._last_reload_ts,
            'interval_sec': self._reload_interval,
        }

    # ── 背景 watcher（自動偵測 DB 變化）──────────────────────────
    def start_watcher(self) -> None:
        if self._watcher_thread is not None and self._watcher_thread.is_alive():
            return
        self._stop_event.clear()
        self._watcher_thread = threading.Thread(target=self._watcher, daemon=True)
        self._watcher_thread.start()
        log.info(f'[Cache] Watcher 已啟動，每 {self._reload_interval:.0f}s 輪詢')

    def stop_watcher(self) -> None:
        self._stop_event.set()
        log.info('[Cache] Watcher 已要求停止')

    def _watcher(self) -> None:
        while not self._stop_event.wait(self._reload_interval):
            ver = self._db_version()
            if ver != self._version:
                log.info(f'[Cache] 偵測到 DB 變更 {self._version} → {ver}，自動 reload')
                self._do_reload('auto')
        log.info('[Cache] Watcher 已停止')


# ──────────────────────────────────────────────────────────────────
# 核心推論函數
# ──────────────────────────────────────────────────────────────────
# Log rotate: 保留最近幾天（0=停用，可用環境變數 LOG_RETAIN_DAYS 覆蓋）
LOG_RETAIN_DAYS = int(os.environ.get('LOG_RETAIN_DAYS', '7'))

METHOD_MAP = {
    'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED,
    'TM_CCORR_NORMED':  cv2.TM_CCORR_NORMED,
    'TM_SQDIFF_NORMED': cv2.TM_SQDIFF_NORMED,
}


def _evaluate_rule_groups(template_results: List[Dict], rule_groups: List[Dict]) -> Tuple[bool, List[Dict]]:
    by_id = {r['id']: r for r in template_results}
    if not rule_groups:
        # Legacy mode: existing products continue to require every template to pass.
        return all(bool(r.get('pass')) for r in template_results), []

    rule_results: List[Dict] = []
    for rule in rule_groups:
        items = []
        for item in rule.get('items', []):
            region_id = item.get('region_id')
            tr = by_id.get(region_id)
            if tr is None:
                items.append({
                    'region_id': region_id,
                    'label': item.get('label') or f'Region-{region_id}',
                    'score': None,
                    'threshold': None,
                    'pass': False,
                    'error': 'template 不存在或已被刪除',
                })
            else:
                items.append({
                    'region_id': region_id,
                    'label': tr.get('label'),
                    'score': tr.get('score'),
                    'threshold': tr.get('threshold'),
                    'pass': bool(tr.get('pass')),
                    'error': tr.get('error'),
                })
        if not items:
            rule_pass = False
        elif rule.get('logic_mode') == 'ALL':
            rule_pass = all(bool(x.get('pass')) for x in items)
        else:
            rule_pass = any(bool(x.get('pass')) for x in items)
        matched = [x for x in items if x.get('pass')]
        rule_results.append({
            'id': rule.get('id'),
            'name': rule.get('name'),
            'logic_mode': _clean_logic_mode(rule.get('logic_mode')),
            'pass': rule_pass,
            'matched': matched[0]['label'] if matched else None,
            'items': items,
        })
    return all(bool(r['pass']) for r in rule_results), rule_results



def _match_one_template(src_gray, sh, sw, reg: Dict, method: int) -> Dict:
    # Kept for callers outside the Edge runtime, including older integrations.
    return TemplateMatcher(method=method).begin_gray(src_gray).match(reg)


def _run_multi_sample_inference(frame: np.ndarray, cache: TemplateCache, method: int,
                                draw_vis: bool, match_frame=None):
    # Rule Groups B+: OK samples are positive acceptance templates; NG samples are hard-reject templates.
    match_frame = match_frame or TemplateMatcher(method=method).begin(frame)
    sh, sw = match_frame.gray.shape[:2]
    vis = frame.copy() if draw_vis else None
    sample_results: List[Dict] = []
    item_results: List[Dict] = []
    hard_reject = False

    for item in cache.inspection_items:
        local_results = []
        ok_results = []
        ng_results = []
        matched_ok = []
        matched_ng = []
        for sample in item.get('samples', []):
            role = _clean_sample_role(sample.get('sample_role'))
            role_label = 'NG' if role == 'NG' else 'OK'
            reg = {
                'id': sample['id'],
                'label': f"{item['name']} / [{role_label}] {sample.get('sample_name') or ('Sample-'+str(sample['id']))}",
                'x': sample['x'], 'y': sample['y'], 'w': sample['w'], 'h': sample['h'],
                'threshold': sample['threshold'], 'search_margin': sample.get('search_margin') or 0,
                'tpl_gray': sample['tpl_gray'], 'th': sample['th'], 'tw': sample['tw'],
                'source_width': sample.get('source_width') or 0,
                'source_height': sample.get('source_height') or 0,
            }
            r = match_frame.match(reg)
            raw_match = bool(r.get('pass'))
            r['sample_id'] = r.pop('id')
            r['item_id'] = item['id']
            r['sample_role'] = role
            r['matched'] = raw_match
            r['reject'] = bool(role == 'NG' and raw_match)
            # For NG samples, pass=True means "not triggered". If triggered, it is a red hard reject.
            if role == 'NG':
                r['pass'] = not raw_match and not r.get('error')
                ng_results.append(r)
                if raw_match:
                    matched_ng.append(r)
            else:
                r['pass'] = raw_match
                ok_results.append(r)
                if raw_match:
                    matched_ok.append(r)
            local_results.append(r)
            sample_results.append(r)

        ng_hit = any(bool(r.get('reject')) for r in ng_results)
        if ok_results:
            item_ok_pass = any(bool(r.get('pass')) for r in ok_results) if item['logic_mode'] == 'ANY' else all(bool(r.get('pass')) for r in ok_results)
        else:
            # NG-only groups are valid: pass when no NG sample is matched.
            item_ok_pass = True
        matching_error = any(r.get('error') for r in local_results)
        item_pass = (not ng_hit) and item_ok_pass and not matching_error
        if ng_hit:
            hard_reject = True

        # Draw ALL sample boxes in multi-sample mode.
        # v6 fix: earlier result images only made the match box obvious; when samples overlap
        # or when users expect the configured/source ROI boxes, it looked like some boxes were missing.
        # We now draw: search area (thin amber), configured/source box (thin cyan), and match box (thick role color).
        if vis is not None:
            for draw_i, r in enumerate(local_results):
                if not r:
                    continue
                role = _clean_sample_role(r.get('sample_role'))
                if role == 'NG' and r.get('reject'):
                    color = (0, 0, 255)
                elif role == 'NG':
                    color = (0, 180, 180)
                else:
                    color = (0, 255, 80) if bool(r.get('pass')) else (0, 60, 255)

                # 1) configured/source ROI: this is the box the user created/copied into the rule group.
                ex, ey = int(r.get('x') or 0), int(r.get('y') or 0)
                ew, eh = int(r.get('w') or 0), int(r.get('h') or 0)
                if ew > 0 and eh > 0:
                    margin = int(r.get('search_margin') or 0)
                    if margin > 0:
                        sx1 = max(0, ex - margin); sy1 = max(0, ey - margin)
                        sx2 = min(sw, ex + ew + margin); sy2 = min(sh, ey + eh + margin)
                        cv2.rectangle(vis, (sx1, sy1), (sx2, sy2), (180, 180, 60), 1)
                    cv2.rectangle(vis, (ex, ey), (ex + ew, ey + eh), (255, 200, 0), 1)

                # 2) actual best-match box: this is where template matching found the sample in the current frame.
                if r.get('match_loc') and r.get('match_size'):
                    tl = tuple(r['match_loc']); tw, th = r['match_size']
                    cv2.rectangle(vis, tl, (tl[0]+tw, tl[1]+th), color, 2)
                    text_x, text_y_base = tl[0], tl[1]
                else:
                    # Still label the configured box if matching failed before a location could be produced.
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
            'id': item['id'], 'name': item['name'], 'logic_mode': item['logic_mode'],
            'pass': bool(item_pass), 'hard_reject': bool(ng_hit),
            'matched': matched_ng[0]['label'] if matched_ng else (matched_ok[0]['label'] if matched_ok else None),
            'items': [{'sample_id': r.get('sample_id'), 'sample_role': r.get('sample_role'), 'label': r.get('label'),
                       'score': r.get('score'), 'threshold': r.get('threshold'), 'matched': bool(r.get('matched')),
                       'reject': bool(r.get('reject')), 'pass': bool(r.get('pass')), 'error': r.get('error')}
                      for r in local_results]
        })
    final_logic_mode = _clean_logic_mode(getattr(cache, 'final_logic_mode', 'ALL'))
    if hard_reject or any(r.get('error') for r in sample_results):
        all_pass = False
    elif item_results:
        all_pass = all(bool(x.get('pass')) for x in item_results) if final_logic_mode == 'ALL' else any(bool(x.get('pass')) for x in item_results)
    else:
        all_pass = False
    if vis is not None:
        stamp = 'FAIL' if hard_reject else ('PASS' if all_pass else 'FAIL')
        color = (0, 220, 80) if all_pass else (0, 50, 220)
        if getattr(cache, 'snapshot_three_state', False):
            stamp = 'NG' if hard_reject else ('OK' if all_pass else 'UNKNOWN')
            if not hard_reject and not all_pass:
                color = (0, 180, 240)
        cv2.putText(vis, stamp, (10, 36), cv2.FONT_HERSHEY_DUPLEX, 1.2, color, 2, cv2.LINE_AA)
        if hard_reject:
            cv2.putText(vis, 'NG OVERRIDE', (10, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2, cv2.LINE_AA)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cv2.putText(vis, ts, (sw - 280, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)
    for _g in item_results:
        _g.setdefault('group_type', 'inspection_item')
    final_name = 'Final Logic / NG override' if hard_reject else 'Final Logic'
    cache.last_rule_results = [{'name': final_name, 'logic_mode': final_logic_mode, 'pass': bool(all_pass), 'hard_reject': bool(hard_reject), 'items': []}] + item_results
    return all_pass, sample_results, vis

def run_inference(
    frame: np.ndarray,
    cache: TemplateCache,
    method: int = cv2.TM_CCOEFF_NORMED,
    draw_vis: bool = True,
    match_frame=None,
) -> Tuple[bool, List[Dict], Optional[np.ndarray]]:
    match_frame = match_frame or TemplateMatcher(method=method).begin(frame)
    if getattr(cache, 'inspection_items', None):
        cache.last_inference_mode = 'multi_sample'
        return _run_multi_sample_inference(frame, cache, method, draw_vis, match_frame)
    sh, sw = match_frame.gray.shape[:2]
    results  = []
    vis      = frame.copy() if draw_vis else None

    for reg in cache.regions:
        label  = reg['label']

        r = match_frame.match(reg, small_roi_fallback=True)
        if r['score'] is None:
            results.append(r)
            continue
        score = r['score']
        tl = tuple(r['match_loc'])
        passed = r['pass']
        tw, th = r['match_size']
        margin = r['search_margin']
        ex, ey, ew, eh = r['x'], r['y'], r['w'], r['h']

        if margin > 0 and vis is not None:
            rx1 = max(0, ex - margin); ry1 = max(0, ey - margin)
            rx2 = min(sw, ex + ew + margin); ry2 = min(sh, ey + eh + margin)
            cv2.rectangle(vis, (rx1, ry1), (rx2, ry2), (180, 180, 60), 1)

        if vis is not None:
            color = (0, 255, 80) if passed else (0, 60, 255)
            cv2.rectangle(vis, tl, (tl[0]+tw, tl[1]+th), color, 2)
            cv2.putText(vis, f'{label} {score:.3f}',
                        (tl[0], max(tl[1]-6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
            cv2.rectangle(vis, (ex, ey), (ex+ew, ey+eh), (255, 200, 0), 1)

        results.append({key: r[key] for key in
                        ('id', 'label', 'score', 'threshold', 'pass', 'match_loc', 'match_size')})

    all_pass, rule_results = _evaluate_rule_groups(results, cache.rule_groups)
    if rule_results:
        results_payload: List[Dict] = list(results)
        # Add rule details without breaking existing monitor UI that reads result['results'] as a list.
        # Consumers that know Rule Group v1 can read result['rules'] from shared HTTP payload below.
    if vis is not None:
        stamp = 'PASS' if all_pass else 'FAIL'
        color = (0, 220, 80) if all_pass else (0, 50, 220)
        if getattr(cache, 'snapshot_three_state', False):
            stamp = 'OK' if all_pass else 'UNKNOWN'
            if not all_pass:
                color = (0, 180, 240)
        cv2.putText(vis, stamp, (10, 36),
                    cv2.FONT_HERSHEY_DUPLEX, 1.2, color, 2, cv2.LINE_AA)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cv2.putText(vis, ts, (sw - 280, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)

    # Store rule results on the cache for the current call's caller via an attribute.
    # This avoids changing the old 3-value return contract used throughout this file.
    cache.last_rule_results = rule_results
    cache.last_inference_mode = 'rule_group' if rule_results else 'legacy_template'
    return all_pass, results, vis


# ──────────────────────────────────────────────────────────────────
# ZLAN6042 Modbus TCP 控制器
# ──────────────────────────────────────────────────────────────────
class Zlan6042Tcp:
    """
    ZLAN6042 Modbus TCP 客戶端：讀 DI1~DI3 + 寫 DO1~DO4。

    參數：
      host              設備 IP
      port              Modbus TCP port（標準 502；模擬伺服器 5020）
      unit_id           站號（與設備"高級參數"一致，預設 1）
      timeout           連線逾時秒
      start_address     DI 起始位址（0起始 → 0；1起始 → 1）
      invert_low_active DI 低有效翻轉（bit=1 表示低電平時設 True）
      retries / retry_delay  讀失敗重試次數與間隔
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 3.0,
        start_address: int = 0,
        invert_low_active: bool = True,
        retries: int = 1,
        retry_delay: float = 0.2,
    ) -> None:
        self.host        = host
        self.port        = port
        self.unit_id     = unit_id
        self.timeout     = timeout
        self.start       = start_address
        self.invert      = invert_low_active
        self.retries     = max(0, retries)
        self.retry_delay = max(0.0, retry_delay)
        self._client     = None
        self._lock       = threading.Lock()

    # ── 連線管理 ──────────────────────────────────────────────────
    def connect(self) -> bool:
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError:
            from pymodbus.client.sync import ModbusTcpClient  # type: ignore
        if self._client is None:
            self._client = ModbusTcpClient(
                host=self.host, port=self.port, timeout=self.timeout
            )
        return self._client.connect()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def _ensure_connected(self) -> None:
        if self._client is None or not getattr(self._client, 'connected', False):
            if not self.connect():
                raise ConnectionError(f'無法連線至 {self.host}:{self.port}')

    def __enter__(self):
        if not self.connect():
            raise ConnectionError(f'無法連線至 {self.host}:{self.port}')
        return self

    def __exit__(self, *_):
        self.close()
        return False

    # ── 讀 DI ─────────────────────────────────────────────────────
    def _read_di_raw_once(self) -> List[bool]:
        rr = self._client.read_discrete_inputs(
            address=self.start, count=3, slave=self.unit_id)
        if hasattr(rr, 'isError') and not rr.isError():
            return list(rr.bits[:3])
        rr2 = self._client.read_coils(
            address=self.start, count=3, slave=self.unit_id)
        if hasattr(rr2, 'isError') and not rr2.isError():
            return list(rr2.bits[:3])
        raise RuntimeError(f'讀DI失敗 start={self.start} slave={self.unit_id}')

    def read_di1_3_raw(self) -> List[bool]:
        with self._lock:
            self._ensure_connected()
            last_exc: Optional[Exception] = None
            for _ in range(self.retries + 1):
                try:
                    return self._read_di_raw_once()
                except Exception as e:
                    last_exc = e
                    try:
                        self.close()
                    except Exception:
                        pass
                    time.sleep(self.retry_delay)
                    self.connect()
            raise last_exc  # type: ignore

    def read_di1_3_level(self) -> List[str]:
        raw = self.read_di1_3_raw()
        def to_level(b: bool) -> str:
            return 'High' if ((not b) if self.invert else b) else 'Low'
        return [to_level(b) for b in raw]

    # ── 寫 DO ─────────────────────────────────────────────────────
    def _write_do_once_unlocked(self, channel: int, on: bool) -> None:
        if not (1 <= channel <= 4):
            raise ValueError('channel 取值範圍 1~4')
        addr = 16 + (channel - 1)   # DO1=16, DO2=17, DO3=18, DO4=19
        self._ensure_connected()
        wr = self._client.write_coil(
            address=addr, value=on, slave=self.unit_id)
        if hasattr(wr, 'isError') and wr.isError():
            raise RuntimeError(
                f'寫DO失敗 channel={channel} addr={addr} slave={self.unit_id}')

    def write_do(self, channel: int, on: bool) -> None:
        """寫入 DO，失敗時自動重新連線重試。"""
        if not (1 <= channel <= 4):
            raise ValueError('channel 取值範圍 1~4')
        with self._lock:
            last_exc: Optional[Exception] = None
            for attempt in range(self.retries + 1):
                try:
                    self._write_do_once_unlocked(channel, on)
                    return
                except Exception as e:
                    last_exc = e
                    try:
                        self.close()
                    except Exception:
                        pass
                    if attempt < self.retries:
                        time.sleep(self.retry_delay)
                        try:
                            self.connect()
                        except Exception:
                            pass
            raise last_exc  # type: ignore

    def force_do_off(self, channel: int, retries: int = 3, delay_sec: float = 0.08) -> bool:
        """盡力把指定 DO 關閉；用於避免通訊中斷後 DO 卡在 ON。"""
        ok = False
        last_exc: Optional[Exception] = None
        for attempt in range(max(1, retries)):
            try:
                self.write_do(channel, False)
                ok = True
                break
            except Exception as e:
                last_exc = e
                time.sleep(delay_sec)
        if not ok:
            log.warning(f'[ZLAN] DO{channel} OFF 重試失敗: {last_exc}')
        return ok

    def all_do_off(self, channels: Optional[List[int]] = None) -> None:
        """將指定 DO 全部關閉；預設關閉 DO1~DO4。"""
        for ch in (channels or [1, 2, 3, 4]):
            try:
                self.force_do_off(int(ch), retries=3)
            except Exception as e:
                log.warning(f'[ZLAN] DO{ch} OFF 失敗: {e}')

    def pulse_do(self, channel: int, duration_ms: float = 500) -> None:
        """閉合 DO → 等待 duration_ms → 斷開；finally 會強制 OFF，避免 NG/OK 卡住。"""
        try:
            self.write_do(channel, True)
            time.sleep(max(0.0, duration_ms) / 1000.0)
        finally:
            self.force_do_off(channel, retries=5)


# ──────────────────────────────────────────────────────────────────
# ZLAN 非阻塞控制器（DO 脈衝在背景執行緒執行）
# ──────────────────────────────────────────────────────────────────
class ZlanController:
    def __init__(self, zlan: Zlan6042Tcp, ch_ok: int, ch_ng: int, pulse_ms: float,
                 free_repeat_sec: float = 0.0):
        self.zlan     = zlan
        self.ch_ok    = ch_ok
        self.ch_ng    = ch_ng
        self.pulse_ms = pulse_ms
        # free 模式預設只在 PASS/FAIL 狀態變化時送 DO；若 free_repeat_sec > 0，
        # 同狀態最少間隔 N 秒才允許重送一次，避免 FAIL 時每幀狂發 NG。
        self.free_repeat_sec = max(0.0, float(free_repeat_sec or 0.0))
        self._signal_lock = threading.Lock()
        self._last_logical_signal: Optional[str] = None
        self._last_signal_ts: float = 0.0
        self._tower_lock = threading.Lock()
        self._last_tower_state = None

        # 啟動時先把 OK/NG 以及 DO1~DO4 清 OFF，避免 ZLAN 或外部電路保留上次狀態。
        try:
            self.zlan.all_do_off()
            log.info('[ZLAN] 啟動時已清除 DO1~DO4 OFF')
        except Exception as e:
            log.warning(f'[ZLAN] 啟動清 DO OFF 失敗: {e}')

        # DO pulse worker：避免寫 DO 脈衝阻塞主推論迴圈
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._thread  = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

        # DI polling cache：把 Modbus DI 輪詢移到背景執行緒，避免待機 preview 被 read_di 卡住
        self._di_lock = threading.Lock()
        self._di1_high = False
        self._di_last_error = ''
        self._di_last_update = 0.0
        self._di_stop = threading.Event()
        self._di_thread: Optional[threading.Thread] = None
        self._di_interval = 0.05

    def _worker(self):
        while True:
            task = self._queue.get()
            if task is None:
                break
            try:
                if task[0] == 'pulse':
                    channel = task[1]
                    self.zlan.pulse_do(channel, self.pulse_ms)
                    log.info(f'[ZLAN] DO{channel} 脈衝完成 ({self.pulse_ms:.0f}ms)')
                elif task[0] == 'tower':
                    _, green, yellow, red, ch_g, ch_y, ch_r, old_channels = task
                    desired = {int(ch_g): bool(green), int(ch_y): bool(yellow), int(ch_r): bool(red)}
                    for ch in sorted(set(int(x) for x in old_channels) | set(desired)):
                        self.zlan.write_do(ch, desired.get(ch, False))
                    log.info(f'[ZLAN] 三色燈 G={green} Y={yellow} R={red} channels={ch_g}/{ch_y}/{ch_r}')
            except Exception as e:
                log.warning(f'[ZLAN] DO 任務失敗: {e}')

    def reset_output_state(self) -> None:
        with self._signal_lock:
            self._last_logical_signal = None
            self._last_signal_ts = 0.0

    def _enqueue_signal(self, logical_signal: str, channel: int, suppress_same_state: bool = False) -> bool:
        now = time.time()
        if suppress_same_state:
            with self._signal_lock:
                same_state = (self._last_logical_signal == logical_signal)
                repeat_due = self.free_repeat_sec > 0 and (now - self._last_signal_ts) >= self.free_repeat_sec
                if same_state and not repeat_due:
                    log.debug(f'[ZLAN] free 模式狀態未變，略過 {logical_signal} 重複輸出')
                    return False
                self._last_logical_signal = logical_signal
                self._last_signal_ts = now
        else:
            with self._signal_lock:
                self._last_logical_signal = logical_signal
                self._last_signal_ts = now
        try:
            self._queue.put_nowait(('pulse', channel))
            return True
        except queue.Full:
            log.warning(f'[ZLAN] DO queue 已滿，跳過 {logical_signal} 信號')
            return False

    def send_ok(self, suppress_same_state: bool = False) -> bool:
        return self._enqueue_signal('OK', self.ch_ok, suppress_same_state=suppress_same_state)

    def send_ng(self, suppress_same_state: bool = False) -> bool:
        return self._enqueue_signal('NG', self.ch_ng, suppress_same_state=suppress_same_state)

    def set_stack_light(self, green: bool, yellow: bool, red: bool,
                        ch_green: int = 1, ch_yellow: int = 2, ch_red: int = 3) -> bool:
        state = (bool(green), bool(yellow), bool(red), int(ch_green), int(ch_yellow), int(ch_red))
        with self._tower_lock:
            if state == self._last_tower_state:
                return False
            old_state = self._last_tower_state
            self._last_tower_state = state
        old_channels = tuple(old_state[3:6]) if old_state else tuple()
        try:
            self._queue.put_nowait(('tower',) + state + (old_channels,))
            return True
        except queue.Full:
            log.warning('[ZLAN] DO queue 已滿，跳過三色燈狀態更新')
            return False

    def start_di_monitor(self, interval_sec: float = 0.05) -> None:
        """背景輪詢 DI1，讓主影像迴圈只讀 cached DI 狀態，不直接等待 Modbus TCP。"""
        self._di_interval = max(0.01, float(interval_sec))
        if self._di_thread is not None and self._di_thread.is_alive():
            return
        self._di_stop.clear()
        self._di_thread = threading.Thread(target=self._di_worker, daemon=True)
        self._di_thread.start()
        log.info(f'[ZLAN] DI 背景輪詢已啟動 interval={self._di_interval:.3f}s')

    def stop_di_monitor(self) -> None:
        self._di_stop.set()
        if self._di_thread is not None and self._di_thread.is_alive():
            self._di_thread.join(timeout=1.0)
        self._di_thread = None

    def _di_worker(self):
        while not self._di_stop.is_set():
            try:
                high = self.zlan.read_di1_3_level()[0] == 'High'
                with self._di_lock:
                    self._di1_high = high
                    self._di_last_error = ''
                    self._di_last_update = time.time()
            except Exception as e:
                # 不要每 50ms 狂洗 log，只在錯誤訊息改變時記一次
                msg = str(e)
                with self._di_lock:
                    changed = (msg != self._di_last_error)
                    self._di_last_error = msg
                    self._di_last_update = time.time()
                    self._di1_high = False
                if changed:
                    log.warning(f'[ZLAN] DI 背景輪詢失敗: {e}')
            self._di_stop.wait(self._di_interval)
        log.info('[ZLAN] DI 背景輪詢已停止')

    def read_di1_high_cached(self) -> bool:
        """讀取背景輪詢快取，不阻塞主推論/preview 迴圈。"""
        with self._di_lock:
            return self._di1_high

    def read_di1_high(self) -> bool:
        """同步讀取 DI1；保留作為 fallback/debug 用，主迴圈應優先用 read_di1_high_cached。"""
        try:
            return self.zlan.read_di1_3_level()[0] == 'High'
        except Exception as e:
            log.warning(f'[ZLAN] 讀 DI1 失敗: {e}')
            return False

    def stop(self):
        self.stop_di_monitor()
        try:
            self._queue.put(None, timeout=1.0)
        except Exception:
            pass
        self._thread.join(timeout=3)
        try:
            self.zlan.all_do_off()
            log.info('[ZLAN] 停止時已清除 DO1~DO4 OFF')
        except Exception as e:
            log.warning(f'[ZLAN] 停止清 DO OFF 失敗: {e}')

# ──────────────────────────────────────────────────────────────────
# 共享狀態（供 HTTP API 讀取）
# ──────────────────────────────────────────────────────────────────
class SharedState:
    def __init__(self, db_path: str = ''):
        self._lock        = threading.Lock()
        self._db_path     = db_path
        self.latest: Optional[Dict] = None
        self.live_img: Optional[str] = None   # 即時原始影像（無標記框）
        self.running      = True
        self.cache_mgr: Optional[CacheManager] = None
        self._product: Optional[Dict] = None
        self._reload_interval: float = 5.0
        self.product_generation: int = 0  # 每次切換產品遞增，用來丟棄舊幀結果
        # ZLAN 動態設定（可透過 /zlan-config 熱更新）
        self.zlan_ctrl: Optional['ZlanController'] = None
        self.trigger_mode: str = 'polling'   # 'free' | 'polling'
        # Camera manual settings（可透過 /camera-config 熱更新；由 GStreamer loop 套用）
        self.camera_config: Dict = {}
        self.camera_config_revision: int = 0
        # Runtime log / storage settings
        self.storage_config: Dict = dict(DEFAULT_STORAGE_CONFIG) if 'DEFAULT_STORAGE_CONFIG' in globals() else {}
        self.pass_counter: int = 0
        self.result_counter: int = 0
        # SOP Flow runtime; initialized lazily for the active product.
        self.sop_engine: Optional[SopFlowEngine] = None
        self.sop_product_id: int = 0
        self.sop_initialized: bool = False

    def update(self, result: Dict):
        with self._lock:
            self.latest = result

    def update_live(self, raw_b64: str):
        with self._lock:
            self.live_img = raw_b64

    def get(self) -> Optional[Dict]:
        with self._lock:
            return self.latest

    def get_live(self) -> Optional[str]:
        with self._lock:
            return self.live_img

    def get_product(self) -> Optional[Dict]:
        with self._lock:
            return self._product

    def get_active_context(self) -> Tuple[Optional[Dict], Optional[CacheManager], int]:
        """原子性取得目前產品、快取管理器與產品世代。"""
        with self._lock:
            return self._product, self.cache_mgr, self.product_generation

    def is_generation_current(self, generation: Optional[int]) -> bool:
        """確認推論開始時的產品世代仍然有效；避免切換瞬間舊幀回寫/觸發 DO。"""
        if generation is None:
            return True
        with self._lock:
            return generation == self.product_generation

    def switch_product(self, product: Dict, reload_interval: float) -> bool:
        """切換產品：原子性換 product + 重建 CacheManager（含 watcher）。"""
        new_mgr = CacheManager(self._db_path, product['id'], reload_interval)
        if not new_mgr.initial_load():
            return False
        with self._lock:
            old_mgr = self.cache_mgr
            self.product_generation += 1
            self._product  = product
            self.cache_mgr = new_mgr
            self.latest    = None   # 清空舊推論結果
            self.live_img  = None
            self.sop_engine = None
            self.sop_product_id = int(product['id'])
            self.sop_initialized = False
            gen = self.product_generation
        new_mgr.start_watcher()
        if old_mgr is not None and old_mgr is not new_mgr:
            old_mgr.stop_watcher()
        log.info(f"[Switch] 切換至產品: {product['serial']}  {product.get('name','')}  generation={gen}")
        return True

    def reload_sop_engine(self) -> Optional[Dict]:
        with self._lock:
            product = dict(self._product) if self._product else None
        if not product:
            return None
        cfg = load_product_sop_config(self._db_path, int(product['id']))
        steps = load_runtime_inspection_items(self._db_path, int(product['id']))
        engine = SopFlowEngine(steps, cfg) if cfg.get('enabled') and steps else None
        with self._lock:
            self.sop_engine = engine
            self.sop_product_id = int(product['id'])
            self.sop_initialized = True
        return engine.summary() if engine else {'enabled': False, 'state': 'DISABLED', 'steps': []}

    def get_sop_engine(self) -> Optional[SopFlowEngine]:
        with self._lock:
            engine = self.sop_engine
            product = dict(self._product) if self._product else None
        if product and (self.sop_product_id != int(product['id']) or not self.sop_initialized):
            self.reload_sop_engine()
            with self._lock:
                engine = self.sop_engine
        return engine

    def get_sop_summary(self) -> Dict:
        engine = self.get_sop_engine()
        return engine.summary() if engine else {'enabled': False, 'state': 'DISABLED', 'steps': [], 'alarm': {'active': False, 'seq': 0}}

    def apply_sop_output(self, summary: Dict) -> None:
        ctrl = self.zlan_ctrl
        cfg = (summary or {}).get('config') or {}
        if ctrl is None or not cfg.get('tower_light_enabled'):
            return
        state = str((summary or {}).get('state') or '')
        ctrl.set_stack_light(
            green=(state == 'COMPLETE'), yellow=(state == 'RUNNING'), red=(state == 'ALARM'),
            ch_green=int(cfg.get('tower_green_channel') or 1),
            ch_yellow=int(cfg.get('tower_yellow_channel') or 2),
            ch_red=int(cfg.get('tower_red_channel') or 3),
        )

    def reset_sop(self, reason: str = 'MANUAL') -> Dict:
        engine = self.get_sop_engine()
        summary = engine.reset(reason=reason) if engine else self.get_sop_summary()
        self.apply_sop_output(summary)
        return summary

    def acknowledge_sop_alarm(self) -> Dict:
        engine = self.get_sop_engine()
        summary = engine.acknowledge_alarm() if engine else self.get_sop_summary()
        self.apply_sop_output(summary)
        return summary

    def finish_sop(self) -> Dict:
        engine = self.get_sop_engine()
        summary = engine.finish() if engine else self.get_sop_summary()
        self.apply_sop_output(summary)
        return summary

    def set_zlan(self, ctrl: Optional['ZlanController'], trigger: str):
        """動態切換 ZLAN controller 和 trigger mode。"""
        with self._lock:
            old = self.zlan_ctrl
            self.zlan_ctrl    = ctrl
            self.trigger_mode = trigger

        # polling 模式改用背景 DI 輪詢，避免主影像迴圈被 Modbus TCP read 卡住。
        if ctrl is not None:
            ctrl.reset_output_state()
            if trigger == 'polling':
                ctrl.start_di_monitor(interval_sec=0.05)
            else:
                ctrl.stop_di_monitor()

        # 關閉舊的 controller（在 lock 外執行避免死鎖）
        if old is not None and old is not ctrl:
            try:
                old.stop()
                old.zlan.close()
            except Exception:
                pass

    def set_camera_config(self, cfg: Dict) -> int:
        """更新相機設定；GStreamer loop 會在下一輪套用到 qtiqmmfsrc。"""
        with self._lock:
            self.camera_config = dict(cfg or {})
            self.camera_config_revision += 1
            return self.camera_config_revision

    def get_camera_config(self) -> Tuple[Dict, int]:
        with self._lock:
            return dict(self.camera_config), self.camera_config_revision

    def set_storage_config(self, cfg: Dict) -> None:
        with self._lock:
            self.storage_config = dict(cfg or {})

    def get_storage_config(self) -> Dict:
        with self._lock:
            return dict(self.storage_config or {})

    def next_result_seq(self, stamp: str) -> Tuple[int, int]:
        with self._lock:
            self.result_counter += 1
            if stamp == 'PASS':
                self.pass_counter += 1
            return self.result_counter, self.pass_counter

    def stop(self):
        self.running = False
        with self._lock:
            mgr = self.cache_mgr
        if mgr is not None:
            mgr.stop_watcher()


# ──────────────────────────────────────────────────────────────────
# HTTP API Server
# ──────────────────────────────────────────────────────────────────
def make_http_handler(shared: SharedState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', len(data))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/health':
                body = {'ok': True, 'running': shared.running}
                if shared.cache_mgr:
                    body['cache'] = shared.cache_mgr.info()
                p = shared.get_product()
                if p:
                    body['product'] = {'id': p['id'], 'serial': p['serial'], 'name': p.get('name')}
                body['sop'] = shared.get_sop_summary()
                self._send(200, body)
            elif self.path == '/sop-state':
                self._send(200, {'ok': True, 'sop': shared.get_sop_summary()})
            elif self.path == '/products':
                # 從 DB 列出所有產品供前端選單使用
                try:
                    conn = get_db(shared._db_path)
                    rows = conn.execute(
                        'SELECT id, serial, name FROM products ORDER BY id'
                    ).fetchall()
                    conn.close()
                    self._send(200, [dict(r) for r in rows])
                except Exception as e:
                    self._send(500, {'error': str(e)})
            elif self.path == '/result':
                r = shared.get()
                self._send(503 if r is None else 200,
                           {'error': '尚無推論結果'} if r is None else r)
            elif self.path == '/live':
                img = shared.get_live()
                if img is None:
                    self._send(503, {'error': '尚無即時影像'})
                else:
                    self._send(200, {'live_img': img})
            elif self.path == '/sop/reset':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    self._send(200, {'ok': True, 'sop': shared.reset_sop(str(body.get('reason') or 'MANUAL'))})
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/sop/ack':
                try:
                    self._send(200, {'ok': True, 'sop': shared.acknowledge_sop_alarm()})
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/sop/finish':
                try:
                    self._send(200, {'ok': True, 'sop': shared.finish_sop()})
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/sop/reload':
                try:
                    self._send(200, {'ok': True, 'sop': shared.reload_sop_engine()})
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/camera-config':
                cfg, rev = shared.get_camera_config()
                body = dict(cfg)
                body['revision'] = rev
                self._send(200, body)
            elif self.path == '/vision-config':
                cfg, rev = shared.get_camera_config()
                body = normalize_image_transform_config(cfg)
                body['ok'] = True
                body['revision'] = rev
                self._send(200, body)
            elif self.path == '/storage-config':
                self._send(200, _public_storage_config(shared.get_storage_config()))
            elif self.path == '/storage-status':
                self._send(200, storage_status(shared.get_storage_config()))
            elif self.path == '/zlan-config':
                # 回傳目前 ZLAN 設定
                ctrl = shared.zlan_ctrl
                if ctrl is not None:
                    self._send(200, {
                        'enabled':      True,
                        'ip':           ctrl.zlan.host,
                        'port':         ctrl.zlan.port,
                        'unit':         ctrl.zlan.unit_id,
                        'invert_di':    ctrl.zlan.invert,
                        'trigger':      shared.trigger_mode,
                        'ch_ok':        ctrl.ch_ok,
                        'ch_ng':        ctrl.ch_ng,
                        'pulse_ms':     ctrl.pulse_ms,
                    })
                else:
                    self._send(200, {'enabled': False, 'trigger': shared.trigger_mode})
            elif self.path == '/result/image':
                r = shared.get()
                if r is None or not r.get('result_img'):
                    self._send(503, {'error': '尚無結果影像'})
                else:
                    self._send(200, {'image': r['result_img']})
            elif self.path.startswith('/logs'):
                try:
                    import urllib.parse
                    qs    = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    limit = int(qs.get('limit', ['50'])[0])
                    conn  = get_db(shared._db_path)
                    rows  = conn.execute(
                        '''SELECT l.id, l.product_id, p.serial, p.name,
                                  l.pass_fail, l.result_json, l.raw_image_path, l.result_image_path,
                                  COALESCE(l.storage_status,'NONE') AS storage_status, l.created_at
                           FROM inference_logs l
                           LEFT JOIN products p ON l.product_id=p.id
                           ORDER BY l.id DESC LIMIT ?''', (limit,)
                    ).fetchall()
                    conn.close()
                    self._send(200, [dict(r) for r in rows])
                except Exception as e:
                    self._send(500, {'error': str(e)})
            else:
                self._send(404, {'error': 'Not found'})

        def do_POST(self):
            if self.path == '/reload':
                if shared.cache_mgr is None:
                    self._send(503, {'error': 'CacheManager 未初始化'})
                    return
                log.info('[Cache] 收到 POST /reload 請求')
                ok = shared.cache_mgr.force_reload()
                if ok:
                    sop = shared.reload_sop_engine()
                    self._send(200, {'ok': True, 'cache': shared.cache_mgr.info(), 'sop': sop})
                else:
                    self._send(500, {'ok': False, 'error': 'Reload 失敗，請查看 log'})
            elif self.path == '/switch-product':
                try:
                    length  = int(self.headers.get('Content-Length', 0))
                    body    = json.loads(self.rfile.read(length) or b'{}')
                    serial  = body.get('serial', '').strip()
                    if not serial:
                        self._send(400, {'error': '缺少 serial 欄位'})
                        return
                    product = load_product(shared._db_path, serial)
                    if not product:
                        self._send(404, {'error': f'找不到產品序號: {serial}'})
                        return
                    interval = getattr(shared, '_reload_interval', 5.0)
                    ok = shared.switch_product(product, interval)
                    if ok:
                        save_product_config(serial, product)
                        self._send(200, {
                            'ok':     True,
                            'product': {'id': product['id'], 'serial': product['serial'],
                                        'name': product.get('name')},
                            'cache':  shared.cache_mgr.info(),
                        })
                    else:
                        self._send(500, {'ok': False,
                                         'error': f'{serial} 沒有有效的 template，請先在 app.py 標注'})
                except Exception as e:
                    self._send(500, {'error': str(e)})
            elif self.path == '/camera-config':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    cur, _ = shared.get_camera_config()
                    cfg = normalize_camera_config(body, base=cur)
                    rev = shared.set_camera_config(cfg)
                    save_camera_config(cfg)
                    save_vision_config(cfg)
                    self._send(200, {'ok': True, 'revision': rev, 'camera': cfg})
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/vision-config':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    cur, _ = shared.get_camera_config()
                    merged = dict(cur)
                    merged.update(normalize_image_transform_config(body))
                    cfg = normalize_camera_config(merged, base=cur)
                    rev = shared.set_camera_config(cfg)
                    save_camera_config(cfg)
                    save_vision_config(cfg)
                    resp = normalize_image_transform_config(cfg)
                    resp['ok'] = True
                    resp['revision'] = rev
                    self._send(200, resp)
                except Exception as e:
                    self._send(500, {'ok': False, 'error': str(e)})
            elif self.path == '/storage-config':
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    cur = shared.get_storage_config()
                    cfg = save_storage_config(normalize_storage_config(body, base=cur))
                    shared.set_storage_config(cfg)
                    self._send(200, {'ok': True, **cfg})
                except Exception as e:
                    self._send(400, {'ok': False, 'error': str(e)})
            elif self.path == '/storage-test':
                cfg = shared.get_storage_config()
                self._send(200, test_storage_write(cfg))
            elif self.path == '/storage-status':
                cfg = shared.get_storage_config()
                self._send(200, storage_status(cfg))
            elif self.path == '/storage-samba-mount':
                cfg = shared.get_storage_config()
                self._send(200, mount_samba_storage(cfg))
            elif self.path == '/storage-samba-unmount':
                cfg = shared.get_storage_config()
                self._send(200, unmount_samba_storage(cfg))
            elif self.path == '/storage-cleanup':
                cfg = shared.get_storage_config()
                res = cleanup_storage(cfg)
                self._send(200, res)
            elif self.path == '/zlan-config':
                try:
                    length  = int(self.headers.get('Content-Length', 0))
                    body    = json.loads(self.rfile.read(length) or b'{}')
                    enabled = body.get('enabled', False)
                    trigger = body.get('trigger', 'polling')
                    if not enabled:
                        shared.set_zlan(None, 'free')
                        save_zlan_config({'enabled': False, 'trigger': 'free'})
                        log.info('[ZLAN] 已停用，切換為 free 模式')
                        self._send(200, {'ok': True, 'enabled': False, 'trigger': 'free'})
                        return
                    ip       = body.get('ip', '').strip()
                    port     = int(body.get('port', 502))
                    unit     = int(body.get('unit', 1))
                    invert   = bool(body.get('invert_di', False))
                    ch_ok    = int(body.get('ch_ok', 1))
                    ch_ng    = int(body.get('ch_ng', 2))
                    pulse_ms = float(body.get('pulse_ms', 500))
                    free_repeat_sec = float(body.get('free_repeat_sec', 0.0))
                    if not ip:
                        self._send(400, {'error': '缺少 ip 欄位'})
                        return
                    log.info(f'[ZLAN] 熱更新設定: {ip}:{port} trigger={trigger}')
                    zlan = Zlan6042Tcp(host=ip, port=port, unit_id=unit,
                                      invert_low_active=invert)
                    if not zlan.connect():
                        shared.set_zlan(None, trigger)
                        self._send(502, {'ok': False, 'error': f'無法連線到 {ip}:{port}', 'trigger': trigger})
                        return
                    new_ctrl = ZlanController(zlan, ch_ok=ch_ok, ch_ng=ch_ng, pulse_ms=pulse_ms,
                                              free_repeat_sec=free_repeat_sec)
                    shared.set_zlan(new_ctrl, trigger)
                    cfg = {'enabled': True, 'ip': ip, 'port': port, 'unit': unit,
                           'invert_di': invert, 'trigger': trigger,
                           'ch_ok': ch_ok, 'ch_ng': ch_ng, 'pulse_ms': pulse_ms,
                           'free_repeat_sec': free_repeat_sec}
                    save_zlan_config(cfg)
                    log.info(f'[ZLAN] 設定完成: {ip}:{port} trigger={trigger}')
                    self._send(200, {'ok': True, 'enabled': True,
                                     'ip': ip, 'port': port, 'trigger': trigger})
                except Exception as e:
                    self._send(500, {'error': str(e)})
            else:
                self._send(404, {'error': 'Not found'})

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

    return Handler


def start_http_server(shared: SharedState, port: int, ssl_cert: str = '', ssl_key: str = '', https: bool = False):
    handler = make_http_handler(shared)
    server  = HTTPServer(('0.0.0.0', port), handler)

    use_https = bool(https or ssl_cert or ssl_key)
    if use_https:
        if not (ssl_cert and ssl_key):
            raise ValueError('HTTPS 啟動需要同時提供 --ssl-cert 與 --ssl-key')
        if not os.path.exists(ssl_cert):
            raise FileNotFoundError(f'找不到 HTTPS 憑證檔: {ssl_cert}')
        if not os.path.exists(ssl_key):
            raise FileNotFoundError(f'找不到 HTTPS 私鑰檔: {ssl_key}')
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    scheme = 'https' if use_https else 'http'
    log.info(f'API server 啟動於 {scheme}://0.0.0.0:{port}')
    if not use_https:
        log.warning('目前 API server 是 HTTP；正式環境請加上 --https --ssl-cert <cert.pem> --ssl-key <key.pem>')
    return server


# ──────────────────────────────────────────────────────────────────
# ZLAN 設定檔讀寫
# ──────────────────────────────────────────────────────────────────
ZLAN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zlan_config.json')

def save_zlan_config(cfg: dict):
    try:
        with open(ZLAN_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log.info(f'[ZLAN] 設定已儲存至 {ZLAN_CONFIG_PATH}')
    except Exception as e:
        log.warning(f'[ZLAN] 設定儲存失敗: {e}')

def load_zlan_config() -> Optional[dict]:
    try:
        if not os.path.exists(ZLAN_CONFIG_PATH):
            return None
        with open(ZLAN_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f'[ZLAN] 設定讀取失敗: {e}')
        return None


# ──────────────────────────────────────────────────────────────────
# Product 設定檔讀寫
#   目的：tm-infer.service 不再寫死 --product xxx。
#   啟動順序：
#     1. CLI --product 有值時優先使用
#     2. 否則讀取 product_config.json 的 last_product_serial
#     3. 若仍無有效產品，從 DB 自動選第一個有 template 的產品
#   前端 /switch-product 成功後會寫回 last_product_serial，下次 reboot 沿用。
# ──────────────────────────────────────────────────────────────────
PRODUCT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'product_config.json')

def save_product_config(serial: str, product: Optional[Dict] = None):
    try:
        cfg = {
            'last_product_serial': serial,
            'updated_at': datetime.now().isoformat(timespec='seconds'),
        }
        if product:
            cfg['product'] = {
                'id': product.get('id'),
                'serial': product.get('serial'),
                'name': product.get('name'),
            }
        with open(PRODUCT_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log.info(f'[Product] 設定已儲存至 {PRODUCT_CONFIG_PATH}: {serial}')
    except Exception as e:
        log.warning(f'[Product] 設定儲存失敗: {e}')

def load_product_config() -> Optional[dict]:
    try:
        if not os.path.exists(PRODUCT_CONFIG_PATH):
            return None
        with open(PRODUCT_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f'[Product] 設定讀取失敗: {e}')
        return None


# ──────────────────────────────────────────────────────────────────
# Log / Storage 設定檔讀寫（v4：支援本機路徑 + Samba + HTTP 上傳）
# ──────────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_CONFIG_PATH = os.path.join(APP_DIR, 'storage_config.json')
STORAGE_SECRET_DIR = os.path.join(APP_DIR, 'secret')
SMB_CRED_PATH = os.path.join(STORAGE_SECRET_DIR, 'smb_storage.cred')
DEFAULT_STORAGE_CONFIG = {
    'log_enabled': False,
    'image_enabled': False,
    'storage_type': 'local',          # local | samba | http
    'root_path': '/data/tm_app/inspection_archive',
    # Samba / Windows share（需要系統支援 CIFS；此 QCS6490 image 多半不可用）
    'samba_url': '',
    'samba_username': '',
    'samba_domain': '',
    'samba_password_saved': False,
    'samba_subdir': '',
    'mount_point': '/mnt/tm_inspect_storage',
    'smb_version': '3.0',
    # HTTP storage server（推薦：不需要 CIFS，不需要 mount）
    'http_url': '',                   # https://PC_IP:9000
    'http_token': '',                 # optional shared token
    'http_subdir': 'AI_Camera_01',
    'http_timeout_sec': 10,
    'save_mode': 'fail_only',         # fail_only | all | fail_and_pass_sample
    'pass_sample_n': 100,
    'save_raw': False,
    'save_result': True,
    'jpeg_quality': 85,
    'retain_days': 30,
    'max_gb': 20,
    'warn_percent': 80,
    'critical_percent': 90,
    'cleanup_interval_results': 100,
}

def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(v)

def _safe_component(v: str) -> str:
    v = (v or '').strip().replace('\\', '/')
    parts = [x for x in v.split('/') if x and x not in ('.', '..')]
    return '/'.join(parts)

def normalize_samba_url(url: str) -> str:
    url = (url or '').strip().replace('\\', '/')
    if url.startswith('smb://'):
        url = '//' + url[6:]
    if url.startswith('////'):
        url = '//' + url.lstrip('/')
    if url and not url.startswith('//'):
        url = '//' + url.lstrip('/')
    return url.rstrip('/')

def normalize_http_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    if url and not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url
    return url

def normalize_storage_config(data: Optional[dict], base: Optional[dict] = None) -> Dict:
    cfg = dict(DEFAULT_STORAGE_CONFIG)
    if base:
        cfg.update(base)
    data = dict(data or {})
    samba_password = data.pop('samba_password', None)
    if data:
        cfg.update(data)
    cfg['log_enabled'] = _as_bool(cfg.get('log_enabled'))
    cfg['image_enabled'] = _as_bool(cfg.get('image_enabled'))
    cfg['save_raw'] = _as_bool(cfg.get('save_raw'))
    cfg['save_result'] = _as_bool(cfg.get('save_result'))
    cfg['storage_type'] = str(cfg.get('storage_type') or 'local').lower()
    if cfg['storage_type'] not in ('local', 'samba', 'http'):
        cfg['storage_type'] = 'local'
    cfg['root_path'] = os.path.abspath(str(cfg.get('root_path') or DEFAULT_STORAGE_CONFIG['root_path']))
    cfg['mount_point'] = os.path.abspath(str(cfg.get('mount_point') or DEFAULT_STORAGE_CONFIG['mount_point']))
    cfg['samba_url'] = normalize_samba_url(str(cfg.get('samba_url') or ''))
    cfg['samba_username'] = str(cfg.get('samba_username') or '').strip()
    cfg['samba_domain'] = str(cfg.get('samba_domain') or '').strip()
    cfg['samba_subdir'] = _safe_component(str(cfg.get('samba_subdir') or ''))
    cfg['smb_version'] = str(cfg.get('smb_version') or '3.0').strip() or '3.0'
    cfg['http_url'] = normalize_http_url(str(cfg.get('http_url') or ''))
    cfg['http_token'] = str(cfg.get('http_token') or '').strip()
    cfg['http_subdir'] = _safe_component(str(cfg.get('http_subdir') or 'AI_Camera_01'))
    cfg['http_timeout_sec'] = max(2, min(60, int(cfg.get('http_timeout_sec') or 10)))
    if cfg.get('save_mode') not in ('fail_only', 'all', 'fail_and_pass_sample'):
        cfg['save_mode'] = 'fail_only'
    cfg['pass_sample_n'] = max(1, int(cfg.get('pass_sample_n') or 100))
    cfg['jpeg_quality'] = max(30, min(95, int(cfg.get('jpeg_quality') or 85)))
    cfg['retain_days'] = max(0, int(cfg.get('retain_days') or 0))
    cfg['max_gb'] = max(0, float(cfg.get('max_gb') or 0))
    cfg['warn_percent'] = max(1, min(99, int(cfg.get('warn_percent') or DEFAULT_STORAGE_CONFIG['warn_percent'])))
    cfg['critical_percent'] = max(cfg['warn_percent'] + 1, min(100, int(cfg.get('critical_percent') or DEFAULT_STORAGE_CONFIG['critical_percent'])))
    cfg['cleanup_interval_results'] = max(10, int(cfg.get('cleanup_interval_results') or DEFAULT_STORAGE_CONFIG['cleanup_interval_results']))
    cfg['samba_password_saved'] = bool(cfg.get('samba_password_saved')) or os.path.exists(SMB_CRED_PATH)
    if samba_password not in (None, ''):
        cfg['_samba_password_to_save'] = str(samba_password)
    return cfg

def _public_storage_config(cfg: Dict) -> Dict:
    out = dict(cfg or {})
    out.pop('_samba_password_to_save', None)
    out.pop('samba_password', None)
    out['samba_password_saved'] = bool(out.get('samba_password_saved')) or os.path.exists(SMB_CRED_PATH)
    out['effective_root'] = effective_storage_root(out)
    out['samba_mounted'] = is_mounted(out.get('mount_point', DEFAULT_STORAGE_CONFIG['mount_point']))
    return out

def _write_samba_credentials(cfg: Dict, password: str) -> None:
    os.makedirs(STORAGE_SECRET_DIR, exist_ok=True)
    lines = [f"username={cfg.get('samba_username','')}", f"password={password}"]
    if cfg.get('samba_domain'):
        lines.append(f"domain={cfg.get('samba_domain')}")
    tmp = SMB_CRED_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, SMB_CRED_PATH)
    os.chmod(SMB_CRED_PATH, 0o600)

def load_storage_config(default_log_enabled: Optional[bool] = None) -> Dict:
    cfg = dict(DEFAULT_STORAGE_CONFIG)
    if default_log_enabled is not None:
        cfg['log_enabled'] = bool(default_log_enabled)
    try:
        if os.path.exists(STORAGE_CONFIG_PATH):
            with open(STORAGE_CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f) or {})
    except Exception as e:
        log.warning(f'[Storage] 設定讀取失敗: {e}')
    cfg = normalize_storage_config(cfg)
    return _public_storage_config(cfg)

def save_storage_config(cfg: Dict) -> Dict:
    clean = normalize_storage_config(cfg)
    password = clean.pop('_samba_password_to_save', None)
    if clean.get('storage_type') == 'samba' and password is not None:
        _write_samba_credentials(clean, password)
        clean['samba_password_saved'] = True
    safe = _public_storage_config(clean)
    tmp = STORAGE_CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({k: v for k, v in safe.items() if k not in ('effective_root', 'samba_mounted')}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORAGE_CONFIG_PATH)
    log.info(f"[Storage] 設定已儲存至 {STORAGE_CONFIG_PATH} type={safe.get('storage_type')} root={safe.get('effective_root')}")
    return safe

def _run_cmd(cmd: List[str], timeout: int = 12) -> Tuple[bool, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        out = (p.stdout or '') + (p.stderr or '')
        return p.returncode == 0, out.strip()
    except FileNotFoundError as e:
        return False, f'找不到指令：{cmd[0]} ({e})'
    except subprocess.TimeoutExpired:
        return False, f'指令逾時：{" ".join(cmd[:3])} ...'
    except Exception as e:
        return False, str(e)

def is_mounted(path: str) -> bool:
    path = os.path.abspath(path)
    try:
        with open('/proc/mounts', 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and os.path.abspath(parts[1].replace('\\040', ' ')) == path:
                    return True
    except Exception:
        pass
    ok, _ = _run_cmd(['mountpoint', '-q', path], timeout=3)
    return ok

def effective_storage_root(cfg: Dict) -> str:
    st = (cfg or {}).get('storage_type')
    if st == 'samba':
        root = os.path.abspath(str((cfg or {}).get('mount_point') or DEFAULT_STORAGE_CONFIG['mount_point']))
        sub = _safe_component(str((cfg or {}).get('samba_subdir') or ''))
        return os.path.join(root, sub) if sub else root
    if st == 'http':
        base = normalize_http_url(str((cfg or {}).get('http_url') or ''))
        sub = _safe_component(str((cfg or {}).get('http_subdir') or ''))
        return (base + ('/' + sub if sub else '')) if base else ''
    return os.path.abspath(str((cfg or {}).get('root_path') or DEFAULT_STORAGE_CONFIG['root_path']))

def _http_headers(cfg: Dict) -> Dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    token = str(cfg.get('http_token') or '').strip()
    if token:
        headers['X-Storage-Token'] = token
    return headers

def _http_json(url: str, payload: Optional[dict] = None, headers: Optional[dict] = None, timeout: int = 10) -> Tuple[bool, Dict]:
    try:
        data = None
        method = 'GET'
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            method = 'POST'
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        try:
            return True, json.loads(raw or '{}')
        except Exception:
            return True, {'raw': raw}
    except Exception as e:
        return False, {'error': str(e)}

def http_storage_health(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    if not cfg.get('http_url'):
        return {'ok': False, 'error': '請輸入 HTTPS 儲存服務 URL，例如 https://PC_IP:9000（舊環境可明確輸入 http://）'}
    url = cfg['http_url'].rstrip('/') + '/health'
    ok, data = _http_json(url, headers=_http_headers(cfg), timeout=cfg.get('http_timeout_sec', 10))
    if not ok:
        return {'ok': False, 'error': data.get('error') or 'HTTP 儲存服務無回應', 'url': url}
    if not data.get('ok', True):
        return {'ok': False, 'error': data.get('error') or str(data), 'url': url}
    return {'ok': True, 'url': cfg['http_url'], 'message': data.get('message','HTTP storage ready'), 'server': data}

def http_storage_status(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    if not cfg.get('http_url'):
        return {'ok': False, 'error': '尚未設定 HTTP URL'}
    url = cfg['http_url'].rstrip('/') + '/status'
    ok, data = _http_json(url, headers=_http_headers(cfg), timeout=cfg.get('http_timeout_sec', 10))
    return data if ok else {'ok': False, 'error': data.get('error') or 'HTTP status failed'}

def mount_samba_storage(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    if cfg.get('storage_type') != 'samba':
        return {'ok': True, 'mounted': False, 'message': '目前不是 Samba 模式'}
    if not cfg.get('samba_url') or cfg.get('samba_url').count('/') < 3:
        return {'ok': False, 'mounted': False, 'error': 'Samba 位址格式錯誤，請填 //IP/share 或 smb://IP/share'}
    if not cfg.get('samba_username'):
        return {'ok': False, 'mounted': False, 'error': '請輸入 Samba 帳號'}
    if not os.path.exists(SMB_CRED_PATH):
        return {'ok': False, 'mounted': False, 'error': '尚未保存 Samba 密碼，請輸入密碼後按「套用」或「測試連線」'}
    mp = cfg['mount_point']
    try:
        os.makedirs(mp, exist_ok=True)
    except Exception as e:
        return {'ok': False, 'mounted': False, 'error': f'建立掛載點失敗：{e}'}
    if is_mounted(mp):
        return {'ok': True, 'mounted': True, 'mount_point': mp, 'message': '已掛載'}
    if os.geteuid() != 0:
        return {'ok': False, 'mounted': False, 'error': '目前程式不是 root，無法執行 mount.cifs'}
    opts = f"credentials={SMB_CRED_PATH},vers={cfg.get('smb_version','3.0')},iocharset=utf8,file_mode=0777,dir_mode=0777,noserverino"
    ok, out = _run_cmd(['mount', '-t', 'cifs', cfg['samba_url'], mp, '-o', opts], timeout=15)
    if not ok:
        msg = out or 'mount.cifs 失敗'
        if 'unknown filesystem type' in msg or 'cifs' in msg.lower():
            msg += '；目前裝置可能未啟用 CIFS/SMB，建議改用 HTTP 上傳模式。'
        return {'ok': False, 'mounted': False, 'mount_point': mp, 'error': msg[-500:]}
    return {'ok': True, 'mounted': True, 'mount_point': mp, 'message': 'Samba 掛載成功'}

def unmount_samba_storage(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    mp = cfg.get('mount_point') or DEFAULT_STORAGE_CONFIG['mount_point']
    if not is_mounted(mp):
        return {'ok': True, 'mounted': False, 'message': '原本未掛載'}
    if os.geteuid() != 0:
        return {'ok': False, 'mounted': True, 'error': '目前程式不是 root，無法卸載'}
    ok, out = _run_cmd(['umount', mp], timeout=10)
    return {'ok': ok, 'mounted': is_mounted(mp), 'message': out if ok else '', 'error': '' if ok else out[-500:]}

def ensure_storage_ready(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    if cfg.get('storage_type') == 'http':
        return http_storage_health(cfg)
    if cfg.get('storage_type') == 'samba':
        res = mount_samba_storage(cfg)
        if not res.get('ok'):
            return res
    root = effective_storage_root(cfg)
    try:
        os.makedirs(root, exist_ok=True)
        return {'ok': True, 'root_path': root, 'storage_type': cfg.get('storage_type'), 'mounted': is_mounted(cfg.get('mount_point','')) if cfg.get('storage_type') == 'samba' else False}
    except Exception as e:
        return {'ok': False, 'root_path': root, 'error': str(e)}

def _folder_usage(root: str) -> Dict:
    total = 0; files = 0; dirs = 0; errors = []
    if not root or not os.path.exists(root):
        return {'bytes': 0, 'files': 0, 'dirs': 0, 'human': '0 MB', 'errors': []}
    for dirpath, dirnames, names in os.walk(root):
        dirs += len(dirnames)
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path); total += int(st.st_size); files += 1
            except Exception as e:
                if len(errors) < 5: errors.append(f'{path}: {e}')
    return {'bytes': total, 'files': files, 'dirs': dirs, 'human': _human_bytes(total), 'errors': errors}

def _human_bytes(n: int) -> str:
    try: n = float(n or 0)
    except Exception: n = 0.0
    units = ['B','KB','MB','GB','TB']; i = 0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0; i += 1
    return f'{int(n)} {units[i]}' if i == 0 else f'{n:.1f} {units[i]}'

def _statvfs_usage(path: str) -> Dict:
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe: break
        probe = parent
    if not probe: probe = '/'
    st = os.statvfs(probe)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    pct = (used / total * 100.0) if total else 0.0
    return {'path_checked': probe, 'total_bytes': int(total), 'free_bytes': int(free), 'used_bytes': int(used), 'total_human': _human_bytes(total), 'free_human': _human_bytes(free), 'used_human': _human_bytes(used), 'used_percent': round(pct, 1)}

def storage_status(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    warnings = []
    ready = {'ok': True}
    root = effective_storage_root(cfg)
    if cfg.get('storage_type') == 'http':
        ready = http_storage_health(cfg) if cfg.get('image_enabled') else {'ok': True}
        srv = http_storage_status(cfg) if cfg.get('http_url') else {}
        archive_usage = srv.get('archive_usage') or {'bytes': 0, 'files': 0, 'dirs': 0, 'human': '由 PC 端管理'}
        fs_usage = srv.get('fs_usage') or {'used_percent': 0, 'used_human': '—', 'total_human': '—', 'free_human': '—'}
        if cfg.get('image_enabled'):
            warnings.append('目前使用 HTTP 上傳到 PC，AI Camera 本機不會大量存圖；容量與清理由 PC storage_server.py 管理。')
    else:
        ready = ensure_storage_ready(cfg) if cfg.get('storage_type') == 'samba' else {'ok': True}
        archive_usage = _folder_usage(root)
        try:
            fs_usage = _statvfs_usage(root)
        except Exception as e:
            fs_usage = {'error': str(e)}; warnings.append(f'無法取得磁碟容量：{e}')
        if cfg.get('image_enabled') and cfg.get('storage_type') == 'local':
            warnings.append('目前使用本機路徑存圖，請確認保留天數與最大 GB 已設定，避免 AI Camera 儲存空間被塞滿。')
    max_gb = float(cfg.get('max_gb') or 0)
    quota_bytes = int(max_gb * 1024 * 1024 * 1024) if max_gb > 0 else 0
    quota_percent = round((archive_usage.get('bytes',0) / quota_bytes * 100.0), 1) if quota_bytes else 0.0
    quota = {'max_gb': max_gb, 'max_bytes': quota_bytes, 'max_human': _human_bytes(quota_bytes) if quota_bytes else '未限制', 'used_percent': quota_percent}
    if cfg.get('image_enabled'):
        if quota_bytes and archive_usage.get('bytes',0) >= quota_bytes * (float(cfg.get('critical_percent',90))/100.0):
            warnings.append(f"歷史圖片容量已達 {quota_percent}% ，已接近或超過上限，建議立即手動清理。")
        elif quota_bytes and archive_usage.get('bytes',0) >= quota_bytes * (float(cfg.get('warn_percent',80))/100.0):
            warnings.append(f"歷史圖片容量已達 {quota_percent}% ，接近上限。")
        free_bytes = fs_usage.get('free_bytes') if isinstance(fs_usage, dict) else None
        if isinstance(free_bytes, int) and free_bytes < 1024*1024*1024:
            warnings.append(f"檔案系統剩餘空間低於 1GB：{_human_bytes(free_bytes)}。")
    return {'ok': bool(ready.get('ok')), **_public_storage_config(cfg), 'ready': ready, 'usage': fs_usage, 'fs_usage': fs_usage, 'archive_usage': archive_usage, 'quota': quota, 'warnings': warnings, 'rotate': {'retain_days': int(cfg.get('retain_days') or 0), 'max_gb': max_gb, 'cleanup_interval_results': int(cfg.get('cleanup_interval_results') or DEFAULT_STORAGE_CONFIG['cleanup_interval_results']), 'rule': '本機/Samba：先刪過期，再依容量刪最舊 PASS→FAIL；HTTP：由 PC 端 storage_server.py 執行同樣規則。'}}

def should_archive_image(cfg: Dict, stamp: str, pass_counter: int) -> bool:
    if not cfg.get('image_enabled'):
        return False
    mode = cfg.get('save_mode', 'fail_only')
    if mode == 'all': return True
    if stamp == 'FAIL': return True
    if mode == 'fail_and_pass_sample':
        n = max(1, int(cfg.get('pass_sample_n') or 100))
        return (pass_counter % n) == 0
    return False

def _cv2_to_jpg_b64(img: np.ndarray, quality: int) -> str:
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError('JPEG encode failed')
    return base64.b64encode(buf.tobytes()).decode('ascii')

def _save_archive_images_http(frame: np.ndarray, vis: Optional[np.ndarray], product: Dict, stamp: str, cfg: Dict, seq: int) -> Dict:
    status = {'raw_image_path': '', 'result_image_path': '', 'storage_status': 'NONE'}
    q = int(cfg.get('jpeg_quality') or 85)
    payload = {
        'subdir': cfg.get('http_subdir') or '',
        'product_serial': str(product.get('serial') or 'UNKNOWN'),
        'stamp': stamp,
        'seq': int(seq),
        'timestamp': datetime.now().isoformat(timespec='milliseconds'),
        'save_raw': bool(cfg.get('save_raw')),
        'save_result': bool(cfg.get('save_result')),
        'raw_jpeg_b64': '',
        'result_jpeg_b64': '',
    }
    if cfg.get('save_raw'):
        payload['raw_jpeg_b64'] = _cv2_to_jpg_b64(frame, q)
    if cfg.get('save_result') and vis is not None:
        payload['result_jpeg_b64'] = _cv2_to_jpg_b64(vis, q)
    if not payload['raw_jpeg_b64'] and not payload['result_jpeg_b64']:
        status['storage_status'] = 'SKIPPED'
        return status
    url = cfg['http_url'].rstrip('/') + '/upload'
    ok, data = _http_json(url, payload=payload, headers=_http_headers(cfg), timeout=cfg.get('http_timeout_sec', 10))
    if not ok or not data.get('ok'):
        status['storage_status'] = f"FAILED: {data.get('error') or 'HTTP upload failed'}"[:200]
        return status
    status['raw_image_path'] = data.get('raw_url') or ''
    status['result_image_path'] = data.get('result_url') or ''
    status['storage_status'] = 'SAVED_HTTP' if (status['raw_image_path'] or status['result_image_path']) else 'SKIPPED'
    return status

def save_archive_images(frame: np.ndarray, vis: Optional[np.ndarray], product: Dict, stamp: str, cfg: Dict, seq: int) -> Dict:
    status = {'raw_image_path': '', 'result_image_path': '', 'storage_status': 'NONE'}
    if not cfg.get('image_enabled'):
        return status
    ready = ensure_storage_ready(cfg)
    if not ready.get('ok'):
        status['storage_status'] = f"FAILED: {ready.get('error') or 'storage not ready'}"[:200]
        log.warning(f"[Storage] 未就緒：{ready}")
        return status
    if cfg.get('storage_type') == 'http':
        try:
            return _save_archive_images_http(frame, vis, product, stamp, cfg, seq)
        except Exception as e:
            status['storage_status'] = f'FAILED: {e}'[:200]
            log.warning(f'[Storage] HTTP 圖片上傳失敗: {e}')
            return status
    try:
        root = effective_storage_root(cfg)
        serial = str(product.get('serial') or 'UNKNOWN').replace('/', '_').replace('\\', '_')
        day = datetime.now().strftime('%Y-%m-%d')
        folder = os.path.join(root, day, serial, stamp)
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        q = int(cfg.get('jpeg_quality') or 85)
        if cfg.get('save_raw'):
            raw_path = os.path.join(folder, f'{ts}_{serial}_{stamp}_{seq:08d}_raw.jpg')
            if cv2.imwrite(raw_path, frame, [cv2.IMWRITE_JPEG_QUALITY, q]): status['raw_image_path'] = raw_path
        if cfg.get('save_result') and vis is not None:
            res_path = os.path.join(folder, f'{ts}_{serial}_{stamp}_{seq:08d}_result.jpg')
            if cv2.imwrite(res_path, vis, [cv2.IMWRITE_JPEG_QUALITY, q]): status['result_image_path'] = res_path
        status['storage_status'] = 'SAVED' if (status['raw_image_path'] or status['result_image_path']) else 'SKIPPED'
    except Exception as e:
        status['storage_status'] = f'FAILED: {e}'[:200]
        log.warning(f'[Storage] 圖片保存失敗: {e}')
    return status

def cleanup_storage(root_or_cfg, retain_days: int = 0, max_gb: float = 0) -> Dict:
    if isinstance(root_or_cfg, dict):
        cfg = normalize_storage_config(root_or_cfg)
        if cfg.get('storage_type') == 'http':
            url = cfg.get('http_url','').rstrip('/') + '/cleanup'
            payload = {'retain_days': int(cfg.get('retain_days') or 0), 'max_gb': float(cfg.get('max_gb') or 0)}
            ok, data = _http_json(url, payload=payload, headers=_http_headers(cfg), timeout=30)
            return data if ok else {'ok': False, 'deleted_files': 0, 'freed_bytes': 0, 'errors': [data.get('error') or 'HTTP cleanup failed']}
        ready = ensure_storage_ready(cfg)
        root = effective_storage_root(cfg)
        retain_days = int(cfg.get('retain_days') or retain_days or 0)
        max_gb = float(cfg.get('max_gb') or max_gb or 0)
        if not ready.get('ok'):
            return {'ok': False, 'root_path': root, 'deleted_files': 0, 'freed_bytes': 0, 'errors': [ready.get('error','storage not ready')]}
    else:
        root = os.path.abspath(str(root_or_cfg or DEFAULT_STORAGE_CONFIG['root_path']))
    result = {'ok': True, 'root_path': root, 'deleted_files': 0, 'freed_bytes': 0, 'errors': []}
    if not os.path.exists(root): return result
    files = []
    now = time.time()
    for dirpath, _, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path); files.append((path, st.st_mtime, st.st_size))
            except Exception as e:
                result['errors'].append(str(e))
    def delete(path, size):
        try:
            os.remove(path); result['deleted_files'] += 1; result['freed_bytes'] += int(size); return True
        except Exception as e:
            result['errors'].append(f'{path}: {e}'); return False
    if retain_days > 0:
        cutoff = now - retain_days * 86400; keep = []
        for path, mtime, size in files:
            if mtime < cutoff: delete(path, size)
            else: keep.append((path, mtime, size))
        files = keep
    if max_gb and max_gb > 0:
        max_bytes = int(max_gb * 1024 * 1024 * 1024)
        total = sum(size for _, _, size in files)
        if total > max_bytes:
            files.sort(key=lambda x: (0 if '/PASS/' in x[0].replace('\\','/') else 1, x[1]))
            for path, mtime, size in files:
                if total <= max_bytes: break
                if delete(path, size): total -= size
    return result

def test_storage_write(cfg: Dict) -> Dict:
    cfg = normalize_storage_config(cfg)
    if cfg.get('storage_type') == 'http':
        url = cfg.get('http_url','').rstrip('/') + '/test'
        payload = {'subdir': cfg.get('http_subdir') or '', 'message': 'tm-inspect write test', 'timestamp': datetime.now().isoformat(timespec='seconds')}
        ok, data = _http_json(url, payload=payload, headers=_http_headers(cfg), timeout=cfg.get('http_timeout_sec', 10))
        if ok and data.get('ok'):
            return {'ok': True, 'root_path': data.get('root_path') or cfg.get('http_url'), 'storage_type': 'http', 'server': data}
        return {'ok': False, 'root_path': cfg.get('http_url'), 'error': data.get('error') or 'HTTP 測試失敗'}
    ready = ensure_storage_ready(cfg)
    if not ready.get('ok'):
        return {'ok': False, 'root_path': effective_storage_root(cfg), 'error': ready.get('error') or str(ready)}
    try:
        root = effective_storage_root(cfg)
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, f'.tm_inspect_write_test_{int(time.time())}.tmp')
        with open(probe, 'w', encoding='utf-8') as f: f.write('ok')
        with open(probe, 'r', encoding='utf-8') as f: txt = f.read()
        os.remove(probe)
        if txt != 'ok': return {'ok': False, 'root_path': root, 'error': '測試檔讀回內容不一致'}
        return {'ok': True, 'root_path': root, 'storage_type': cfg.get('storage_type'), 'mounted': is_mounted(cfg.get('mount_point','')) if cfg.get('storage_type') == 'samba' else False}
    except Exception as e:
        return {'ok': False, 'root_path': effective_storage_root(cfg), 'error': str(e)}

def load_first_available_product(db_path: str) -> Optional[Dict]:
    """找第一個有有效 template 的產品，避免沒有 last_product 時 tm-infer 起不來。"""
    try:
        conn = get_db(db_path)
        row = conn.execute("""
            SELECT p.*
            FROM products p
            WHERE EXISTS (
                SELECT 1 FROM regions r
                WHERE r.product_id = p.id
                  AND r.template_b64 IS NOT NULL
                  AND r.template_b64 != ''
            )
            ORDER BY p.id
            LIMIT 1
        """).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.warning(f'[Product] 自動選取產品失敗: {e}')
        return None

def resolve_startup_product(db_path: str, cli_serial: str = '') -> Tuple[Optional[Dict], str]:
    """解析啟動產品，回傳 (product, source)。source 用於 log 顯示。"""
    cli_serial = (cli_serial or '').strip()
    if cli_serial:
        p = load_product(db_path, cli_serial)
        return p, f'cli:{cli_serial}'

    cfg = load_product_config()
    serial = ''
    if cfg:
        serial = str(cfg.get('last_product_serial') or cfg.get('serial') or '').strip()
    if serial:
        p = load_product(db_path, serial)
        if p:
            return p, f'config:{serial}'
        log.warning(f'[Product] product_config.json 指定的產品不存在: {serial}，改用自動選取')

    p = load_first_available_product(db_path)
    if p:
        return p, f'auto-first:{p.get("serial")}'
    return None, 'none'


# ──────────────────────────────────────────────────────────────────
# Camera 設定檔讀寫
# ──────────────────────────────────────────────────────────────────
CAMERA_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'camera_config.json')
VISION_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vision_config.json')

def save_camera_config(cfg: dict):
    try:
        with open(CAMERA_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log.info(f'[Camera] 設定已儲存至 {CAMERA_CONFIG_PATH}')
    except Exception as e:
        log.warning(f'[Camera] 設定儲存失敗: {e}')

def load_camera_config() -> Optional[dict]:
    try:
        if not os.path.exists(CAMERA_CONFIG_PATH):
            return None
        with open(CAMERA_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f'[Camera] 設定讀取失敗: {e}')
        return None


def save_vision_config(cfg: dict):
    try:
        v = {k: cfg.get(k) for k in ('digital_zoom_enabled', 'digital_zoom', 'zoom_center_x', 'zoom_center_y') if k in cfg}
        v = normalize_image_transform_config(v)
        with open(VISION_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(v, f, ensure_ascii=False, indent=2)
        log.info(f'[Vision] 共用 zoom 設定已儲存至 {VISION_CONFIG_PATH}')
    except Exception as e:
        log.warning(f'[Vision] 設定儲存失敗: {e}')


def load_vision_config() -> Optional[dict]:
    try:
        if not os.path.exists(VISION_CONFIG_PATH):
            return None
        with open(VISION_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f'[Vision] 設定讀取失敗: {e}')
        return None

_VISION_CACHE = {'mtime': None, 'cfg': None}

def get_runtime_vision_config() -> Optional[dict]:
    # Read shared vision_config.json written by app.py / index.html; cache by mtime.
    try:
        if not os.path.exists(VISION_CONFIG_PATH):
            return None
        mt = os.path.getmtime(VISION_CONFIG_PATH)
        if _VISION_CACHE.get('mtime') == mt:
            return _VISION_CACHE.get('cfg')
        cfg = load_vision_config()
        _VISION_CACHE['mtime'] = mt
        _VISION_CACHE['cfg'] = cfg
        return cfg
    except Exception:
        return _VISION_CACHE.get('cfg')


# ──────────────────────────────────────────────────────────────────
# Camera 手動設定/熱更新 helpers（qtiqmmfsrc）
# ──────────────────────────────────────────────────────────────────
CAMERA_PROP_SPECS = {
    'white_balance_mode':      {'gst': 'white-balance-mode',      'min': 0,  'max': 10,   'default': 1},
    'antibanding':             {'gst': 'antibanding',             'min': 0,  'max': 3,    'default': 3},
    'iso_mode':                {'gst': 'iso-mode',                'min': 0,  'max': 8,    'default': 0},
    'manual_iso_value':        {'gst': 'manual-iso-value',        'min': 100,'max': 3200, 'default': 800},
    'exposure_compensation':   {'gst': 'exposure-compensation',   'min': -12,'max': 12,   'default': 0},
    'contrast':                {'gst': 'contrast',                'min': 1,  'max': 10,   'default': 5},
    'saturation':              {'gst': 'saturation',              'min': 0,  'max': 10,   'default': 5},
    'sharpness':               {'gst': 'sharpness',               'min': 0,  'max': 6,    'default': 2},
}

def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return default

def _clamp_float(v, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return default

IMAGE_TRANSFORM_DEFAULTS = {
    'digital_zoom_enabled': False,
    'digital_zoom': 1.0,
    'zoom_center_x': 0.5,
    'zoom_center_y': 0.5,
}

def normalize_image_transform_config(src: Optional[dict], base: Optional[dict] = None) -> Dict:
    out = dict(base or {})
    src = src or {}
    if 'digital_zoom_enabled' in src:
        out['digital_zoom_enabled'] = bool(src.get('digital_zoom_enabled'))
    elif 'digital_zoom_enabled' not in out:
        out['digital_zoom_enabled'] = IMAGE_TRANSFORM_DEFAULTS['digital_zoom_enabled']
    if 'digital_zoom' in src:
        out['digital_zoom'] = round(_clamp_float(src.get('digital_zoom'), 1.0, 4.0, 1.0), 2)
    elif 'digital_zoom' not in out:
        out['digital_zoom'] = IMAGE_TRANSFORM_DEFAULTS['digital_zoom']
    if 'zoom_center_x' in src:
        out['zoom_center_x'] = round(_clamp_float(src.get('zoom_center_x'), 0.05, 0.95, 0.5), 3)
    elif 'zoom_center_x' not in out:
        out['zoom_center_x'] = IMAGE_TRANSFORM_DEFAULTS['zoom_center_x']
    if 'zoom_center_y' in src:
        out['zoom_center_y'] = round(_clamp_float(src.get('zoom_center_y'), 0.05, 0.95, 0.5), 3)
    elif 'zoom_center_y' not in out:
        out['zoom_center_y'] = IMAGE_TRANSFORM_DEFAULTS['zoom_center_y']
    return out

def get_video_rotate_code(cap) -> Optional[int]:
    """讀取影片容器內的旋轉 metadata（常見於手機錄影，感光元件是橫向拍攝，
    靠這個標籤告訴播放器「顯示時請轉正」），回傳對應的 cv2.rotate() code。

    背景：部分 OpenCV/FFmpeg 組合會透過 CAP_PROP_ORIENTATION_AUTO 自動套用這個
    旋轉，但這個行為在不同 OpenCV 版本、不同平台的預編譯 wheel 之間並不一致
    ——同一支影片，在一台機器上顯示正常，換一台機器（或换一個 OpenCV 版本）
    就可能是顛倒或側躺的，且無法從程式碼判斷「這次自動旋轉到底有沒有生效」。

    這裡不依賴那個隱性行為：明確關閉 CAP_PROP_ORIENTATION_AUTO，自己讀取
    CAP_PROP_ORIENTATION_META 的角度值，自己決定要不要轉、轉多少——行為在
    任何機器、任何 OpenCV 版本上都一致，不會因環境不同而有時候正常、有時候顛倒。
    """
    try:
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        angle = cap.get(cv2.CAP_PROP_ORIENTATION_META)
    except Exception:
        return None
    try:
        angle = round(float(angle or 0.0)) % 360
    except (TypeError, ValueError):
        return None
    if angle == 90:
        return cv2.ROTATE_90_CLOCKWISE
    if angle == 180:
        return cv2.ROTATE_180
    if angle == 270:
        return cv2.ROTATE_90_COUNTERCLOCKWISE
    return None


def apply_rotate_code(frame: Optional[np.ndarray], rotate_code: Optional[int]) -> Optional[np.ndarray]:
    """套用 get_video_rotate_code() 回傳的旋轉 code；沒有旋轉需求時原樣回傳。"""
    if frame is None or rotate_code is None:
        return frame
    return cv2.rotate(frame, rotate_code)


def rotated_frame_size(width: int, height: int, rotate_code: Optional[int]) -> Tuple[int, int]:
    """90°/270° 旋轉後寬高會互換，180° 或不旋轉則維持原樣——用來讓回報給前端
    的影片寬高資訊跟實際旋轉後的畫面一致。"""
    if rotate_code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        return height, width
    return width, height


def apply_digital_zoom(frame: np.ndarray, cfg: Optional[Dict]) -> np.ndarray:
    """Digital zoom v1: crop around normalized center and resize back to original size.
    This keeps output resolution stable, so the rest of the inference pipeline does not change.
    """
    cfg = dict(cfg or {})
    runtime_cfg = get_runtime_vision_config()
    if runtime_cfg:
        cfg.update(normalize_image_transform_config(runtime_cfg))
    if not bool(cfg.get('digital_zoom_enabled', False)):
        return frame
    zoom = _clamp_float(cfg.get('digital_zoom', 1.0), 1.0, 4.0, 1.0)
    if zoom <= 1.001:
        return frame
    h, w = frame.shape[:2]
    crop_w = max(2, int(round(w / zoom)))
    crop_h = max(2, int(round(h / zoom)))
    cx = int(round(_clamp_float(cfg.get('zoom_center_x', 0.5), 0.05, 0.95, 0.5) * w))
    cy = int(round(_clamp_float(cfg.get('zoom_center_y', 0.5), 0.05, 0.95, 0.5) * h))
    x1 = max(0, min(w - crop_w, cx - crop_w // 2))
    y1 = max(0, min(h - crop_h, cy - crop_h // 2))
    crop = frame[y1:y1 + crop_h, x1:x1 + crop_w]
    if crop.size == 0:
        return frame
    return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)

def normalize_camera_config(src: Optional[dict], base: Optional[dict] = None) -> Dict:
    """正規化相機參數。base 可用於 PATCH 式更新。"""
    out = dict(base or {})
    src = src or {}
    for key, spec in CAMERA_PROP_SPECS.items():
        if key in src:
            out[key] = _clamp_int(src.get(key), spec['min'], spec['max'], spec['default'])
        elif key not in out:
            out[key] = spec['default']
    out = normalize_image_transform_config(src, base=out)
    return out

def camera_config_from_args(args) -> Dict:
    return normalize_camera_config({
        'white_balance_mode':    getattr(args, 'wb_mode', 1),
        'antibanding':           getattr(args, 'antibanding', 3),
        'iso_mode':              getattr(args, 'iso_mode', 0),
        'manual_iso_value':      getattr(args, 'manual_iso_value', 800),
        'exposure_compensation': getattr(args, 'exposure_compensation', 0),
        'contrast':              getattr(args, 'contrast', 5),
        'saturation':            getattr(args, 'saturation', 5),
        'sharpness':             getattr(args, 'sharpness', 2),
    })

def apply_gst_camera_config(camsrc, cfg: Dict, reason: str = '', mode: str = 'safe') -> None:
    """把 camera config 套用到 qtiqmmfsrc。

    新版 QIR/QIM firmware 對部分舊版 qtiqmmfsrc vendor tag 較敏感。
    先用 safe/off 避開 antibanding / ISO / exposure / contrast 這類可能觸發
    HFRPreviewFPS 或 multicam_exptime warning 的手動控制；需要舊行為時再用
    --camera-controls manual。數位變焦仍在 Python frame 層處理，不依賴 qtiqmmfsrc。
    """
    if camsrc is None:
        return
    mode = str(mode or 'safe').strip().lower()
    if mode not in ('off', 'safe', 'manual'):
        mode = 'safe'

    if mode == 'off':
        log.info('[Camera] qtiqmmfsrc 手動控制已停用%s；僅保留 Python digital zoom 設定 zoom=%s/%sx center=(%s,%s)' % (
            f'({reason})' if reason else '',
            'ON' if cfg.get('digital_zoom_enabled') else 'OFF', cfg.get('digital_zoom'),
            cfg.get('zoom_center_x'), cfg.get('zoom_center_y')
        ))
        return

    if mode == 'safe':
        # Vendor sample on QIR only applies a minimal camera control.  Avoid old
        # manual exposure / ISO / antibanding properties unless explicitly requested.
        spec = CAMERA_PROP_SPECS.get('white_balance_mode')
        if spec and 'white_balance_mode' in cfg:
            try:
                camsrc.set_property(spec['gst'], int(cfg.get('white_balance_mode', 0)))
            except Exception as e:
                log.warning(f'[Camera] 設定 {spec["gst"]}={cfg.get("white_balance_mode")} 失敗: {e}')
        log.info('[Camera] 已套用安全相機設定%s: wb=%s；略過舊版手動 ISO/曝光/antibanding/色彩控制，zoom=%s/%sx center=(%s,%s)' % (
            f'({reason})' if reason else '',
            cfg.get('white_balance_mode'),
            'ON' if cfg.get('digital_zoom_enabled') else 'OFF', cfg.get('digital_zoom'),
            cfg.get('zoom_center_x'), cfg.get('zoom_center_y')
        ))
        return

    for key, spec in CAMERA_PROP_SPECS.items():
        if key not in cfg:
            continue
        try:
            camsrc.set_property(spec['gst'], int(cfg[key]))
        except Exception as e:
            log.warning(f'[Camera] 設定 {spec["gst"]}={cfg[key]} 失敗: {e}')
    log.info('[Camera] 已套用完整手動設定%s: wb=%s ab=%s iso=%s manual_iso=%s exp=%s contrast=%s saturation=%s sharpness=%s zoom=%s/%sx center=(%s,%s)' % (
        f'({reason})' if reason else '',
        cfg.get('white_balance_mode'), cfg.get('antibanding'), cfg.get('iso_mode'),
        cfg.get('manual_iso_value'), cfg.get('exposure_compensation'), cfg.get('contrast'),
        cfg.get('saturation'), cfg.get('sharpness'),
        'ON' if cfg.get('digital_zoom_enabled') else 'OFF', cfg.get('digital_zoom'),
        cfg.get('zoom_center_x'), cfg.get('zoom_center_y')
    ))

def apply_zlan_config(cfg: dict, shared: SharedState) -> bool:
    """從設定 dict 建立 ZlanController 並注入 shared，回傳是否成功。"""
    if not cfg.get('enabled', False):
        shared.set_zlan(None, 'free')
        return True
    try:
        zlan = Zlan6042Tcp(
            host=cfg['ip'],
            port=int(cfg.get('port', 502)),
            unit_id=int(cfg.get('unit', 1)),
            invert_low_active=bool(cfg.get('invert_di', False)),
        )
        if not zlan.connect():
            trigger = cfg.get('trigger', 'polling')
            shared.set_zlan(None, trigger)
            log.error(f'[ZLAN] 無法連線到 {cfg["ip"]}:{cfg.get("port", 502)}，保持 trigger={trigger}，不退回 free 連續推論')
            return False
        ctrl = ZlanController(
            zlan=zlan,
            ch_ok=int(cfg.get('ch_ok', 1)),
            ch_ng=int(cfg.get('ch_ng', 2)),
            pulse_ms=float(cfg.get('pulse_ms', 500)),
            free_repeat_sec=float(cfg.get('free_repeat_sec', 0.0)),
        )
        shared.set_zlan(ctrl, cfg.get('trigger', 'polling'))
        log.info(f'[ZLAN] 設定套用成功: {cfg["ip"]} trigger={cfg.get("trigger","polling")}')
        return True
    except Exception as e:
        log.error(f'[ZLAN] 套用設定失敗: {e}')
        return False



# ──────────────────────────────────────────────────────────────────
# 共用：待機 live preview 更新節流
# ──────────────────────────────────────────────────────────────────
def _live_preview_should_update(state: Dict, live_fps: float) -> bool:
    """依 live_fps 決定這一幀是否要重新 JPEG/base64 給前端 /live。"""
    if live_fps <= 0:
        return True
    now = time.time()
    interval = 1.0 / max(live_fps, 0.01)
    last = state.get('last_live_ts', 0.0)
    if (now - last) >= interval:
        state['last_live_ts'] = now
        return True
    return False


def _update_live_preview(frame: np.ndarray, shared: SharedState, state: Dict, args) -> bool:
    """更新乾淨原始 live frame。只做 preview，不做推論、不寫 DB、不送 DO。"""
    live_fps = float(getattr(args, 'live_fps', 5.0))
    if not _live_preview_should_update(state, live_fps):
        return False

    q = int(getattr(args, 'live_jpeg_quality', 70))
    q = max(30, min(95, q))
    ok, raw_buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        return False

    shared.update_live('data:image/jpeg;base64,' + base64.b64encode(raw_buf).decode())
    return True

# ──────────────────────────────────────────────────────────────────
# 共用：單幀推論結果處理
# ──────────────────────────────────────────────────────────────────
def _handle_result(
    frame, vis, all_pass, results, stamp,
    product, do_log, save_fail, writer, shared, db_path,
    zlan_ctrl: Optional[ZlanController] = None,
    generation: Optional[int] = None,
    rules: Optional[List[Dict]] = None,
    inference_mode: str = 'legacy_template',
):
    if not shared.is_generation_current(generation):
        serial = product.get('serial') if product else '?'
        log.info(f'[Switch] 丟棄舊產品推論結果，不回寫、不輸出 DO: serial={serial}, generation={generation}')
        return

    pid = product['id']

    # SOP Flow consumes per-frame rule-group matches and latches completed steps across time.
    sop_engine = shared.get_sop_engine()
    sop_summary = None
    sop_enabled = sop_engine is not None
    if sop_engine is not None:
        step_rules = [r for r in (rules or []) if r.get('id') is not None]
        sop_summary = sop_engine.update(step_rules)
        sop_state = sop_summary.get('state')
        if sop_state == 'COMPLETE':
            all_pass, stamp = True, 'PASS'
        elif sop_state == 'ALARM':
            all_pass, stamp = False, 'FAIL'
        else:
            all_pass, stamp = False, 'RUNNING'

    seq, pass_seq = shared.next_result_seq(stamp)
    storage_cfg = shared.get_storage_config()
    log_enabled = bool(storage_cfg.get('log_enabled', do_log))
    archive = {'raw_image_path': '', 'result_image_path': '', 'storage_status': 'NONE'}
    if should_archive_image(storage_cfg, stamp, pass_seq):
        archive = save_archive_images(frame, vis, product, stamp, storage_cfg, seq)

    if log_enabled:
        write_log(db_path, pid, results, stamp,
                  archive.get('raw_image_path',''), archive.get('result_image_path',''),
                  archive.get('storage_status','NONE'))

    # Lightweight automatic cleanup / rotate: run periodically in background so storage rules are not only manual.
    # Default interval is 100 results. User can adjust via cleanup_interval_results.
    cleanup_n = max(10, int(storage_cfg.get('cleanup_interval_results') or DEFAULT_STORAGE_CONFIG.get('cleanup_interval_results', 100)))
    if storage_cfg.get('image_enabled') and seq % cleanup_n == 0:
        threading.Thread(
            target=cleanup_storage,
            args=(storage_cfg,),
            daemon=True,
        ).start()

    if writer is not None and vis is not None:
        writer.write(vis)

    # Legacy CLI path remains for compatibility; new UI storage config is preferred.
    if save_fail and stamp == 'FAIL' and vis is not None:
        os.makedirs(save_fail, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        fn = os.path.join(save_fail, f'FAIL_{product["serial"]}_{ts}.jpg')
        cv2.imwrite(fn, vis)

    result_img = None
    if vis is not None:
        _, buf = cv2.imencode('.jpg', vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
        result_img = 'data:image/jpeg;base64,' + base64.b64encode(buf).decode()

    # 乾淨原圖（無標記框）供即時顯示和截圖用
    _, raw_buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    raw_img = 'data:image/jpeg;base64,' + base64.b64encode(raw_buf).decode()
    shared.update_live(raw_img)

    shared.update({
        'pass':       all_pass,
        'stamp':      stamp,
        'results':    results,
        'rules':      rules or [],
        'mode':       'sop_flow' if sop_enabled else inference_mode,
        'mode_label': 'SOP FLOW' if sop_enabled else {'multi_sample':'MULTI-SAMPLE', 'rule_group':'RULE-GROUP', 'legacy_template':'LEGACY TEMPLATE'}.get(inference_mode, inference_mode),
        'sop':        sop_summary or {'enabled': False, 'state': 'DISABLED', 'steps': [], 'alarm': {'active': False, 'seq': 0}},
        'result_img': result_img,
        'raw_img':    raw_img,
        'ts':         datetime.now().isoformat(timespec='milliseconds'),
        'product':    {'id': pid, 'serial': product['serial'], 'name': product.get('name')},
        'storage':    archive,
        'log_enabled': log_enabled,
    })

    # ── DO / stack-light output ───────────────────────────────────
    if zlan_ctrl is not None:
        if sop_enabled and sop_summary is not None:
            cfg = sop_summary.get('config') or {}
            state = sop_summary.get('state')
            if cfg.get('tower_light_enabled'):
                shared.apply_sop_output(sop_summary)
            elif state == 'COMPLETE':
                zlan_ctrl.send_ok(suppress_same_state=True)
            elif state == 'ALARM':
                zlan_ctrl.send_ng(suppress_same_state=True)
        else:
            # Legacy behavior.
            trigger_mode = getattr(shared, 'trigger_mode', 'free')
            suppress_same_state = (trigger_mode == 'free')
            if all_pass:
                queued = zlan_ctrl.send_ok(suppress_same_state=suppress_same_state)
                log.info('[ZLAN] → DO OK 已排隊' if queued else '[ZLAN] → DO OK 重複狀態已抑制')
            else:
                queued = zlan_ctrl.send_ng(suppress_same_state=suppress_same_state)
                log.info('[ZLAN] → DO NG 已排隊' if queued else '[ZLAN] → DO NG 重複狀態已抑制')


# ──────────────────────────────────────────────────────────────────
# 模式 A：OpenCV VideoCapture 推論迴圈
# ──────────────────────────────────────────────────────────────────
def infer_loop_opencv(
    args, cache_mgr: CacheManager,
    shared: SharedState, zlan_ctrl: Optional[ZlanController]
):
    method       = METHOD_MAP.get(args.method, cv2.TM_CCOEFF_NORMED)
    source       = args.source
    do_show      = args.show
    do_log       = args.enable_log
    save_fail    = args.save_fail
    fps_limit    = args.fps
    trigger_mode = getattr(args, 'zlan_trigger', 'free') if zlan_ctrl else 'free'

    try:
        source = int(source)
    except (ValueError, TypeError):
        pass

    log.info(f'[OpenCV] 開啟來源: {source}')
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        log.error(f'無法開啟影像來源: {source}')
        shared.stop()
        return

    rotate_code = get_video_rotate_code(cap)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    if fps_limit <= 0:
        fps_limit = src_fps
    frame_interval = max(1, round(src_fps / fps_limit))
    log.info(f'來源 fps={src_fps:.1f}  推論幀率={fps_limit:.1f}  每 {frame_interval} 幀推論一次')

    writer = None
    if args.output:
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        ok, first = cap.read()
        if ok:
            first = apply_rotate_code(first, rotate_code)
            h, w = first.shape[:2]
            writer = cv2.VideoWriter(args.output, fourcc, fps_limit, (w, h))
            log.info(f'輸出影片: {args.output}  ({w}x{h} @ {fps_limit:.1f}fps)')
            product, active_mgr, generation = shared.get_active_context()
            cache = active_mgr.get() if active_mgr else None
            if cache is not None and product is not None:
                pass_, results, vis = run_inference(first, cache, method, draw_vis=True)
                _handle_result(first, vis, pass_, results, 'PASS' if pass_ else 'FAIL',
                               product, do_log, save_fail, writer, shared, args.db,
                               zlan_ctrl, generation=generation,
                               rules=getattr(cache, 'last_rule_results', []),
                               inference_mode=getattr(cache, 'last_inference_mode', 'legacy_template'))
        cap.set(cv2.CAP_PROP_POS_FRAMES, 1)

    frame_idx   = 0
    t_last      = time.time()
    fps_display = 0.0
    live_state  = {'last_live_ts': 0.0, 'di_armed': True}
    log.info(f'[Live] preview 限制={getattr(args, "live_fps", 5.0):.1f}fps  JPEG品質={getattr(args, "live_jpeg_quality", 70)}')

    try:
        while shared.running:
            ok, frame = cap.read()
            if not ok:
                if isinstance(source, str) and not source.startswith('rtsp'):
                    log.info('影片播放完畢')
                    break
                else:
                    log.warning('讀幀失敗，1秒後重試…')
                    time.sleep(1)
                    cap.release()
                    cap = cv2.VideoCapture(source)
                    rotate_code = get_video_rotate_code(cap)
                    continue

            frame = apply_rotate_code(frame, rotate_code)
            frame_idx += 1

            cam_cfg, _ = shared.get_camera_config()
            frame = apply_digital_zoom(frame, cam_cfg)

            if frame_idx % frame_interval != 0:
                if do_show and frame_idx % 3 == 0:
                    cv2.imshow('TM-Inspect [Live]', frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
                continue

            # ── polling 模式：DI1 未觸發則跳過推論，但更新即時影像 ──
            _zlan  = shared.zlan_ctrl
            _tmode = shared.trigger_mode
            if _tmode == 'polling' and _zlan is not None:
                di_high = _zlan.read_di1_high_cached()
                if not di_high:
                    live_state['di_armed'] = True
                    _update_live_preview(frame, shared, live_state, args)
                    time.sleep(0.005)
                    continue
                if not live_state.get('di_armed', True):
                    # DI1 仍維持 High 時不重複推論/重複送 DO，等待 DI1 回 Low 後重新 armed。
                    _update_live_preview(frame, shared, live_state, args)
                    time.sleep(0.005)
                    continue
                live_state['di_armed'] = False
                log.info('[ZLAN] DI1 上升觸發，執行一次推論')
                time.sleep(args.zlan_debounce)

            t0 = time.time()
            draw = bool(args.http) or do_show or bool(args.output) or bool(args.save_fail)
            product, active_mgr, generation = shared.get_active_context()
            cache = active_mgr.get() if active_mgr else None
            if cache is None or product is None:
                log.warning('Cache/Product 尚未就緒，跳過此幀')
                continue
            all_pass, results, vis = run_inference(frame, cache, method, draw_vis=draw)
            elapsed_ms = (time.time() - t0) * 1000
            stamp = 'PASS' if all_pass else 'FAIL'

            _handle_result(frame, vis, all_pass, results, stamp,
                           product, do_log, save_fail, writer, shared, args.db,
                           shared.zlan_ctrl, generation=generation,
                           rules=getattr(cache, 'last_rule_results', []),
                           inference_mode=getattr(cache, 'last_inference_mode', 'legacy_template'))

            now = time.time()
            fps_display = 1.0 / max(now - t_last, 1e-6)
            t_last = now

            if frame_idx % 100 == 0:
                scores = [f"{r['label']}={r['score']:.3f}"
                          for r in results if r.get('score') is not None]
                log.info(f'F{frame_idx}  {stamp}  {elapsed_ms:.1f}ms  '
                         f'fps={fps_display:.1f}  {", ".join(scores)}')

            if do_show and vis is not None:
                cv2.putText(vis, f'{fps_display:.1f} fps  {elapsed_ms:.0f}ms',
                            (10, vis.shape[0]-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1)
                cv2.imshow('TM-Inspect [Live]', vis)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    log.info('使用者按 Q 停止')
                    break

    except KeyboardInterrupt:
        log.info('收到 Ctrl+C，停止推論')
    except Exception as e:
        import traceback
        log.critical(f'[CRASH] OpenCV 推論迴圈例外: {e}')
        log.critical(traceback.format_exc())
    finally:
        cap.release()
        if writer:
            writer.release()
        if do_show:
            cv2.destroyAllWindows()
        shared.stop()
        log.info(f'推論結束，共處理 {frame_idx} 幀')


# ──────────────────────────────────────────────────────────────────
# GStreamer / QIR camera helpers
# ──────────────────────────────────────────────────────────────────
def _gst_make(Gst, factory: str, name: str):
    """Create a GStreamer element with an actionable error message.

    QIR images still expose Qualcomm camera through qtiqmmfsrc, but the element
    is provided by the firmware / QIM GStreamer plugin package. If this fails,
    it is usually an image/environment issue rather than a Python inference bug.
    """
    elem = Gst.ElementFactory.make(factory, name)
    if elem is None:
        registry = Gst.Registry.get()
        qti_plugins = []
        try:
            for feat in registry.get_feature_list(Gst.ElementFactory):
                fname = feat.get_name()
                if fname and fname.startswith('qti'):
                    qti_plugins.append(fname)
        except Exception:
            pass
        qti_hint = ', '.join(sorted(qti_plugins)[:30]) if qti_plugins else '<none>'
        raise RuntimeError(
            f'GStreamer element not found: {factory}. '
            f'目前 registry 可見的 qti* elements: {qti_hint}. '
            '請先在同一個 shell / systemd 環境確認：gst-inspect-1.0 qtiqmmfsrc'
        )
    return elem


def _gst_set_if_property(elem, prop: str, value, label: str = '') -> bool:
    """Set a GObject property only when the current firmware exposes it."""
    try:
        if elem is None or elem.find_property(prop) is None:
            return False
        elem.set_property(prop, value)
        if label:
            log.info(f'{label} {prop}={value}')
        return True
    except Exception as e:
        log.warning(f'[GStreamer] 設定 {elem.get_name() if elem else "?"}.{prop}={value} 失敗: {e}')
        return False


def _request_qir_preview_pad(Gst, camsrc):
    """QIR/QIM camera source needs video_0 request pad set to PREVIEW.

    The old parse_launch one-liner cannot safely set this pad property before
    linking.  The vendor sample uses the same idea to avoid the camera service
    choosing an unsuitable default usecase on newer firmware.
    """
    pad = None

    def _set_preview(p):
        if p is None:
            return None
        try:
            # 0 = GST_SOURCE_STREAM_TYPE_PREVIEW in Qualcomm sample code.
            if p.find_property('type') is not None:
                p.set_property('type', 0)
                log.info(f'[Camera/QIR] {p.get_name()} type=PREVIEW')
        except Exception as e:
            log.warning(f'[Camera/QIR] 設定 {p.get_name()} type=PREVIEW 失敗: {e}')
        return p

    try:
        pad = camsrc.get_static_pad('video_0')
        if pad is None and hasattr(camsrc, 'request_pad_simple'):
            pad = camsrc.request_pad_simple('video_0')
        if pad is None and hasattr(camsrc, 'get_request_pad'):
            # Older GI bindings keep get_request_pad instead of request_pad_simple.
            pad = camsrc.get_request_pad('video_0')
        if pad is not None:
            return _set_preview(pad)
    except Exception as e:
        log.warning(f'[Camera/QIR] request video_0 pad 失敗: {e}')

    # Last resort: if the pad is emitted later, at least set it then.  This is
    # weaker than requesting before link, but better than silently using default.
    try:
        def _on_pad_added(_src, new_pad):
            if new_pad and new_pad.get_name() == 'video_0':
                _set_preview(new_pad)
        camsrc.connect('pad-added', _on_pad_added)
        log.info('[Camera/QIR] video_0 尚未建立，已掛 pad-added fallback')
    except Exception as e:
        log.warning(f'[Camera/QIR] pad-added fallback 失敗: {e}')
    return None


def _link_or_raise(src, dst, desc: str) -> None:
    if not src.link(dst):
        raise RuntimeError(f'GStreamer link failed: {desc}')


def _link_src_pad_or_raise(Gst, src_pad, dst, desc: str) -> None:
    if src_pad is None:
        raise RuntimeError(f'GStreamer source pad missing: {desc}')
    sink_pad = dst.get_static_pad('sink')
    if sink_pad is None:
        raise RuntimeError(f'GStreamer sink pad missing: {dst.get_name()}.sink')
    ret = src_pad.link(sink_pad)
    if ret not in (Gst.PadLinkReturn.OK, Gst.PadLinkReturn.WAS_LINKED):
        raise RuntimeError(f'GStreamer pad link failed: {desc}, ret={ret.value_nick}')


def _configure_leaky_queue(queue_elem, max_buffers: int = 4) -> None:
    """Configure a queue like the vendor QIR sample: bounded + downstream leaky."""
    _gst_set_if_property(queue_elem, 'max-size-buffers', int(max_buffers))
    _gst_set_if_property(queue_elem, 'max-size-bytes', 0)
    _gst_set_if_property(queue_elem, 'max-size-time', 0)
    # 2 = downstream. If the enum differs, failure is non-fatal.
    _gst_set_if_property(queue_elem, 'leaky', 2)


def _configure_appsink(appsink) -> None:
    appsink.set_property('emit-signals', True)
    appsink.set_property('max-buffers', 2)
    appsink.set_property('drop', True)
    appsink.set_property('sync', False)


def build_qir_camera_appsink_pipeline(Gst, args):
    """Build QIR/QIM camera pipeline programmatically.

    v3 follows the vendor sample more closely than the old direct-BGR path:
    qtiqmmfsrc is explicitly requested as video_0/PREVIEW, camera frames stay
    NV12 through the QIR path, and the default Python-facing branch is JPEG.

    Why JPEG by default:
      * New FW can expose NV12/GBM buffers that do not cooperate with generic
        videoconvert/appsink CPU mapping.
      * The vendor sample's live-view path uses queue -> jpegenc -> sink.
      * Python receives a small image/jpeg buffer and decodes it with OpenCV,
        so template matching can keep using normal BGR frames.

    --gst-capture-mode:
      jpeg  qtiqmmfsrc -> NV12 -> tee -> queue -> jpegenc -> appsink [default]
      nv12  qtiqmmfsrc -> NV12 -> appsink, Python cvtColor NV12->BGR
      bgr   legacy path qtiqmmfsrc -> NV12 -> videoconvert -> BGR appsink
    """
    camera = int(getattr(args, 'camera', 0))
    width = int(getattr(args, 'gst_width', 1920))
    height = int(getattr(args, 'gst_height', 1080))
    fps = int(getattr(args, 'gst_fps', 30))
    capture_mode = str(getattr(args, 'gst_capture_mode', 'jpeg') or 'jpeg').strip().lower()
    if capture_mode not in ('jpeg', 'nv12', 'bgr'):
        capture_mode = 'jpeg'

    pipeline = Gst.Pipeline.new('tm_inspect_qir_camera')
    camsrc = _gst_make(Gst, 'qtiqmmfsrc', 'camsrc')
    camcaps = _gst_make(Gst, 'capsfilter', 'camcaps')
    queue0 = _gst_make(Gst, 'queue', 'cam_queue')
    appsink = _gst_make(Gst, 'appsink', 'sink')

    _gst_set_if_property(camsrc, 'camera', camera, '[Camera/QIR]')
    _gst_set_if_property(camsrc, 'ldc', bool(getattr(args, 'ldc', True)), '[Camera/QIR]')

    video_pad = _request_qir_preview_pad(Gst, camsrc)

    camcaps.set_property(
        'caps',
        Gst.Caps.from_string(
            f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1'
        )
    )
    _configure_leaky_queue(queue0, max_buffers=4)
    _configure_appsink(appsink)

    # Base elements common to all capture modes.
    for elem in (camsrc, camcaps, queue0):
        pipeline.add(elem)

    if video_pad is not None:
        _link_src_pad_or_raise(Gst, video_pad, camcaps, 'camsrc.video_0 -> camcaps')
    else:
        _link_or_raise(camsrc, camcaps, 'camsrc -> camcaps')
    _link_or_raise(camcaps, queue0, 'camcaps -> queue')

    if capture_mode == 'jpeg':
        # Closest to smartcam_inference.py live-view branch:
        # output_tee -> queue_image -> jpegenc -> sink.  We keep a fakesink
        # branch so the tee behaves like the vendor sample's output/display path
        # while Python consumes JPEG buffers from appsink.
        split = _gst_make(Gst, 'tee', 'output_tee')
        queue_display = _gst_make(Gst, 'queue', 'queue_display')
        display = _gst_make(Gst, 'fakesink', 'display')
        queue_image = _gst_make(Gst, 'queue', 'queue_image')
        jpegenc = _gst_make(Gst, 'jpegenc', 'jpegenc')

        _gst_set_if_property(split, 'allow-not-linked', True)
        _configure_leaky_queue(queue_display, max_buffers=3)
        _configure_leaky_queue(queue_image, max_buffers=8)
        _gst_set_if_property(display, 'sync', False)
        q = max(30, min(95, int(getattr(args, 'live_jpeg_quality', 70) or 70)))
        _gst_set_if_property(jpegenc, 'quality', q)

        for elem in (split, queue_display, display, queue_image, jpegenc, appsink):
            pipeline.add(elem)

        _link_or_raise(queue0, split, 'queue -> output_tee')
        _link_or_raise(split, queue_display, 'output_tee -> queue_display')
        _link_or_raise(queue_display, display, 'queue_display -> fakesink')
        _link_or_raise(split, queue_image, 'output_tee -> queue_image')
        _link_or_raise(queue_image, jpegenc, 'queue_image -> jpegenc')
        _link_or_raise(jpegenc, appsink, 'jpegenc -> appsink')

        log.info(
            '[GStreamer/QIR] Pipeline: '
            f'qtiqmmfsrc camera={camera} video_0(type=PREVIEW) ! '
            f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1 ! '
            'queue ! tee name=output_tee '
            'output_tee. ! queue leaky=downstream ! fakesink sync=false '
            f'output_tee. ! queue leaky=downstream ! jpegenc quality={q} ! appsink'
        )

    elif capture_mode == 'nv12':
        pipeline.add(appsink)
        _link_or_raise(queue0, appsink, 'queue -> NV12 appsink')
        log.info(
            '[GStreamer/QIR] Pipeline: '
            f'qtiqmmfsrc camera={camera} video_0(type=PREVIEW) ! '
            f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1 ! '
            'queue leaky=downstream ! appsink ; Python NV12->BGR'
        )

    else:  # bgr legacy fallback
        convert = _gst_make(Gst, 'videoconvert', 'bgr_convert')
        bgrcaps = _gst_make(Gst, 'capsfilter', 'bgrcaps')
        bgrcaps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=BGR'))
        for elem in (convert, bgrcaps, appsink):
            pipeline.add(elem)
        _link_or_raise(queue0, convert, 'queue -> videoconvert')
        _link_or_raise(convert, bgrcaps, 'videoconvert -> BGR caps')
        _link_or_raise(bgrcaps, appsink, 'BGR caps -> appsink')
        log.info(
            '[GStreamer/QIR] Pipeline: '
            f'qtiqmmfsrc camera={camera} video_0(type=PREVIEW) ! '
            f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1 ! '
            'queue leaky=downstream ! videoconvert ! video/x-raw,format=BGR ! appsink'
        )

    return pipeline, camsrc, appsink, capture_mode


# ──────────────────────────────────────────────────────────────────
# 模式 B：GStreamer qtiqmmfsrc / QIR 推論迴圈（QCS 平台鏡頭）
# ──────────────────────────────────────────────────────────────────
def infer_loop_gst(
    args, cache_mgr: CacheManager,
    shared: SharedState, zlan_ctrl: Optional[ZlanController]
):
    try:
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
    except ImportError:
        log.error('找不到 PyGObject / GStreamer，請安裝 python3-gi')
        shared.stop()
        return

    Gst.init(None)

    method       = METHOD_MAP.get(args.method, cv2.TM_CCOEFF_NORMED)
    do_log       = args.enable_log
    save_fail    = args.save_fail
    fps_limit    = args.fps if args.fps > 0 else args.gst_fps
    camera       = args.camera
    width        = args.gst_width
    height       = args.gst_height
    src_fps      = args.gst_fps
    trigger_mode = getattr(args, 'zlan_trigger', 'free') if zlan_ctrl else 'free'

    try:
        pipeline, camsrc, appsink, capture_mode = build_qir_camera_appsink_pipeline(Gst, args)
    except Exception as e:
        import traceback
        log.critical(f'[GStreamer/QIR] 無法建立 camera pipeline: {e}')
        log.critical(traceback.format_exc())
        shared.stop()
        return

    cam_cfg, cam_rev = shared.get_camera_config()
    if not cam_cfg:
        cam_cfg = camera_config_from_args(args)
        cam_rev = shared.set_camera_config(cam_cfg)
    apply_gst_camera_config(camsrc, cam_cfg, reason='startup', mode=getattr(args, 'camera_controls', 'safe'))

    frame_queue: queue.Queue = queue.Queue(maxsize=max(1, int(getattr(args, 'gst_queue_size', 1))))
    watchdog_state = {'last_frame_ts': time.time(), 'dropped': 0}
    watchdog_sec = float(getattr(args, 'frame_watchdog_sec', 15.0) or 0.0)

    def on_new_sample(sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf  = sample.get_buffer()
        caps = sample.get_caps()
        s    = caps.get_structure(0) if caps and caps.get_size() > 0 else None
        caps_name = s.get_name() if s is not None else ''
        fmt = s.get_value('format') if s is not None and s.has_field('format') else ''
        w = int(s.get_value('width')) if s is not None and s.has_field('width') else int(getattr(args, 'gst_width', 0) or 0)
        h = int(s.get_value('height')) if s is not None and s.has_field('height') else int(getattr(args, 'gst_height', 0) or 0)
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            # 只要 appsink 還有吐 sample，就更新 watchdog；這能判斷 QMMF 是否停止出幀。
            watchdog_state['last_frame_ts'] = time.time()

            # 如果 Python 端還沒消化上一幀，直接丟掉本幀，避免 1080p frame copy 堆 CPU/記憶體壓力。
            if frame_queue.full():
                watchdog_state['dropped'] += 1
                return Gst.FlowReturn.OK

            arr = np.frombuffer(map_info.data, dtype=np.uint8)
            frm = None
            if caps_name == 'image/jpeg' or capture_mode == 'jpeg':
                # jpegenc -> appsink: CPU-friendly small buffer. cv2.imdecode returns BGR.
                frm = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            elif str(fmt).upper() == 'NV12' or capture_mode == 'nv12':
                # Raw NV12 fallback. Some firmware exposes padded buffers; use the
                # configured width/height and ignore any tail padding when possible.
                need = h * w * 3 // 2
                if w > 0 and h > 0 and arr.size >= need:
                    yuv = arr[:need].reshape((h * 3 // 2, w))
                    frm = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            else:
                # Legacy BGR path.
                need = h * w * 3
                if w > 0 and h > 0 and arr.size >= need:
                    frm = arr[:need].reshape((h, w, 3)).copy()

            if frm is not None and frm.size > 0:
                try:
                    frame_queue.put_nowait(frm)
                except queue.Full:
                    watchdog_state['dropped'] += 1
            else:
                watchdog_state['decode_failed'] = watchdog_state.get('decode_failed', 0) + 1
                if watchdog_state['decode_failed'] <= 3:
                    log.warning(f'[GStreamer] sample decode failed: mode={capture_mode} caps={caps.to_string() if caps else "?"} size={arr.size}')
        except Exception as e:
            log.debug(f'[GStreamer] 幀轉換失敗: {e}')
        finally:
            buf.unmap(map_info)
        return Gst.FlowReturn.OK

    appsink.connect('new-sample', on_new_sample)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        log.error('[GStreamer] Pipeline 啟動失敗')
        shared.stop()
        return
    try:
        state_ret, state_cur, state_pending = pipeline.get_state(10 * Gst.SECOND)
        log.info(f'[GStreamer] state={state_cur.value_nick} pending={state_pending.value_nick} ret={state_ret.value_nick}')
    except Exception as e:
        log.warning(f'[GStreamer] 等待 PLAYING 狀態失敗/逾時: {e}')

    watchdog_state['last_frame_ts'] = time.time()

    log.info(f'[GStreamer] 攝影機={camera}  {width}x{height}@{src_fps}fps  '
             f'推論限制={fps_limit:.1f}fps')

    min_interval = 1.0 / fps_limit if fps_limit > 0 else 0.0
    frame_idx    = 0
    t_last_infer = time.time()
    live_state   = {'last_live_ts': 0.0}
    camera_state = {'last_rev': cam_rev}
    log.info(f'[Live] preview 限制={getattr(args, "live_fps", 5.0):.1f}fps  JPEG品質={getattr(args, "live_jpeg_quality", 70)}')

    try:
        while shared.running:
            try:
                frame = frame_queue.get(timeout=1.0)
            except queue.Empty:
                bus = pipeline.get_bus()
                msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg:
                    if msg.type == Gst.MessageType.ERROR:
                        err, dbg = msg.parse_error()
                        log.critical(f'[GStreamer] Bus ERROR: {err.message} debug={dbg}')
                        raise SystemExit(3)
                    if msg.type == Gst.MessageType.EOS:
                        log.critical('[GStreamer] Bus EOS：camera pipeline 結束，交由 systemd 重啟')
                        raise SystemExit(4)

                if watchdog_sec > 0 and (time.time() - watchdog_state.get('last_frame_ts', 0)) > watchdog_sec:
                    log.critical(
                        f'[WATCHDOG] 超過 {watchdog_sec:.1f}s 未收到 camera frame；'
                        f'dropped={watchdog_state.get("dropped", 0)}。強制退出交由 systemd 重啟。'
                    )
                    raise SystemExit(2)
                continue

            # 套用前端/API 動態相機設定；僅手動調整，不做自動亮度控制
            new_cam_cfg, new_cam_rev = shared.get_camera_config()
            if new_cam_rev != camera_state.get('last_rev'):
                apply_gst_camera_config(camsrc, new_cam_cfg, reason=f'rev={new_cam_rev}', mode=getattr(args, 'camera_controls', 'safe'))
                camera_state['last_rev'] = new_cam_rev
            frame = apply_digital_zoom(frame, new_cam_cfg)

            now = time.time()
            if min_interval > 0 and (now - t_last_infer) < min_interval:
                continue

            # ── polling 模式：DI1 未觸發則跳過，但更新即時影像 ──────
            _zlan  = shared.zlan_ctrl
            _tmode = shared.trigger_mode
            if _tmode == 'polling':
                if _zlan is None:
                    # ZLAN 尚未連線時仍保持 polling 待命，只更新 live preview，不做推論。
                    live_state['di_armed'] = True
                    _update_live_preview(frame, shared, live_state, args)
                    continue
                di_high = _zlan.read_di1_high_cached()
                if not di_high:
                    live_state['di_armed'] = True
                    _update_live_preview(frame, shared, live_state, args)
                    continue
                if not live_state.get('di_armed', True):
                    # DI1 仍維持 High 時不重複推論/重複送 DO，等待 DI1 回 Low 後重新 armed。
                    _update_live_preview(frame, shared, live_state, args)
                    continue
                live_state['di_armed'] = False
                log.info('[ZLAN] DI1 上升觸發，執行一次推論')
                time.sleep(args.zlan_debounce)

            t_last_infer = time.time()
            frame_idx += 1

            t0 = time.time()
            product, active_mgr, generation = shared.get_active_context()
            cache = active_mgr.get() if active_mgr else None
            if cache is None or product is None:
                log.warning('Cache/Product 尚未就緒，跳過此幀')
                continue
            all_pass, results, vis = run_inference(frame, cache, method, draw_vis=True)
            elapsed_ms = (time.time() - t0) * 1000
            stamp = 'PASS' if all_pass else 'FAIL'

            _handle_result(frame, vis, all_pass, results, stamp,
                           product, do_log, save_fail, None, shared, args.db,
                           shared.zlan_ctrl, generation=generation,
                           rules=getattr(cache, 'last_rule_results', []),
                           inference_mode=getattr(cache, 'last_inference_mode', 'legacy_template'))

            if frame_idx % 100 == 0:
                scores = [f"{r['label']}={r['score']:.3f}"
                          for r in results if r.get('score') is not None]
                log.info(f'F{frame_idx}  {stamp}  {elapsed_ms:.1f}ms  {", ".join(scores)}')

    except KeyboardInterrupt:
        log.info('收到 Ctrl+C，停止推論')
    except Exception as e:
        import traceback
        log.critical(f'[CRASH] GStreamer 推論迴圈例外: {e}')
        log.critical(traceback.format_exc())
    finally:
        log.info('[GStreamer] 停止 pipeline…')
        try:
            pipeline.set_state(Gst.State.NULL)
            # 等待 QMMF / qtiqmmfsrc 釋放；若底層卡住，systemd 最終仍會殺掉整個 process。
            try:
                pipeline.get_state(2 * Gst.SECOND)
            except Exception as e:
                log.warning(f'[GStreamer] 等待 pipeline NULL 超時/失敗: {e}')
        except Exception as e:
            log.warning(f'[GStreamer] pipeline.set_state(NULL) 失敗: {e}')
        # 給 cam-server / QMMF 一點 settle 時間，避免快速重啟時 camera resource 未釋放。
        time.sleep(3)
        shared.stop()
        log.info(f'[GStreamer] 推論結束，共處理 {frame_idx} 幀')


# ──────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description='TM-Inspect 正式環境即時推論程式（含 ZLAN6042 DO 輸出）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--product',   default='', help='產品序號；空白時從 product_config.json 的 last_product_serial 載入，若仍沒有則自動選第一個有 template 的產品')
    p.add_argument('--source',    required=True,
                   help='RTSP URL / 影片路徑 / 攝影機 index / "gst"')
    p.add_argument('--db',        default=os.path.join(
                       os.path.dirname(__file__), 'db', 'inspection.db'))
    p.add_argument('--method',    default='TM_CCOEFF_NORMED',
                   choices=list(METHOD_MAP.keys()))
    p.add_argument('--fps',       type=float, default=0)
    p.add_argument('--show',      action='store_true')
    p.add_argument('--output',    default='')
    p.add_argument('--enable-log',   action='store_true',
                   help='寫入 inference_logs DB（預設不寫）')
    p.add_argument('--log-retain-days', type=int, default=LOG_RETAIN_DAYS,
                   help=f'Log 保留天數（0=不清理，預設 {LOG_RETAIN_DAYS}）')
    p.add_argument('--save-fail', default='')
    p.add_argument('--http',      action='store_true', help='啟動 API server；正式環境請搭配 --https')
    p.add_argument('--https',     action='store_true', help='API server 使用 HTTPS；需 --ssl-cert 與 --ssl-key')
    p.add_argument('--http-port', type=int, default=8765)
    p.add_argument('--ssl-cert',  default=os.environ.get('TM_INSPECT_SSL_CERT') or os.environ.get('SSL_CERT_FILE') or '',
                   help='HTTPS 憑證檔 PEM/CRT；也可用 TM_INSPECT_SSL_CERT 或 SSL_CERT_FILE')
    p.add_argument('--ssl-key',   default=os.environ.get('TM_INSPECT_SSL_KEY') or os.environ.get('SSL_KEY_FILE') or '',
                   help='HTTPS 私鑰檔 KEY；也可用 TM_INSPECT_SSL_KEY 或 SSL_KEY_FILE')
    p.add_argument('--live-fps', type=float, default=5.0,
                   help='待機 live preview 最大更新 FPS；0=不限制（預設 5）')
    p.add_argument('--live-jpeg-quality', type=int, default=70,
                   help='待機 live preview JPEG 品質 30~95（預設 70）')
    p.add_argument('--frame-watchdog-sec', type=float, default=15.0,
                   help='GStreamer 模式：超過 N 秒未收到 camera frame 時，以非 0 狀態退出，交由 systemd 重啟（0=停用，預設 15）')
    p.add_argument('--gst-queue-size', type=int, default=1,
                   help='GStreamer appsink 進 Python 的 frame queue 大小；越小越不堆積記憶體（預設 1）')

    g = p.add_argument_group('GStreamer 模式（--source gst）')
    g.add_argument('--camera',     type=int, default=0)
    g.add_argument('--gst-width',  type=int, default=1920)
    g.add_argument('--gst-height', type=int, default=1080)
    g.add_argument('--gst-fps',    type=int, default=30)
    g.add_argument('--ldc', type=lambda v: str(v).lower() in ('1', 'true', 'yes', 'on'), default=True,
                   help='QIR/QIM camera LDC 鏡頭畸變校正 true/false（預設 true）')
    g.add_argument('--gst-capture-mode', choices=['jpeg', 'nv12', 'bgr'], default='jpeg',
                   help='QIR frame capture path：jpeg=vendor-like queue->jpegenc->appsink（預設）；nv12=raw NV12 appsink；bgr=legacy videoconvert BGR appsink')
    g.add_argument('--camera-controls', choices=['off', 'safe', 'manual'], default='safe',
                   help='QIR 相機控制套用模式：off=不寫 qtiqmmfsrc 控制；safe=只套用最小安全設定；manual=舊版完整手動控制（預設 safe）')
    g.add_argument('--wb-mode',    type=int, default=1,
                   help='white-balance-mode 0~10（預設 1 auto；6 fluorescent）')
    g.add_argument('--antibanding', type=int, default=3,
                   help='抗閃爍 0=off 1=50Hz 2=60Hz 3=auto（預設 3）')
    g.add_argument('--iso-mode', type=int, default=0,
                   help='ISO 模式 0=auto, 8=manual（預設 0）')
    g.add_argument('--manual-iso-value', type=int, default=800,
                   help='手動 ISO 100~3200，僅 iso-mode=8 時有效')
    g.add_argument('--exposure-compensation', type=int, default=0,
                   help='曝光補償 -12~12（越大越亮，預設 0）')
    g.add_argument('--contrast', type=int, default=5,
                   help='對比 1~10（預設 5）')
    g.add_argument('--saturation', type=int, default=5,
                   help='飽和度 0~10（預設 5）')
    g.add_argument('--sharpness', type=int, default=2,
                   help='銳利度 0~6（預設 2）')
    z = p.add_argument_group('ZLAN6042 DO 輸出（--zlan-ip 設定後啟用）')
    z.add_argument('--zlan-ip',        default='',
                   help='ZLAN6042 IP（空白 = 停用）')
    z.add_argument('--zlan-port',      type=int, default=502)
    z.add_argument('--zlan-unit',      type=int, default=1)
    z.add_argument('--zlan-invert-di', type=lambda x: x.lower() != 'false',
                   default=True,
                   help='DI 低有效翻轉 true/false（模擬伺服器用 false）')
    z.add_argument('--zlan-trigger',   default='polling',
                   choices=['polling', 'free'],
                   help='polling=DI1觸發後推論, free=連續推論後輸出DO')
    z.add_argument('--zlan-do-ok',     type=int, default=1)
    z.add_argument('--zlan-do-ng',     type=int, default=2)
    z.add_argument('--zlan-pulse-ms',  type=float, default=500)
    z.add_argument('--zlan-free-repeat-sec', type=float, default=0.0,
                   help='free 模式同一 PASS/FAIL 狀態是否定期重送 DO；0=不重送，只在狀態變化時送')
    z.add_argument('--zlan-debounce',  type=float, default=2.0,
                   help='polling 模式：DI1觸發後去抖等待秒')

    p.add_argument('--reload-interval', type=float, default=5.0,
                   help='自動偵測 DB 變更的輪詢間隔秒（預設 5）')

    return p.parse_args()


def main():
    # ── 全域未處理例外：寫 log 後讓程式正常結束（才能看到 traceback）──
    import traceback as _tb
    def _uncaught_handler(exc_type, exc_value, exc_tb):
        log.critical('='*60)
        log.critical('[CRASH] 未處理的例外，程式即將終止')
        log.critical(''.join(_tb.format_exception(exc_type, exc_value, exc_tb)))
        log.critical('='*60)
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _uncaught_handler

    # ── Thread 內的例外也捕捉（Python 3.8+）──────────────────────
    def _thread_exc_handler(args_):
        log.critical('='*60)
        log.critical(f'[CRASH] Thread {args_.thread.name} 內例外')
        log.critical(''.join(_tb.format_exception(args_.exc_type, args_.exc_value, args_.exc_tb)))
        log.critical('='*60)
    threading.excepthook = _thread_exc_handler

    args = parse_args()

    # 套用 CLI 設定到 rotate globals
    global LOG_RETAIN_DAYS
    LOG_RETAIN_DAYS = args.log_retain_days
    if LOG_RETAIN_DAYS > 0:
        log.info(f'[Log Rotate] 啟動清理，保留 {LOG_RETAIN_DAYS} 天')
        rotate_logs_db(args.db)   # 啟動時先清一次

    if not os.path.exists(args.db):
        log.error(f'找不到 DB 檔案: {args.db}')
        sys.exit(1)

    product, product_source = resolve_startup_product(args.db, args.product)
    if not product:
        log.error('找不到可用產品：請先在 app.py 建立產品、標注 ROI 並儲存 template，或用 --product 指定有效序號')
        sys.exit(1)

    # 如果是自動/設定檔選出的產品，也寫回 product_config.json，確保下次 reboot 穩定沿用。
    save_product_config(product['serial'], product)
    log.info(f"產品: {product['serial']}  {product.get('name', '')}  來源={product_source}")

    # ── CacheManager：熱更新 template 快取 ──────────────────────
    cache_mgr = CacheManager(
        db_path=args.db,
        product_id=product['id'],
        reload_interval=args.reload_interval,
    )
    if not cache_mgr.initial_load():
        log.error('初始 template 載入失敗，請先用 app.py 標注並儲存 ROI')
        sys.exit(1)
    cache_mgr.start_watcher()   # 背景自動偵測 DB 變更

    if args.save_fail:
        os.makedirs(args.save_fail, exist_ok=True)

    ensure_log_schema(args.db)
    shared = SharedState(db_path=args.db)
    storage_cfg = load_storage_config(default_log_enabled=args.enable_log)
    shared.set_storage_config(storage_cfg)
    log.info(f"[Storage] log_enabled={storage_cfg.get('log_enabled')} image_enabled={storage_cfg.get('image_enabled')} type={storage_cfg.get('storage_type')} root={storage_cfg.get('effective_root')}")
    shared._product         = product    # 初始產品
    shared._reload_interval = args.reload_interval
    shared.cache_mgr        = cache_mgr   # 注入供 POST /reload 使用

    # Camera 設定：優先讀取前端儲存的 camera_config.json；沒有檔案才採用命令列預設。
    saved_cam_cfg = load_camera_config()
    if saved_cam_cfg is not None:
        cam_cfg = normalize_camera_config(saved_cam_cfg, base=camera_config_from_args(args))
        log.info(f'[Camera] 從設定檔載入: {CAMERA_CONFIG_PATH}')
    else:
        cam_cfg = camera_config_from_args(args)
    shared.set_camera_config(cam_cfg)

    def _sig_handler(sig, frame):
        name = 'SIGTERM（systemd 停止）' if sig == signal.SIGTERM else 'SIGINT（Ctrl+C）'
        log.info(f'收到 {name}，開始優雅關閉…')
        shared.stop()
    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    if args.http:
        try:
            start_http_server(shared, args.http_port, args.ssl_cert, args.ssl_key, args.https)
        except Exception as e:
            log.error(f'API server 啟動失敗: {e}')
            sys.exit(1)

    # ── ZLAN6042 初始化（優先讀設定檔，其次用命令列參數）──────────
    zlan_cfg = load_zlan_config()
    if zlan_cfg is not None:
        log.info(f'[ZLAN] 從設定檔載入: {ZLAN_CONFIG_PATH}')
        apply_zlan_config(zlan_cfg, shared)
    elif args.zlan_ip:
        log.info(f'[ZLAN] 連線 {args.zlan_ip}:{args.zlan_port}  '
                 f'unit={args.zlan_unit}  invert_di={args.zlan_invert_di}')
        try:
            zlan = Zlan6042Tcp(
                host=args.zlan_ip,
                port=args.zlan_port,
                unit_id=args.zlan_unit,
                invert_low_active=args.zlan_invert_di,
            )
            if not zlan.connect():
                log.error(f'[ZLAN] 無法連線，停用 DO 輸出')
            else:
                zlan_ctrl = ZlanController(
                    zlan=zlan,
                    ch_ok=args.zlan_do_ok,
                    ch_ng=args.zlan_do_ng,
                    pulse_ms=args.zlan_pulse_ms,
                    free_repeat_sec=args.zlan_free_repeat_sec,
                )
                shared.set_zlan(zlan_ctrl, args.zlan_trigger)
                log.info(f'[ZLAN] DO 輸出已啟用  '
                         f'OK=DO{args.zlan_do_ok}  NG=DO{args.zlan_do_ng}  '
                         f'脈衝={args.zlan_pulse_ms:.0f}ms  觸發={args.zlan_trigger}  '
                         f'free_repeat={args.zlan_free_repeat_sec:.1f}s')
        except Exception as e:
            log.error(f'[ZLAN] 初始化失敗: {e}，停用 DO 輸出')
    else:
        shared.set_zlan(None, 'free')

    # ── 每日定時 rotate（背景執行緒，每 24 小時清一次）──────────────
    if LOG_RETAIN_DAYS > 0:
        def _daily_rotate():
            while shared.running:
                time.sleep(24 * 3600)
                deleted = rotate_logs_db(args.db)
                log.info(f'[Log Rotate] 每日清理完成，刪除 {deleted} 筆')
        threading.Thread(target=_daily_rotate, daemon=True).start()
        log.info(f'[Log Rotate] 每日定時清理已啟動（保留 {LOG_RETAIN_DAYS} 天）')

    # ── 定期健康監控（每分鐘印一次，便於分析 crash 前的趨勢）────
    def _health_monitor():
        import gc
        while shared.running:
            time.sleep(60)
            try:
                # 記憶體
                with open('/proc/self/status') as f:
                    mem_lines = {l.split(':')[0]: l.split(':')[1].strip()
                                 for l in f if ':' in l}
                vmrss = mem_lines.get('VmRSS', '?')
                vmvirt = mem_lines.get('VmSize', '?')
                # 執行緒數
                n_threads = threading.active_count()
                # GC 物件數
                n_obj = len(gc.get_objects())
                log.info(
                    f'[Health] RSS={vmrss}  Virt={vmvirt}  '
                    f'threads={n_threads}  gc_objects={n_obj}'
                )
            except Exception as e:
                log.warning(f'[Health] 監控失敗: {e}')
    threading.Thread(target=_health_monitor, daemon=True, name='health-monitor').start()
    log.info('[Health] 定期監控已啟動（每 60 秒）')

    # ── 選擇推論迴圈 ──────────────────────────────────────────────
    use_gst = str(args.source).strip().lower() == 'gst'
    try:
        if use_gst:
            log.info('=== 模式: GStreamer qtiqmmfsrc（QCS 平台鏡頭）===')
            infer_loop_gst(args, cache_mgr, shared, None)
        else:
            log.info('=== 模式: OpenCV VideoCapture ===')
            infer_loop_opencv(args, cache_mgr, shared, None)
    finally:
        ctrl = shared.zlan_ctrl
        if ctrl is not None:
            ctrl.stop()
            ctrl.zlan.close()
            log.info('[ZLAN] 連線已關閉')


if __name__ == '__main__':
    import traceback as _tb2
    try:
        main()
    except SystemExit:
        raise
    except Exception as _e:
        # main() 層的例外（logging 可能還沒初始化，同時印到 stderr）
        _tb2.print_exc()
        sys.exit(1)
