# VisionEdge 2.4.9-jetson.1 整合驗證

## 已通過

- `python -m py_compile *.py`
- `node --check static/live_detail.js`
- `edge_smoke_test.py`
- `flow_designer_test.py`
- `i18n_static_test.py`
- `label_flow_regression_test.py`
- `live_flow_graph_test.py`
- `packaging_cycle_test.py`（13 cases）
- `smoke_test.py`
- `v4l2_controls_test.py`
- `video_label_e2e_test.py`
- `visionedge_regression_test.py`
- `notice_ui_test.js`
- `preview_lifecycle_test.js`
- `workspace_lifecycle_test.js`

## 此環境無法執行的 API tests

下列測試在 import `server.py` / `visionedge_server.py` 時即因測試容器沒有 Flask 而停止；沒有進入測試 assertion。容器無網路，因此也無法臨時 pip install。Jetson 正式環境請用專案 `.venv` / `uv` 依賴再跑一次。

- `edge_consistency_test.py`
- `flow_clear_test.py`
- `inspection_history_test.py`
- `packaging_integration_test.py`
- `preview_backpressure_test.py`
- `product_switch_regression_test.py`
- `resolution_matching_test.py`
- `rule_group_roundtrip_test.py`
- `studio_api_test.py`
- `workspace_test.py`

## Jetson 保護檢查

下列核心檔案與輸入 Jetson 版 SHA-256 完全一致：`edge_runtime.py`、`visionedge_server.py`、`server.py`、`studio_api.py`、`workspace_api.py`、`inspection_history.py`、`template_matching.py`、`v4l2_controls.py`、`static/latest_preview.js`、`static/camera_settings.html`、`static/camera_settings.js`、`static/workspace.js`、`static/template_studio.js`。

`static/template_workspace.html` 只修改 `workspace.css` cache-bust query；Jetson 的 LatestPreview / camera dialog 邏輯未更動。
