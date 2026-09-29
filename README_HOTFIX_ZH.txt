Jetson 含框選錄影 FFmpeg 相容性修補 — 2026-09-30

適用：已安裝 VisionEdge-2.4.9-jetson-long-recording-update-20260930.zip 的 Jetson 專案。
這是修補包，不能單獨用於尚未安裝長時間錄影更新的原始版本，也不適用於 OELinux/QTI 版本。

修正：啟動含框選錄影出現 Unrecognized option 'sc_threshold'，隨後 Broken pipe。
錄影程式與檢查工具移除非必要的 -sc_threshold 0；保留 GOP 與分檔邊界強制關鍵影格。
本包只更新 long_recording.py、check_recording_env.py 及相容性測試。

操作：
1. 將本包解壓至獨立資料夾。使用與 VisionEdge 服務相同帳號、環境及 PATH 執行新版檢查工具，例如：
   cd /你的/VisionEdge
   .venv/bin/python /修補包解壓路徑/check_recording_env.py
   若沒有 .venv，改用實際啟動 VisionEdge 的 Python。
2. 出現 PASS 後，停止 VisionEdge 程式/服務。備份原 long_recording.py、check_recording_env.py。
3. 將本包的 long_recording.py、check_recording_env.py 覆蓋至專案根目錄（與 edge_runtime.py 同層）。
   jetson_recording_compat_test.py 是供開發驗證的測試，可選擇一併覆蓋。
4. 重新啟動 VisionEdge，啟用含框選錄影，停止後確認影片可播放，再確認跨分檔邊界正常。

若檢查失敗，請保留完整輸出；可再提供 ffmpeg -version 及 ffmpeg -hide_banner -encoders。
此修補處理截圖中的參數錯誤；如果該 FFmpeg 未提供 libx264，檢查工具會顯示另一個錯誤，需另行處理。
回復：停止服務，還原備份程式檔，再重新啟動。

驗證：Windows 主機 FFmpeg 7.1，long_recording_test.py、jetson_recording_compat_test.py 通過。
實際 H.264 分檔/解碼、低空間停止、過期影像停止、編碼器失敗、尺寸與設定相容性均通過；
環境檢查產生四段完整 MP4。未在使用者 Jetson/FFmpeg 實機上測試，仍需上述實機確認。
不含設定、資料庫、錄影、憑證或私鑰。
