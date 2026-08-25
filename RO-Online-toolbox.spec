# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 打包設定（單一 .exe）。
#
# 這是**唯一一份**打包設定：build_local.py（本機驗證）與 release.py（發布）
# 都用它，所以「本機編出來的」和「發出去的」保證是同一顆。
#
# 三個非收不可的東西，漏了都不會報錯、只會安靜地壞掉：
#
# 1. `assets/*.json.gz`（道具／怪物／傳點表）—— 漏收的話 item_name() 一律查不到，
#    自動補水的選單整個空白，程式完全不會抱怨。路徑對應
#    `config/paths.py` 的 `_bundle_root()`。
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
for package in ("numpy", "capstone", "pymem", "pefile", "psutil"):
    try:
        found_datas, found_binaries, found_hidden = collect_all(package)
    except Exception:                      # noqa: BLE001 - 沒裝就跳過，不擋打包
        continue
    datas += found_datas
    binaries += found_binaries
    hiddenimports += found_hidden

# scapy 只在有 Npcap 的機器上用得到，整包收進去會胖很多且大部分用不到。
# 只收實際會 import 的那幾支（services/npcap_capture.py）。
hiddenimports += [
    "scapy.all",
    "scapy.layers.inet",
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
