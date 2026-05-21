# Phone Macro Pad PWA

這個專案把手機螢幕變成類似 Stream Deck 的 4x8 PWA 控制面板。整體分成三個部分：

1. `config.html`：電腦端一次性拖拉設定頁，負責把 APP、媒體鍵、巨集鍵安排到 layout。
2. `pwa/`：手機上的 PWA，透過 MQTT WebSocket 收到 layout 後顯示控制方塊，按下方塊後送出 action。可由 GitHub Pages 直接開啟。
3. `macro_pad_runtime.py`：電腦端 Python runtime，讀取 `macro_pad_layout.json`，把 layout 傳給手機，並接收 action 執行 APP 啟動、媒體控制或鍵盤巨集。

## 安裝套件

```powershell
python -m pip install -r requirements.txt
```

## 產生 Windows APP 清單

先掃描電腦上的開始功能表與桌面捷徑：

```powershell
python app_icon_reporter.py
```

預設會建立並自動開啟 `installed_apps_icons_report.html`，可直接在報表中勾選要放進 Macro Pad 的 APP：

```text
installed-app-icons-report/
  installed_apps_icons.csv
  installed_apps_icons_report.html
  icons/
```

## GitHub Pages 使用方式

公開發佈後可以直接使用：

```text
https://jjtmutw.github.io/macro_pad_html/config.html
https://jjtmutw.github.io/macro_pad_html/pwa/
```

設定頁在 GitHub Pages 上執行時，建議匯入 `app_icon_reporter.py` 產生的 `installed_apps_icons_report.html`，因為 HTML 報表內含 icon data，可以讓輸出的 `macro_pad_layout.json` 不依賴本機網站或 Windows 檔案路徑。

手機 PWA 從 GitHub Pages 開啟時，MQTT 必須使用安全 WebSocket：

```text
wss://broker.emqx.io:8084/mqtt
```

瀏覽器通常會阻擋 HTTPS 網頁連到 `ws://`。若只是區網測試，可改用本機 HTTP 開發模式；若要推廣給一般使用者，建議架設支援 TLS 的 MQTT WebSocket broker。

## 編輯 4x8 控制頁

若使用 GitHub Pages，可直接開啟公開設定頁。若要用本機模式，也可以啟動 Python runtime 後，用電腦瀏覽器打開設定頁：

```powershell
python macro_pad_runtime.py --mqtt-host 127.0.0.1
```

設定頁網址：

```text
http://電腦IP:8080/config.html
```

在設定頁中：

- 匯入 `installed-app-icons-report/installed_apps_icons.csv`
- 把 APP 從左側清單拖到 4x8 方格
- 設定頁面名稱，例如「啟動應用程式」、「多媒體操控」、「巨集鍵盤指令」
- 按「儲存 / 匯出 layout JSON」
- 將輸出的檔案存成專案根目錄的 `macro_pad_layout.json`

設定頁只需要在要修改版面時使用；平常不需要長駐。

## 執行電腦端 Runtime

```powershell
python macro_pad_runtime.py --mqtt-host 127.0.0.1 --base-topic macro-pad
```

常用參數：

```text
--layout macro_pad_layout.json
--mqtt-host 192.168.1.10
--mqtt-port 1883
--mqtt-user 使用者
--mqtt-password 密碼
--base-topic macro-pad
--http-port 8080
```

Runtime 會：

- 讀取 `macro_pad_layout.json`
- 發布 layout 到 `macro-pad/layout`
- 接收手機按鈕事件 `macro-pad/action`
- 在收到手機 `macro-pad/hello` 時重新傳送 layout
- 提供手機 PWA 網頁：`http://電腦IP:8080/pwa/`

如果已經使用 GitHub Pages 提供 `config.html` 與 `pwa/`，runtime 可關閉本機網頁伺服器：

```powershell
python macro_pad_runtime.py --mqtt-host 127.0.0.1 --base-topic macro-pad --no-http
```

## MQTT Broker 注意事項

手機瀏覽器不能直接連一般 TCP MQTT `1883`，必須使用 MQTT over WebSocket。GitHub Pages 版本預設使用：

```text
wss://broker.emqx.io:8084/mqtt
```

如果使用 Mosquitto，可以開一個 WebSocket listener，概念如下：

```text
listener 1883
protocol mqtt

listener 9001
protocol websockets
```

Python runtime 連 `1883`，手機 PWA 連 `9001`。

## 手機 PWA

手機打開：

```text
http://電腦IP:8080/pwa/
```

第一頁填 MQTT WebSocket URL、帳號密碼與 base topic。連線後，Python runtime 會把 layout 傳到手機，手機會顯示後續控制頁。

若要正式安裝成 PWA，手機瀏覽器通常需要 HTTPS 或 localhost 這類安全來源。開發測試時可先直接用瀏覽器全螢幕開啟。

## Layout 動作格式

啟動 APP：

```json
{
  "type": "launch",
  "targetPath": "C:\\Program Files\\App\\app.exe",
  "arguments": "",
  "workingDirectory": "C:\\Program Files\\App"
}
```

多媒體控制：

```json
{ "type": "media", "command": "play_pause" }
```

支援的 command：

```text
play_pause, next, previous, stop, mute, volume_down, volume_up
```

鍵盤巨集：

```json
{ "type": "hotkey", "keys": ["ctrl", "shift", "esc"] }
```

輸入文字：

```json
{ "type": "text", "text": "Hello" }
```
