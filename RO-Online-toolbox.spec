# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 打包設定（單一 .exe）。
#
# 這是**唯一一份**打包設定：build_local.py（本機驗證）與 release.py（發布）
# 都用它，所以「本機編出來的」和「發出去的」保證是同一顆。
#
# 三個非收不可的東西，漏了都不會報錯、只會安靜地壞掉：
#
# 1. `assets/` —— **使用者的電腦沒有 `RODATA/`，這裡是那些資料的唯一來源**。
#    漏收不會報錯，只會安靜地少一塊功能。路徑對應 `config/paths.py`
#    的 `_bundle_root()`，每一份都有對應的 `tools/build_*.py` 可以重建：
#
#      items.json.gz    1.3 MB  漏收 → item_name() 查不到、補水選單全空
#      mobs.json.gz      90 KB  漏收 → 草／MVP 過濾失效（會去打草或送死）
#      warps.json.gz     31 KB  漏收 → 跨圖尋路算不出路線
#      mapnames.json.gz   8 KB  漏收 → 尋路只顯示得出 prt_fild08 這種內部名
#      terrain.bin.gz   1.5 MB  ⛔ 漏收 → **走路類功能全滅**（不漫遊、尋路停用）
#      icons.bin        3.0 MB  漏收 → 道具圖示全空白（外觀降級，功能不受影響）
# 2. `ro_toolbox/ui/resources/`（icon.ico、styles/*.qss）—— 漏收會變成
#    沒有圖示的白底視窗。
# 3. 有原生 DLL 或 lazy import 的套件（pymem、pefile、capstone、numpy…）——
#    PyInstaller 的靜態分析收不齊，要 collect_all。
#
# 設環境變數 ROT_CONSOLE=1 可以編出「帶主控台的除錯版」（看得到 traceback、
# 名稱加 -debug 後綴）。build_local.py --debug 會用到。
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("VERSION", "."),
    ("assets", "assets"),
    ("src/ro_toolbox/ui/resources", "ro_toolbox/ui/resources"),
]
binaries = []
hiddenimports = []

# 分頁與服務都是靜態 import，但服務裡有不少「用到才 import」的（capstone、
# pymem、scapy…）。整包收進來最省事，也不會因為某天改成動態載入就白屏。
hiddenimports += collect_submodules("ro_toolbox")

# 這幾個有原生 DLL 或 lazy import，要 collect_all 才收得齊。
# 少了它們的症狀分別是：記憶體掃描變慢或報錯（numpy）、
# 封包長度表抽不出來 → 切包退回啟發式（capstone）、
# 注入功能不可用（pymem/pefile，本專案不用但 import 得到才不會炸）。
# zxingcpp 是編譯出來的擴充模組，漏收的話帳號頁的「匯入 QR」會停用
# （qr.available() 回 False），程式不會炸但功能就少一半。
# pydivert 一定要 collect_all：它帶著 **WinDivert64.dll 與 WinDivert64.sys**，
# 那兩個檔案就是「使用者不必自己安裝 Npcap」的全部原因。漏收的話
# exe 跑起來抓不到任何封包，而且錯誤訊息會指向「沒裝 pydivert」，
# 讓人以為是相依沒裝好（見 services/packet_capture.py）。
for package in ("numpy", "capstone", "pymem", "pefile", "psutil", "zxingcpp",
                "pydivert"):
    try:
        found_datas, found_binaries, found_hidden = collect_all(package)
    except Exception:                      # noqa: BLE001 - 沒裝就跳過，不擋打包
        continue
    datas += found_datas
    binaries += found_binaries
    hiddenimports += found_hidden

hiddenimports += [
    "PySide6.QtNetwork",
]

DEBUG_CONSOLE = os.environ.get("ROT_CONSOLE", "0") == "1"
APP_NAME = "RO-Online-toolbox" + ("-debug" if DEBUG_CONSOLE else "")


a = Analysis(
    ["main.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 檔案總管／桌面／捷徑上看到的圖示（嵌進 exe）。
    # 執行中的視窗左上角與工作列圖示是另一條路，由 app.py 的
    # setWindowIcon + SetCurrentProcessExplicitAppUserModelID 負責。
    icon="src/ro_toolbox/ui/resources/icon.ico",
    console=DEBUG_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
