# Jetson Orin Nano Super + Logitech BRIO 部署

本版本的目標是以 BRIO 取得 3840×2160 MJPEG 影像，用 Jetson CUDA 執行樣板比對，並將錄影軟體編碼為最多 1920×1080。樣板、OK/NG 條件、SOP 和包裝循環仍使用既有資料模型。Mac 開發環境預設 CPU。

## Python 與 OpenCV

Jetson Linux R36.4.3 的系統 Python 是 3.10；登入 shell 的 Miniforge Python 3.13 不適用於這份專案。專案已將 Python 鎖在 3.10，使用 uv 管理 Flask、NumPy 等 Python 套件：

```bash
cd /home/jetson/VisionEdge
/home/jetson/.local/bin/uv sync --inexact
```

一般 PyPI `opencv-python-headless` wheel 沒有 CUDA。Jetson 上必須以 JetPack 的 CUDA toolkit 建置 OpenCV Python 擴充套件；建置腳本會下載 OpenCV 4.11.0 與同版 `opencv_contrib`，編譯 `cudaarithm`、`cudaimgproc` 等必要模組並安裝到本專案 `.venv`，不覆寫系統 OpenCV：

```bash
cd /home/jetson/VisionEdge
sudo apt-get update
sudo apt-get install -y build-essential cmake python3.10-dev \
  libjpeg-dev libpng-dev libavcodec-dev libavformat-dev libavutil-dev \
  libswscale-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
bash scripts/build_jetson_opencv_cuda.sh
```

建置需要 CMake、C/C++ 編譯器、Python 3.10 開發標頭、NumPy、CUDA 12.6，以及 JPEG、FFmpeg、GStreamer 開發套件。建置腳本最後會用隨機樣板驗證 `cv2.cuda.createTemplateMatching` 和實際 GPU 定位。日後重新執行 uv 同步請保留 `--inexact`，以免移除本機建置的 OpenCV。Mac 開發環境使用 `uv sync --extra cpu`。

## 相機與設定

先確認 BRIO 的 by-id 路徑：

```bash
ls -l /dev/v4l/by-id/*BRIO*video-index0
```

新設備可參考 [config/jetson-brio.example.ini](config/jetson-brio.example.ini) 設定 `config/edge.ini`。現場已有設定時先備份，保留產品資料庫、`runtime_data/` 和憑證。範例使用 `backend=opencv`、GStreamer `jpegdec` 擷取 4K MJPEG、`inference_device=cuda`、1080p 錄影、1280px 瀏覽器預覽，且不旋轉影像。開始時 `product_id=0` 只預覽；建立或匯入產品與樣板後，選擇產品並套用才開始檢測。

```bash
cd /home/jetson/VisionEdge
bash run_edge.sh
```

通過相機與 CUDA 驗收後，可用 [deploy/visionedge-jetson.service](deploy/visionedge-jetson.service) 作為 systemd 服務單元。這份檔案的使用者與路徑以目前設備的 `jetson` 帳號和 `/home/jetson/VisionEdge` 為準：

```bash
sudo install -m 0644 deploy/visionedge-jetson.service /etc/systemd/system/visionedge-jetson.service
sudo systemctl daemon-reload
sudo systemctl enable --now visionedge-jetson.service
systemctl status visionedge-jetson.service
```

範例的 Web 服務使用 HTTP `:8080`，供目前共用網路的初次測試。正式接入其他網路前，依現場要求設定 TLS 與存取控制。

## 驗收

從 4K 切換至 1080p（或反向切換）時，已儲存的樣板會依每個樣板的來源影像尺寸縮放 ROI、搜尋範圍與樣板影像，資料庫中的原始座標與圖片不會被改寫。舊資料若沒有來源尺寸，可從產品參考圖推回；兩者都沒有時需先確認原本標註解析度，再補上尺寸或重新標註。不同長寬比會拒絕自動比對，避免錯誤座標產生誤判。即使長寬比相同，BRIO 的視角、曝光或影像細節仍可能隨解析度改變；正式使用前須比對 OK/NG 分數並視情況調整閾值或重做樣板。

「取像與樣板」頁面（`/template-studio`）可選產品、擷取或載入影像、標註多個 Label，再一次儲存；取消編輯會還原未儲存變更。清除全部樣板會保留產品與流程，但被規則或包裝條件引用的 Label 不能刪除。儲存後先試跑 Label，再到「規則／流程」（`/flow-studio`）選取樣板並套用；同產品樣板重新取樣時，規則副本的來源尺寸會同步更新。套用流程會在相機影格之間切換檢測定義，不重新開啟 BRIO；錄影或工件檢測進行中仍需先結束作業。

1. 狀態 API `/api/edge/status` 應顯示 `backend=opencv`、`inference_device=cuda`，套用有樣板的產品後 `inference_device_active=cuda`；相機 `backend_status` 應顯示 `3840×2160` 和 `MJPG`。確認原始擷取影像是 3840×2160，而瀏覽器預覽可縮小。
2. 建立 OK 與 NG 樣板，確認分數、位置、判定與 Mac CPU 路徑一致；擷取檢測履歷亦須使用 CUDA。CUDA 不可用時應明確進入 `ERROR`，不能默默改用 CPU。
3. 連續預覽、檢測與錄影，檢查錄影可播放且解析度 1920×1080；量測 `source_fps`、`infer_fps`、CPU/GPU 使用率、溫度、掉幀與儲存空間。範例錄影設定為 15 FPS，編碼在獨立執行緒處理，並按影格時間補／略影格，避免相機負載變動時影片加速。錄影目前由 OpenCV `mp4v` 軟體編碼，失敗時退回 MJPG AVI。
4. 先在產線實測 1080p 錄影穩定性。4K 錄影只有通過持續時間、掉幀、溫度與檔案完整性測試後才納入下一版。

這台 Jetson 的 `v4l2-ctl` 可收到 BRIO 4K MJPEG 30 FPS。系統 OpenCV 直接透過 V4L2 擷取／解碼約 10 FPS；GStreamer `jpegdec` 單獨取像約 21.5 FPS。已使用 BRIO 實際 4K 畫面執行 CUDA 樣板定位，分數 0.9994、位置正確。服務已啟用並設為開機自啟。最終 90 秒 4K 擷取加 1080p 錄影試驗中，`source_fps` 約 21、影片 15 FPS／1352 影格／90.13 秒（實際 90.16 秒），首尾影格可解碼；錄影佇列只替換 1 個過期輸入影格。這是錄影與預覽的驗收；`product_id=0` 時服務尚未執行產線檢測，須匯入實際產品與樣板，才能驗收 OK/NG、SOP、包裝循環，以及錄影與實際 CUDA 檢測同時運作的負載。

### 預覽延遲控制

網頁預覽使用 `/api/edge/preview.jpg`，每個預覽同時最多一個請求，上限 10 FPS；完整接收與解碼後才取得最新畫面。檢測結果依推論序號去重。背景分頁、離開即時檢測頁、關閉取像視窗或套用設定時會停止預覽並釋放物件網址。此限制不更改相機取像、推論或錄影 FPS。

驗證：`.venv/bin/python preview_backpressure_test.py`、`node preview_lifecycle_test.js`。部署需同時更新後端、HTML 與 `static/latest_preview.js`；已開啟的網頁需重新整理，才能停止舊版 MJPEG 連線。
