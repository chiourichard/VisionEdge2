Jetson Orin Nano 私有錄影 FFmpeg 修補 — 2026-09-30
適用：已安裝長時間錄影更新的 VisionEdge Jetson 版本，Ubuntu 24.04 / aarch64。
此包整合前次 sc_threshold 修補。不是原始專案的完整升級包，也不適用於 QTI/OELinux。

原因：NVIDIA FFmpeg 8.0.1 建置未提供 libx264 與 segment；apt 優先來源仍是 NVIDIA。
此包讓錄影程式及檢查工具優先使用 tools/recording-ffmpeg/bin/ffmpeg。
不替換 /usr/bin/ffmpeg，不調整 NVIDIA apt 優先權，不改 CUDA/OpenCV 或服務設定。

安裝步驟（在 Jetson 上操作）：
1. 停止 VisionEdge：sudo systemctl stop visionedge-jetson.service
2. 備份原專案 long_recording.py、check_recording_env.py；若已有 recording_ffmpeg.py 也請備份。
3. 將本 ZIP 解壓至 ~/VisionEdge，讓 long_recording.py 與 edge_runtime.py 在同層。
4. 用原 VisionEdge 使用者執行，不要 sudo bash：
   cd ~/VisionEdge
   bash install_recording_ffmpeg.sh
5. 看到最後的 PASS: private recording FFmpeg installed 後執行：
   sudo systemctl restart visionedge-jetson.service
6. 開啟含框選錄影、停止並播放確認，再測試跨分檔及 AI 偵測並行負載。

腳本會使用 sudo apt-get 安裝 build-essential、pkg-config、libx264-dev、curl、ca-certificates、xz-utils；
需要網路、sudo 權限、額外磁碟空間及編譯時間。使用兩個編譯工作以限制負載。
FFmpeg 7.1.3 原始碼取自 https://ffmpeg.org/releases/ffmpeg-7.1.3.tar.xz，
只編入 BGR rawvideo -> libx264 -> MP4/segment 錄影用途，未啟用網路輸入。
FFmpeg 自身函式庫靜態連結，libx264/libc 使用 Ubuntu 系統函式庫。
啟動包裝腳本僅清除 FFmpeg 子程序 LD_LIBRARY_PATH，VisionEdge/CUDA/OpenCV 環境不變。
成功前先用實際編碼與四段 MP4 檢查，成功才移至 tools/recording-ffmpeg。
已有該目的資料夾時腳本停止而不覆蓋。下載或編譯失敗保留日誌並退出。
失敗請提供終端最後錯誤及腳本顯示的日誌路徑，勿自行替換系統 FFmpeg。
腳本不會自動啟動服務；需要恢復偵測時可執行上述 restart。

已驗證：Windows 主機 FFmpeg 7.1 的錄影/分檔/解碼/空間保護測試、私有路徑選擇、
Jetson 設定相容性測試及 Bash 語法檢查。尚未於 Ubuntu aarch64 執行來源編譯；
本包不是已編譯 ARM64 二進位，現場 PASS 是必要驗收條件，並需另測長時間負載。

回復：停止服務、還原備份的 Python 檔後重啟。私有 tools 目錄可保留，不會更動系統 FFmpeg。
新增加的 recording_ffmpeg.py 不會被還原後的舊程式使用。
本包不含設定、資料庫、憑證或使用者錄影。
