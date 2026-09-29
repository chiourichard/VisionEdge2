# Jetson Orin Nano 長時間錄影更新（2026-09-30）

以使用者提供的 `VisionEdge-2.4.9-jetson-integrated (1).zip` 為基礎整合。
這是 Jetson 專用更新，請勿拿 OELinux/QTI 更新包覆蓋。

## 功能

- 原始與含標記錄影改用 FFmpeg/libx264 → H.264 MP4，不再退回 MJPEG AVI。
- 可設定碼率、輸出 FPS、最高解析度與 5/10/15/30 分鐘分檔。
- 每段先寫 `.mp4.part`，完成封裝後才改名為可下載的 `.mp4`。
- 檔名包含本次錄影識別碼、段序號與片段時間戳，系統校時不造成同名覆蓋。
- 顯示編碼器、段數、近期實測碼率、估計剩餘時間；剛開始尚無樣本時顯示 `--`。
- 獨立 FFmpeg 編碼程序與有限影像暫存，低空間、編碼失敗、停滯或影像過期會停止並提示。
- 已完成的段可在持續錄影時下載；未完成檔案禁止開啟/下載，停止後可刪除殘檔。
- 不加入 Label 觸發、循環覆寫、自動刪除或自動恢復。

## Jetson 的編碼限制

Orin Nano 沒有 NVENC。這版明確使用 CPU 的 libx264，不因為系統有 GStreamer
就選擇 QTI v4l2h264enc，也不呼叫 NVIDIA 硬體編碼器。
CUDA 仍用於既有樣板比對，BRIO V4L2/GStreamer 取像設定不變。
獨立程序能避免同步寫檔直接卡住檢測，但仍共用 CPU、記憶體頻寬及儲存裝置，
實際負載與溫度需要現場驗收。

官方依據：
https://docs.nvidia.com/jetson/archives/r36.5/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html

## 更新前檢查

Jetson 的服務環境需在 PATH 找到含 libx264 的 FFmpeg 執行檔。
OpenCV 能使用 FFmpeg 解碼，不代表系統已安裝 ffmpeg 命令列工具。
不要安裝 CPU 版 OpenCV wheel 來解決錄影依賴，以免替換現有 CUDA OpenCV。

將更新包解壓到暫存資料夾，用 VisionEdge 的 Python 環境執行新版檢查工具。例如：

```sh
cd /home/jetson/VisionEdge
.venv/bin/python /更新包解壓目錄/check_recording_env.py
```

請將範例路徑換成實際路徑。檢查工具不開相機、不安裝套件、不重啟服務；
它在暫存目錄測試 libx264、指定碼率及多段 MP4 封裝，並刪除測試檔。
若缺少 FFmpeg，先由設備管理員安裝適用於該 Jetson 系統的 FFmpeg/libx264，
再執行檢查。PASS 只是基本編碼與封裝能力確認，不代表全天穩定。

## 套用

1. 確認檢查工具 PASS，再停止錄影與 VisionEdge 程序。
2. 備份更新包同名的現有程式檔；原有 config、db、uploads、runtime_data 與憑證保留。
3. 將更新包內容覆蓋到現有專案根目錄，合併 static，不要整個刪除 static。
4. 依原本方式啟動服務，瀏覽器 Ctrl+F5。
5. 在錄影設定先使用 2.5 Mbps、15 FPS、1080p、每 10 分鐘一檔，保留原有空間門檻。
   儲存設定需停止錄影；既有套用流程可能重新啟動相機取像。

更新包不含現場設定、資料庫、憑證或影片，不修改 uv.lock、pyproject.toml、
OpenCV CUDA 建置、相機控制、模板、SOP、產品資料與預覽請求控制。

## 舊設定相容性

- 原有 recording_fps / recording_bitrate 會保留；不會強制把 30 FPS 改為 15。
- recording_max_height 缺少時為 0，UI 顯示「沿用既有錄影尺寸」：使用既有
  recording_width / recording_height 作為上限，等比例縮小、不放大。
- 選擇 720p/1080p/2160p 後，使用新高度上限取代舊寬高上限，仍保留畫面比例。
  2160p 會增加軟體編碼負荷，未經現場持續測試，不建議用於正式全天錄影。
- 新增的分檔預設為 600 秒；沿用取像與檢測解析度，不更動 CUDA/CPU 選擇。
- 自動碼率在 Jetson 路徑為 2.5 Mbps（不是精確固定檔案大小）。
- 空間門檻是固定容量，不是百分比。預設 min_free_mb=10240，即 10 GiB；
  若現場已有其他門檻則保留。低於門檻停止並嘗試完成關檔，不自動刪舊片。

2.5 Mbps 理論上約 27 GB/24 小時（未計其他資料與封裝）。容量估計使用近期
實際寫入量，畫面由靜態變成大量移動時，估計會變化。

## 影像時間與失敗保護

錄影採固定輸出 FPS 與單調時鐘排程。含標記模式只接收新判定完成的結果畫面，
兩次判定間重複上一張完整結果，避免把舊框貼到新的相機影像。
所以 15 FPS 錄影不等於每秒 15 次判定。連續 3 秒沒有新錄影影像會停止。
編碼落後超過 2 秒停止，超過 5 秒無寫入進展會終止編碼程序；這不是不掉幀保證。
突然斷電或被強制終止仍可能損失最後一段。清出空間後需手動重啟錄影。

## 驗證範圍

本機合成影像已通過：H.264 解碼、分檔、時間長度、碼率/解析度設定、
低空間停止、編碼失敗、過期影像、錄影 API、完成/未完成檔案保護、媒體更新、
舊錄影尺寸相容、寬高比、Jetson 設定往返、前端設定與多語系。
既有預覽請求控制、預覽生命週期、提示介面、解析度樣板比對與產品切換回歸通過。
CUDA/BRIO 相關核心檔案與附件保持一致；沒有把本機 CPU 測試當成 GPU 實機驗收。

尚未在現場 Orin Nano 執行本版的「BRIO 4K＋CUDA 檢測＋H.264 錄影」並行測試，
也未完成 12/24 小時穩定性驗收。原部署文件中的 90 秒 mp4v 測試屬於舊版，
不能代替本次 H.264 的效能證據。

## 回復

停止服務，將備份的 edge_runtime.py、visionedge_server.py、
static/edge_dashboard.html、static/visionedge_i18n.js 及測試檔還原，再啟動。
新增的 long_recording.py 留著不會被舊程式引用；影片不需刪除。
