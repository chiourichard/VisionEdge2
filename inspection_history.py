"""Local snapshot inspections. Evidence and metadata commit in one SQLite transaction."""
import copy
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from flask import jsonify, request, Response


class InspectionHistory:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, sn TEXT, product_id INTEGER,
                    revision TEXT, created REAL, closed REAL);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, cycle_id TEXT,
                    created REAL, verdict TEXT, metadata TEXT, raw BLOB, result BLOB);
                CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_cycle ON cycles((1)) WHERE closed IS NULL;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self):
        with self.connect() as db:
            row = db.execute('SELECT * FROM cycles WHERE closed IS NULL').fetchone()
            pref = db.execute("SELECT value FROM preferences WHERE key='require_sn'").fetchone()
            active = dict(row) if row else None
            last = db.execute('SELECT id,verdict,created FROM attempts WHERE cycle_id=? ORDER BY created DESC LIMIT 1',
                              (active['id'],)).fetchone() if active else None
            return {'active': active, 'last': dict(last) if last else None, 'require_sn': bool(pref and pref[0])}


def register_inspections(app, edge, base, path):
    store = InspectionHistory(path)
    edge.inspection_active = lambda: bool(store.state()['active'])

    @app.before_request
    def guard_active_cycle():
        if request.method in ('POST', 'PUT') and request.path in (
            '/api/edge/apply', '/api/edge/config') and store.state()['active']:
            return jsonify(error='請先結束目前這件產品，再變更檢測設定'), 409

    @app.route('/api/edge/inspection', methods=['GET', 'POST'])
    def inspection():
        if request.method == 'GET':
            return jsonify(store.state())
        data = request.get_json(silent=True) or {}
        # Lock camera configuration throughout capture + inference + evidence commit.
        with store.lock, edge.control_lock:
            state = store.state()
            action = data.get('action', 'capture')
            if getattr(edge, 'packaging', None) and action != 'close':
                return jsonify(error='目前使用影像包裝循環，請依右側包裝指示操作'), 409
            if action == 'preferences':
                if state['active']:
                    return jsonify(error='請先結束目前這件產品'), 409
                if not isinstance(data.get('require_sn'), bool):
                    return jsonify(error='掃碼設定無效'), 400
                with store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO preferences VALUES ('require_sn',?)", (int(data['require_sn']),))
                return jsonify(store.state())
            if action == 'close':
                if state['active'] and data.get('cycle_id') != state['active']['id']:
                    return jsonify(error='目前產品已變更，請重新整理'), 409
                with store.connect() as db:
                    db.execute('UPDATE cycles SET closed=? WHERE id=?', (time.time(), data.get('cycle_id')))
                return jsonify(store.state())
            if action != 'capture':
                return jsonify(error='操作無效'), 400
            token = str(data.get('request_id', ''))
            try:
                uuid.UUID(token)
            except ValueError:
                return jsonify(error='缺少有效的拍照識別碼'), 400
            with store.connect() as db:
                previous = db.execute('SELECT id,cycle_id,verdict FROM attempts WHERE id=?', (token,)).fetchone()
                if previous:
                    return jsonify(attempt=dict(previous), **store.state())
            sn = str(data.get('sn', '')).strip()
            if len(sn) > 128 or any(ord(c) < 32 for c in sn):
                return jsonify(error='序號最多 128 字，不可包含控制字元'), 400
            if state['require_sn'] and not sn:
                return jsonify(error='請先掃描或輸入產品序號'), 400
            active = state['active']
            if active and sn != active['sn']:
                return jsonify(error='序號已鎖定，請先結束目前這件產品'), 409
            with edge.lock:
                if not edge.status()['inference_ready'] or edge.latest_raw is None:
                    return jsonify(error='相機或檢測設定尚未就緒，請啟動相機並套用產品'), 409
                frame = edge.latest_raw.copy()
                pid, revision = edge.cfg.product_id, edge.active_revision
                product = copy.deepcopy(edge.product)
            if active and (active['product_id'] != pid or active['revision'] != str(revision)):
                return jsonify(error='產品設定已變更，請結束此件後重新檢測'), 409
            if not edge._storage_ok():
                return jsonify(error='儲存空間不足，未建立檢測紀錄'), 409
            # Dedicated cache: preview must not overwrite this snapshot's rule results.
            mgr = base.vc.CacheManager(base.DB_PATH, int(pid))
            if not mgr.initial_load() or mgr._version != revision:
                return jsonify(error='設定已變更，請重新套用後檢測'), 409
            prepare = getattr(base, '_prepare_edge_frame', base._prepare_frame)
            prepared = prepare(frame, product or {})
            mgr.get().snapshot_three_state = True
            cache = mgr.get()
            kwargs = {'method': edge._method(), 'draw_vis': True}
            try:
                if hasattr(base.vc, 'TemplateMatcher'):
                    matcher = base.vc.TemplateMatcher(getattr(edge.cfg, 'inference_device', 'cpu'), edge._method())
                    matcher.prepare([reg['tpl_gray'] for reg in cache.regions] +
                                    [sample['tpl_gray'] for item in cache.inspection_items
                                     for sample in item.get('samples', [])])
                    kwargs['match_frame'] = matcher.begin(prepared)
                passed, results, vis = base.vc.run_inference(prepared, cache, **kwargs)
            except Exception as exc:
                return jsonify(error=f'檢測運算失敗：{exc}'), 503
            rules = getattr(mgr.get(), 'last_rule_results', []) or []
            if mgr._db_version() != revision:
                return jsonify(error='拍照期間設定被修改，本次未保存，請重新套用'), 409
            verdict = 'NG' if any(r.get('hard_reject') for r in rules) else 'OK' if passed else 'UNKNOWN'
            # No evidence of an explicit reject is never represented as confirmed NG.
            now = time.time()
            cycle_id = active['id'] if active else str(uuid.uuid4())
            metadata = {'product_id': pid, 'product_name': (product or {}).get('name', ''),
                        'revision': str(revision), 'sn': sn, 'verdict': verdict,
                        'scope': 'snapshot_only', 'results': results, 'rules': rules, 'created': now}
            blobs = []
            for img in (frame, vis if vis is not None else prepared):
                ok, encoded = base.vc.cv2.imencode('.jpg', img)
                if not ok:
                    return jsonify(error='影像編碼失敗，未保存紀錄'), 500
                blobs.append(encoded.tobytes())
            try:
                with store.connect() as db:
                    if not active:
                        db.execute('INSERT INTO cycles VALUES (?,?,?,?,?,NULL)', (cycle_id, sn, pid, str(revision), now))
                    db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?)',
                               (token, cycle_id, now, verdict, json.dumps(metadata, ensure_ascii=False, default=str), *blobs))
            except (sqlite3.Error, OSError):
                return jsonify(error='紀錄儲存失敗，請檢查磁碟後重試'), 500
            return jsonify(attempt={'id': token, 'cycle_id': cycle_id, 'verdict': verdict}, **store.state())

    @app.route('/api/edge/history')
    def history():
        query = request.args.get('q', '').strip()
        offset = max(0, request.args.get('offset', 0, type=int))
        with store.connect() as db:
            rows = db.execute('''SELECT a.id,a.cycle_id,a.created,a.verdict,c.sn,c.product_id,c.closed
                FROM attempts a JOIN cycles c ON c.id=a.cycle_id
                WHERE instr(c.sn,?)>0 OR instr(a.id,?)>0
                ORDER BY a.created DESC LIMIT 31 OFFSET ?''', (query, query, offset)).fetchall()
        return jsonify(items=[dict(r) for r in rows[:30]], more=len(rows)>30)

    @app.route('/api/edge/history/<attempt_id>/<kind>')
    def evidence(attempt_id, kind):
        if kind not in ('raw', 'result', 'metadata'):
            return jsonify(error='找不到檔案'), 404
        with store.connect() as db:
            row = db.execute(f'SELECT {kind} FROM attempts WHERE id=?', (attempt_id,)).fetchone()
        if not row:
            return jsonify(error='找不到紀錄'), 404
        return Response(row[0], mimetype='application/json' if kind == 'metadata' else 'image/jpeg')

    return store
