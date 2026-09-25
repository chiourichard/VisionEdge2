#!/usr/bin/env python3
"""VisionEdge HTTPS entrypoint and Edge APIs.

The application reuses LIVE_FLOW_GRAPH_REVIEW's Flask app/data model and adds a
single-camera Edge runtime. HTTPS configuration intentionally mirrors the
SmartCam webserver mechanism: cert/key are loaded from the project config and
can be overridden for development from the environment/CLI.
"""
from __future__ import annotations

import argparse
import atexit
import configparser
import os
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Response, jsonify, request, send_from_directory, stream_with_context

import server as base
from edge_runtime import EdgeRuntime

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config' / 'edge.ini'


@dataclass
class ServerConfig:
    host: str = '0.0.0.0'
    port: int = 8080
    tls: bool = True
    certfile: str = 'config/certs/CRS0000000616.cert'
    keyfile: str = 'config/certs/CRS0000000616.key'
    threads: int = 12

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> 'ServerConfig':
        cfg = cls()
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8-sig')
        if cp.has_section('server'):
            sec = cp['server']
            cfg.host = sec.get('host', cfg.host).strip() or cfg.host
            try:
                cfg.port = sec.getint('port', fallback=cfg.port)
            except Exception:
                pass
            try:
                cfg.tls = sec.getboolean('tls', fallback=cfg.tls)
            except Exception:
                pass
            cfg.certfile = sec.get('certfile', cfg.certfile).strip() or cfg.certfile
            cfg.keyfile = sec.get('keyfile', cfg.keyfile).strip() or cfg.keyfile
            try:
                cfg.threads = max(4, sec.getint('threads', fallback=cfg.threads))
            except Exception:
                pass
        return cfg

    def resolve_cert(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (BASE_DIR / p)


EDGE = EdgeRuntime(base)
app = base.app
app.extensions['visionedge_runtime'] = EDGE
from inspection_history import register_inspections
INSPECTIONS = register_inspections(app, EDGE, base, BASE_DIR / 'runtime_data' / 'inspection_history.sqlite3')
from packaging_api import register as register_packaging
PACKAGING_HISTORY = register_packaging(app, EDGE, BASE_DIR / 'runtime_data' / 'edge' / 'packaging_history.sqlite3')
from studio_api import register as register_studio
register_studio(app, base, EDGE)
from workspace_api import register as register_workspace
register_workspace(app, base, EDGE)


@app.after_request
def visionedge_no_cache(response):
    # Dynamic state/media APIs must never be served from the browser cache.
    # This is especially important after deleting a capture.
    if (request.path.startswith(('/api/edge', '/api/products')) or request.path == '/api/templates'
            or '/label-library' in request.path
            or request.path.endswith('/workspace')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.route('/edge')
@app.route('/visionedge')
def edge_page():
    return send_from_directory('static', 'edge_dashboard.html')


# VisionEdge opens directly into the operator screen; no login or legacy landing page.
app.view_functions['index'] = edge_page


def control_response(result):
    return jsonify(result), (200 if result.get('success', True) else
                             409 if result.get('code') in ('recording_active', 'restart_required') else 400)


@app.route('/api/edge/apply', methods=['POST'])
def edge_apply():
    data = request.get_json(silent=True) or {}
    try:
        pid = int(data.get('product_id', EDGE.cfg.product_id))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '產品編號無效'}), 400
    if pid and not base._load_product_dict(pid):
        return jsonify({'success': False, 'error': '產品不存在，請重新選擇'}), 404
    return control_response(EDGE.apply_definition(pid))


@app.route('/api/edge/template-frame', methods=['GET'])
def edge_template_frame():
    with EDGE.lock:
        if not EDGE.status()['frame_fresh'] or EDGE.latest_raw is None:
            return jsonify({'error': '相機尚未就緒，請先回即時檢測啟動相機'}), 409
        frame = EDGE.latest_raw.copy()
    product = base._load_product_dict(request.args.get('product_id', type=int))
    if not product:
        return jsonify({'error': '請先建立或選擇產品'}), 400
    return jsonify({'image_b64': base.cv2_to_b64(base._prepare_edge_frame(frame, product))})


@app.route('/api/edge/config', methods=['GET'])
def edge_get_config():
    return jsonify({'ok': True, 'config': EDGE.cfg.public()})


@app.route('/api/edge/config', methods=['PUT', 'POST'])
def edge_set_config():
    data = request.get_json(silent=True) or {}
    restart = bool(data.pop('restart', False))
    return control_response(EDGE.update_config(data, restart=restart))


@app.route('/api/edge/start', methods=['POST'])
def edge_start():
    return jsonify(EDGE.start())


@app.route('/api/edge/stop', methods=['POST'])
def edge_stop():
    return control_response(EDGE.stop())


@app.route('/api/edge/restart', methods=['POST'])
def edge_restart():
    return control_response(EDGE.restart())


@app.route('/api/edge/status', methods=['GET'])
def edge_status():
    return jsonify(EDGE.status())


@app.route('/api/edge/frame.jpg', methods=['GET'])
def edge_frame():
    mode = request.args.get('mode', 'result')
    data = EDGE.jpeg(mode)
    if not data:
        return Response(status=204)
    return Response(data, mimetype='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.route('/api/edge/preview.jpg', methods=['GET'])
def edge_preview():
    token, data = EDGE.preview_frame(request.args.get('mode', 'raw'), request.args.get('after'))
    headers = {'Cache-Control': 'no-store', 'X-Frame-Sequence': token}
    return Response(data if data else None, status=200 if data else 204,
                    mimetype='image/jpeg', headers=headers)


@app.route('/api/edge/live.mjpg', methods=['GET'])
def edge_mjpeg():
    mode = request.args.get('mode', 'result')

    def generate():
        seq = None
        next_frame = 0.0
        while not EDGE.stop_event.is_set():
            if EDGE.stop_event.wait(max(0, next_frame - time.monotonic())):
                break
            seq, data = EDGE.wait_jpeg(seq, mode=mode, timeout=2.0)
            if not data:
                continue
            next_frame = time.monotonic() + .1
            yield (
                b'--frame\r\nContent-Type: image/jpeg\r\n'
                b'Cache-Control: no-store\r\n\r\n' + data + b'\r\n'
            )

    return Response(
        stream_with_context(generate()),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/edge/snapshot', methods=['POST'])
def edge_snapshot():
    data = request.get_json(silent=True) or {}
    return jsonify(EDGE.snapshot(data.get('mode', 'result')))


@app.route('/api/edge/record/start', methods=['POST'])
def edge_record_start():
    data = request.get_json(silent=True) or {}
    return control_response(EDGE.start_recording(data.get('mode')))


@app.route('/api/edge/record/stop', methods=['POST'])
def edge_record_stop():
    return jsonify(EDGE.stop_recording())


@app.route('/api/edge/media', methods=['GET'])
def edge_media():
    return jsonify(EDGE.list_media())


@app.route('/api/edge/media/delete', methods=['POST'])
def edge_media_delete():
    data = request.get_json(silent=True) or {}
    result = EDGE.delete_media(data.get('path', ''))
    if result.get('success'):
        return jsonify(result)
    code = result.get('code')
    status = 404 if code == 'not_found' else 409 if code == 'recording_active' else 400
    return jsonify(result), status


@app.route('/api/edge/media/<path:rel>', methods=['GET', 'DELETE'])
def edge_media_file(rel):
    if request.method == 'DELETE':
        # Backward-compatible route. New UI uses POST /media/delete to avoid
        # proxy/path-encoding ambiguity and mirrors smartcam_webserver behavior.
        result = EDGE.delete_media(rel)
        if result.get('success'):
            return jsonify(result)
        code = result.get('code')
        status = 404 if code == 'not_found' else 409 if code == 'recording_active' else 400
        return jsonify(result), status
    try:
        path = EDGE.resolve_media(rel)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if not path.exists() or not path.is_file():
        return jsonify({'error': 'not found'}), 404
    status = EDGE.status()
    if status['recording'] and status.get('record_path') and Path(status['record_path']).resolve() == path.resolve():
        return jsonify({'error': '錄影尚未完成，請先停止錄影'}), 409
    return send_from_directory(
        str(path.parent), path.name,
        as_attachment=request.args.get('download') == '1',
        max_age=0,
    )


@app.route('/api/edge/sop/reset', methods=['POST'])
def edge_sop_reset():
    if not EDGE.status()['inference_ready'] or not EDGE.engine:
        return jsonify({'success': False, 'error': '流程尚未就緒'}), 409
    return jsonify({'success': True, 'sop': EDGE.reset_sop()})


@app.route('/api/edge/sop/finish', methods=['POST'])
def edge_sop_finish():
    if not EDGE.status()['inference_ready'] or not EDGE.engine:
        return jsonify({'success': False, 'error': '流程尚未就緒'}), 409
    return jsonify({'success': True, 'sop': EDGE.finish_sop()})


@app.route('/api/edge/sop/ack', methods=['POST'])
def edge_sop_ack():
    if not EDGE.status()['inference_ready'] or not EDGE.engine:
        return jsonify({'success': False, 'error': '流程尚未就緒'}), 409
    return jsonify({'success': True, 'sop': EDGE.acknowledge_sop()})


def _shutdown():
    try:
        EDGE.stop(force=True)
    except Exception:
        pass


atexit.register(_shutdown)


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name, '').strip().lower()
    if raw in ('1', 'on', 'true', 'yes'):
        return True
    if raw in ('0', 'off', 'false', 'no'):
        return False
    return None


def _build_tls_context(enabled: bool, cfg: ServerConfig):
    if not enabled:
        return None
    cert = cfg.resolve_cert(cfg.certfile)
    key = cfg.resolve_cert(cfg.keyfile)
    missing = [str(p) for p in (cert, key) if not p.exists()]
    if missing:
        raise RuntimeError('TLS is enabled but certificate/key is missing: ' + ', '.join(missing))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def _assert_port_available(host: str, port: int) -> None:
    # Do this before touching qtiqmmfsrc. It prevents the half-open Qualcomm
    # camera teardown we saw when Flask discovered a busy port after auto-start.
    test_host = host if host not in ('', '*') else '0.0.0.0'
    family = socket.AF_INET6 if ':' in test_host and test_host != '0.0.0.0' else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((test_host, int(port)))
    except OSError as exc:
        raise RuntimeError(f'Port {port} is not available on {host}: {exc}') from exc
    finally:
        sock.close()


def main():
    cfg = ServerConfig.load()
    parser = argparse.ArgumentParser(description='VisionEdge AI Camera server')
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--threads', type=int, default=None)
    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument('--tls', dest='tls', action='store_true', help='force HTTPS on')
    tls_group.add_argument('--no-tls', dest='tls', action='store_false', help='force plain HTTP (development only)')
    parser.set_defaults(tls=None)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    # Config -> legacy env -> VisionEdge env -> CLI. New names win.
    host = os.environ.get('TM_EDGE_HOST', cfg.host)
    host = os.environ.get('VISIONEDGE_HOST', host)
    port = int(os.environ.get('TM_EDGE_PORT', cfg.port))
    port = int(os.environ.get('VISIONEDGE_PORT', port))
    threads = int(os.environ.get('TM_EDGE_THREADS', cfg.threads))
    threads = int(os.environ.get('VISIONEDGE_THREADS', threads))
    tls_enabled = cfg.tls
    env_tls = _env_bool('VISIONEDGE_TLS')
    if env_tls is not None:
        tls_enabled = env_tls
    if args.host is not None:
        host = args.host
    if args.port is not None:
        port = args.port
    if args.threads is not None:
        threads = args.threads
    if args.tls is not None:
        tls_enabled = args.tls

    try:
        tls_context = _build_tls_context(tls_enabled, cfg)
        _assert_port_available(host, port)
    except Exception as exc:
        print(f'[VisionEdge] startup preflight failed: {exc}')
        raise SystemExit(2)

    # Camera auto-start happens only after TLS + port preflight succeeds.
    if EDGE.cfg.auto_start:
        result = EDGE.start()
        if not result.get('success'):
            print(f"[VisionEdge] camera auto-start failed: {result.get('error', 'unknown error')}")

    scheme = 'https' if tls_context is not None else 'http'
    print(f'VisionEdge: {scheme}://{host}:{port}/edge')
    print(f'Backend requested={EDGE.cfg.backend}; product_id={EDGE.cfg.product_id}')
    if tls_context is not None:
        print(f'[VisionEdge] TLS enabled (cert={Path(cfg.certfile).name}, TLS >= 1.2)')
    else:
        print('[VisionEdge] WARNING: TLS disabled; use only for development')

    # Waitress does not terminate TLS itself. For HTTPS we intentionally use
    # Werkzeug's threaded server with the SSLContext; this is the same direct
    # device-side TLS deployment model as smartcam_webserver. Plain HTTP may use
    # Waitress when it is installed.
    if tls_context is not None or args.debug:
        app.run(
            host=host, port=port, debug=args.debug, use_reloader=False,
            threaded=True, ssl_context=tls_context,
        )
        return
    try:
        from waitress import serve
    except Exception:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        return
    serve(app, host=host, port=port, threads=max(4, threads), channel_timeout=120,
          outbuf_high_watermark=256 * 1024, outbuf_overflow=256 * 1024)


if __name__ == '__main__':
    main()
