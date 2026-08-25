# RO-Online-toolbox

RO Online 桌面工具箱／自動化程式（自用）。

> **開工前先看 [CLAUDE.md](CLAUDE.md)（開發鐵則）與 [GAMEDATA.md](GAMEDATA.md)（已驗證事實庫）。**
> 客戶端解包資料的索引在 [RODATA-INDEX.md](RODATA-INDEX.md)。
>
> GAMEDATA 記錄實測確認的東西（含試過不行的方案）—
> 寫程式時直接當前提用，不要重驗。
> 反過來，驗證出任何新事實也立刻補進去。

## 技術選型

- **GUI**：PySide6 (Qt 6) — 選它是因為自動化程式需要背景執行緒安全回報 UI（Signal/Slot）、
  系統匣常駐、透明 overlay 視窗，這些 Qt 都內建。
- **未來自動化相依**：`mss`（螢幕擷取）、`opencv-python`（影像比對）、
  `pydirectinput`（DirectInput 遊戲送鍵）。裝法見下方 optional dependencies。

## 環境建置

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev,packet,memory]
```

## 啟動

```powershell
.\scripts\run.ps1           # 一般啟動
.\scripts\run.ps1 -Admin    # 以系統管理員啟動（封包擷取必須）
```

或直接指定 venv 的 python：

```powershell
.\.venv\Scripts\python.exe main.py
```

> **不要用 `py main.py`。** 那走的是系統 Python，`scapy`、`psutil` 都不在裡面，
> 封包分頁會停用。main.py 偵測到這種情況會在終端機提醒。

## 測試

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 功能：封包攔截

選一個遊戲視窗，攔截它送出的封包，看 hex dump，匯出成可直接貼給 AI 的文字。

做法是**注入遊戲行程 hook `send`**：把執行檔匯入表（IAT）裡 send 那一格改指向
自寫的 stub，記錄完再跳回真正的 send。因此：

- 拿到的是**送進 send 之前**的內容——就算連線加密，這裡看到的也是明文。
- 每筆附**呼叫鏈**（沿 EBP 框架鏈走出來的返回位址），可以認出「建構這種封包的
  函式」，不同動作各不相同。
- 純讀寫記憶體，不改遊戲邏輯、不搶滑鼠鍵盤，停止時還原 IAT。

移植自 `s26016041/Angels-Online-toolbox` 的 `app/core/injector.py`，
組語 stub 原樣保留，程式碼範圍改成用 pefile 動態解析（原本是寫死的）。

**前提：**

1. **以系統管理員身分執行**（`.\scripts\run.ps1 -Admin`）。
2. 目標必須是 **32 位元**且**匯入表裡有 send**。不符合時按下「開始攔截」
   會直接說明是哪一項不過，不會靜靜失敗。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[packet]
```

網路層的 raw socket 後端仍保留在 `services/raw_capture.py`，但它收不到
伺服器回應（Windows 的 TCP 堆疊會攔下 inbound TCP，見 GAMEDATA [PKT-003]），
所以正式功能走注入。

## 功能：記憶體掃描

Cheat Engine 風格，找出遊戲數值（經驗、HP、金錢…）存在哪個記憶體位址：

1. 選遊戲視窗 → 「選定此程序」
2. 選型別（通常是 4 位元組整數），輸入現在看到的數字 → 首次搜尋
   （不知道確切數字就用「未知初始值」）
3. 回遊戲讓數值改變 → 條件改成「增加 / 減少 / 已改變」→ 再次搜尋
4. 重複幾次，候選就會剩下幾個位址
5. 「加入觀察」持續看即時值，也能寫入

另外支援字串搜尋（角色名、地圖名），編碼可選 UTF-16 / ASCII / UTF-8。
掃描跑在背景執行緒，不會卡介面。

掃描核心（`services/memory_scan.py`）從 Angels-Online-toolbox 原樣移植，
純 ctypes + numpy，對原專案零耦合所以整檔搬入。

## 目錄結構

```
src/ro_toolbox/
├── app.py              # 組裝 QApplication、設定、樣式、主視窗
├── __main__.py         # python -m ro_toolbox 入口
├── config/             # 路徑常數與 settings.json 讀寫
│   ├── paths.py
│   └── settings.py
├── core/                    # 核心邏輯，不依賴任何 UI 類別
│   ├── engine.py            # 自動化引擎狀態機（骨架）
│   ├── events.py            # 狀態列舉
│   ├── packet.py            # 封包資料模型
│   └── worker.py            # QThread worker 基底
├── services/                # 與外部世界互動
│   ├── packet_capture.py    # 封包擷取（scapy + Npcap）
│   ├── process_monitor.py   # 行程與 TCP 連線查詢
│   ├── capture.py           # 螢幕擷取（尚未實作）
│   ├── input.py             # 鍵鼠輸入（尚未實作）
│   └── vision.py            # 影像辨識（尚未實作）
├── ui/
│   ├── main_window.py       # 側欄 + 分頁堆疊 + 日誌面板 + 狀態列
│   ├── models/              # table model（封包列表）
│   ├── pages/               # 各分頁（總覽／封包／自動化／設定）
│   ├── widgets/             # 可重用元件（側欄、日誌、hex 檢視）
│   └── resources/styles/app.qss
└── utils/
    ├── hexdump.py           # hex dump 與匯出格式
    └── logging.py           # 檔案 + 主控台 + UI 三路日誌
```

分層原則：`ui` 可以 import `core`／`services`，反向不行。要新增分頁時，
在 `ui/pages/` 建一個繼承 `BasePage` 的類別，加進 `main_window.PAGE_CLASSES` 即可。

設定與日誌寫在 `%APPDATA%\RO-Online-toolbox\`。

## 打包

```powershell
.\scripts\build.ps1
```
