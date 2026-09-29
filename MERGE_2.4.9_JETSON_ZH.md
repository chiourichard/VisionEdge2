# VisionEdge 2.4.9 Jetson 整合版

## 合併原則

本版本以 Jetson 整合版為底，只移植新版 VisionEdge 中與硬體無關的 UI / 流程功能。Jetson 既有的相機、V4L2、Logitech BRIO、GStreamer/OpenCV、CUDA template matching、解析度縮放、錄影與 LatestPreview/backpressure 機制保留。

## 已整合

- 新版即時檢測 Label score UI。
- 放大/全螢幕檢測時的可拖曳「檢測操作」浮動面板與精簡流程圖。
- 檢測結果預設顯示；尚無有效 inference 時仍自動 fallback 到 raw preview。
- 結果框加粗：實際 match 4 px、expected/reference 2 px。
- 規則／流程頁新增「相機設定」入口。
- 樣板選擇視窗不再強制鎖定目前產品，可保留跨產品樣板挑選流程。
- 新版 UI 文字翻譯項目併入 Jetson 既有 i18n，Jetson/V4L2 專用翻譯保留。
- 圖片驗證 dialog 版面補上新版 workspace 樣式。

## 明確保留、未覆蓋

- `edge_runtime.py`
- `visionedge_server.py`
- `server.py`
- `studio_api.py`
- `workspace_api.py`
- `inspection_history.py`
- `template_matching.py`
- `v4l2_controls.py`
- `static/latest_preview.js`
- `static/camera_settings.html` / `camera_settings.js`
- `static/workspace.js` / `template_workspace.html` / `template_studio.js`
- Jetson service、BRIO config、CUDA OpenCV build script

## 沒有併入的新版裝置專用內容

- Qualcomm/QTI `qti_controls.py`、`qti_health.py`、`qti_process.py` 與其相機控制路徑。
- 任何會把 Jetson `/api/edge/preview.jpg` LatestPreview 改回 MJPEG queue 的前端變更。
- 任何會把 Jetson CUDA / resolution-aware matching 改回一般 CPU `cv2.matchTemplate` 的變更。

## 版本

`2.4.9-jetson.1`
