# OELinux / GStreamer 錄影修正版（2026-09-29）

此節取代下方「初版 FFmpeg 開發紀錄」的設備依賴說明。

- OELinux 偵測到 gst-inspect-1.0 時，使用同一 Python 環境啟動獨立的 gst_record_worker.py。
- 管線：appsrc(BGR) → videoconvert → NV12 → v4l2h264enc → h264parse → splitmuxsink/mp4mux。
- OELinux 路徑不需要 FFmpeg 或 x264enc；需要 Python gi/Gst binding 及上述外掛。
- Windows／沒有 GStreamer 工具的環境沿用 FFmpeg/libx264。硬體路徑失敗不會悄悄改用另一個編碼器。
- 畫面疊字與縮放仍在 CPU，H.264 編碼才是硬體；沒有宣稱整條影像流程零 CPU 負荷。
- 使用者的 USI SmartCam QIR aarch64 已確認 BGR → NV12 → v4l2h264enc → h264parse → fakesink 可到 EOS。
  此結果尚未驗證 appsrc、Python gi、碼率控制、MP4 分檔與相機同時運作。

## 更新前檢查

先停止 VisionEdge 錄影與取像，保留系統 cam-server 正常運作。
在解壓目錄，以與 VisionEdge 相同的 Python 和服務帳號執行：

```sh
python3 check_recording_env.py
```

新版工具會用測試色塊跑同一個硬體工作程序，驗證碼率屬性設定、EOS 關檔、
至少兩個完成片段，以及 MP4 的 moov/mdat 結構。暫存檔會移除，
不會開相機、不會安裝套件、不會重啟相機服務。
PASS 只代表小尺寸短測通過，不代表 1080p 或 24 小時穩定，也不是影像解碼品質驗證。
FAIL 時保留原版，提供完整輸出再處理。

## 分檔與保護

GStreamer 使用實際時間戳、有限 appsrc 佇列、硬體碼率控制與關鍵影格請求，
以 splitmuxsink 非同步關閉舊片段。只有 fragment-closed 訊息確認完成後，
才把 .mp4.part 改名為 .mp4，供下載；錯誤或強制終止的殘檔不宣稱成功。
段序號與錄影識別碼避免系統校時造成同名覆蓋。
封裝可能在關鍵影格邊界延後，並不保證精準整秒。
參考：https://gstreamer.freedesktop.org/documentation/multifile/splitmuxsink.html

此版仍保留原始 QTI 相機的既有硬體錄影路徑與編碼分支，沒有為節省資源而動相機管線。
因此，含標記錄影可能同時使用另一個編碼 session；設備資源不足時會顯示錄影錯誤。
更新後請先驗證「相機預覽＋檢測＋含標記錄影」同時運作，再進行長時間驗收。

## 驗證範圍

本機已驗證 Gst worker 的輸入分框、BGR stride 補齊、PTS、有限緩衝設定、
碼率/分檔設定、完成片段清單與 bus 錯誤處理（替身測試，不是硬體測試）。
另保留可執行的 Windows FFmpeg 路徑、錄影 API、分檔/異常及介面回歸。
此開發主機沒有現場硬體；完整 GStreamer 分檔實測由上述新版檢查工具執行。

---

## 初版 FFmpeg 開發紀錄（修正版設備差異以上方為準）

# 長時間含標記錄影

此修改適用於含標記錄影，以及 OpenCV 相機／影片來源的原始錄影。
Qualcomm 原始錄影仍使用既有硬體 H.264 路徑，沒有套用本次新增的
FPS、最高解析度及定時分檔功能。未加入 Label 自動觸發或循環覆寫。

## 設備需求與部署

部署時一併更新 `edge_runtime.py`、`long_recording.py`、`visionedge_server.py`、
`static/edge_dashboard.html`、`static/visionedge_i18n.js`。
既有 `config/edge.ini` 不必覆蓋：新增欄位有相容預設，原有設定會保留。
服務需能從 PATH 執行含 **libx264** 的 FFmpeg；不是安裝 Python 套件即可取得。
請在實際服務帳號環境執行 `ffmpeg -hide_banner -encoders`，確認有 libx264。
缺少 FFmpeg 或編碼器時會拒絕錄影，顯示原因，不退回 AVI/MJPEG。
本機測試使用 FFmpeg 7.1；設備端 FFmpeg 版本、編碼可用性仍需驗證。

此版使用獨立 FFmpeg 程序進行**軟體** H.264 編碼，限制編碼執行緒為 2。
尚未驗證把含標記影像送回 Qualcomm 硬體編碼器；不能宣稱硬體加速。
既有 QTI 管線仍包含原始影像硬體編碼分支，實機需測量整體負荷。

## 設定

在「設備設定 → 錄影與儲存空間」調整並儲存。錄影中禁止更改設定。

| 設定 | 新安裝預設 | 說明 |
|---|---|---|
| recording_bitrate | 0（自動） | 含標記／PC 為 2.5 Mbps；QTI 原始維持 1080p 8 Mbps、4K 16 Mbps。可選 2／2.5／3／4／8／16／24 Mbps |
| recording_fps | 15 | 可選 5／10／15／20／30 FPS；舊設定檔已有 30 則保留，建議先改為 15 |
| recording_max_height | 1080 | 720／1080／2160；等比例縮小、不放大，不改變檢測影像 |
| recording_segment_seconds | 600 | UI 可選 5／10／15／30 分鐘；API 限制 60～1800 秒 |
| min_free_mb | 10240 | 保留 10 GiB，空間不足停止錄影，不自動刪除舊影片 |

碼率是受限制的目標碼率，不代表檔案精確固定大小。2.5 Mbps 理論上約
27 GB／24 小時（十進位 GB），尚未包含封裝及其他資料。
固定碼率下，降低 FPS 主要降低處理負荷，不代表容量同比下降。
介面顯示近期實際寫入量、目前段數與估計剩餘時數；前幾秒顯示 `--`。
估計使用最近約一分鐘樣本，靜態畫面的估計不能保證之後大量移動仍可錄同樣久。
刪除本次已完成片段不會讓已寫入量倒退。QTI 原始路徑不提供本次新增的實測容量估計。

## 影像與時間

保留既有含標記影片的語意：每次判定提供一張完整結果畫面，兩次判定間
重複上一張結果畫面；不會把舊框貼到新的相機畫面。錄影 15 FPS 並不表示
15 次／秒的檢測，動作細節仍受檢測更新率限制。結果超過 3 秒未更新則停止並提示。
如需流暢的原始動作，可使用原始影像錄影；本次沒有引入物件追蹤。

以單調時鐘安排畫格，FFmpeg 使用指定 FPS。短暫排程延遲可補上畫格，
編碼落後超過 2 秒就顯示異常並停止；管線超過 5 秒不回應會終止編碼器。
這保護了檢測執行緒，但無法承諾資源不足時仍完整錄影。錯誤不會顯示成成功存檔。

## 分檔與失敗處理

FFmpeg 保持同一編碼程序，以關鍵影格分檔，每段有獨立時間戳，另有整次錄影
唯一識別碼與段序號避免覆蓋。每段時間標記由錄影起始時間加影片時間計算，
系統時間中途校正不會造成同名覆蓋。切點以影片時間與關鍵影格為準，不承諾精確到毫秒。
每段先寫入 `.mp4.part`，確認完成封裝後才改為 `.mp4`；已完成段可在持續錄影時下載。
進行中或未完整封裝的 `.part` 禁止開啟／下載，停止後可刪除失敗殘檔。
突然斷電仍可能損失最後一段及未落盤資料，分檔不是斷電保證。
先前成功片段不因新片段失敗而刪除；不自動覆寫任何影片。

## 驗證

本機：`python long_recording_test.py`（需要 ffmpeg / ffprobe）驗證 H.264 解碼、
分檔、畫面標記、播放長度、設定往返、編碼器失敗、低空間和過期畫面。
另執行 edge_smoke_test.py、edge_consistency_test.py 與既有相關回歸測試。

部署驗收：用現場干擾最多的畫面，以 1080p／15 FPS／2.5 Mbps 起測；
確認文字與工件細節、檢測頻率、CPU、溫度、實際容量與切檔連續性。
再進行 12／24 小時測試、低空間及異常重啟測試。
本機短測通過不等於 Qualcomm 設備已通過全天錄影驗收。

### 2026-09-29 本機驗證結果

- 通過：long_recording_test.py、long_recording_api_test.py、long_recording_ui_test.js。
- 通過：edge_smoke_test.py、edge_consistency_test.py、visionedge_regression_test.py、
  i18n_static_test.py、qti_process_test.py、camera_settings_test.js、live_detail_test.js。
- Windows 本機 1080p／15 FPS／2.5 Mbps 合成移動畫面短測：約 15.47 秒，
  232 張、4 段（測試用 5 秒分段），實測近期約 2.603 Mbps，沒有延遲保護觸發。
  這不是實機品質或持續效能保證，也不是 24 小時壓力測試。
- video_label_e2e_test.py 因本機測試環境缺少 websocket-client 而跳過。
- 尚未部署至現場相機，尚未驗證 Qualcomm 端 FFmpeg/libx264、溫度及全天穩定性。
