# -*- mode: python ; coding: utf-8 -*-
#
# 送輸入的**小 exe**（`ro-input.exe`）。
#
# ⚠⚠ **它的重點就是「小」。** 83 MB 的主 exe 送輸入會被 GameGuard 隨機整批
# 擋掉（同一個視窗、同一時間交錯實測：主 exe PostMessage 5/10、SendInput 4/10；
# 同一台機器上 7 MB 的小 exe 20/20 全過）。詳見 GAMEDATA [INP-023]。
#
# 所以這裡最重要的一行是 `excludes`：**Qt、numpy 一旦被收進來就前功盡棄**
# （這顆會從 7 MB 變回 80 MB，然後照樣被擋，而且沒有任何錯誤訊息）。
# `tools/check_input_worker.py` 會把大小釘住，超過就讓打包失敗。
#
# 編出來的東西由 `RO-Online-toolbox.spec` 收進主 exe 當資料檔，
# 執行時在 `sys._MEIPASS/ro-input.exe`（見 `input_helper.input_worker()`）。

#: 這顆只准帶 ctypes ＋ pywin32。清單裡任何一個被收進來都是**打包設定寫錯了**。
EXCLUDES = [
    "PySide6", "shiboken6", "numpy", "capstone", "pymem", "pefile",
    "pydivert", "scapy", "zxingcpp", "PIL", "matplotlib", "tkinter",
    "ro_toolbox.ui", "ro_toolbox.core", "ro_toolbox.services.game_screen",
    "ro_toolbox.services.aob", "ro_toolbox.services.memory_scan",
]

a = Analysis(
    ["src/ro_toolbox/input_worker.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=["win32api", "win32con", "win32gui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
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
    name="ro-input",
    exclude_binaries=False,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # ⚠ 不要 UPX：壓過的執行檔是防作弊軟體的經典紅旗。
    # （這台機器本來就沒裝 upx，PyInstaller 會安靜地跳過 —— 明寫著比較保險。）
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # ⚠ **一定要有主控台**：這顆的回報（`DONE n`、失敗原因）走 stdout／stderr，
    # 主行程靠那些字判斷「重送安不安全」。console=False 會把輸出丟掉。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
