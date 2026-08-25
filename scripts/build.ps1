# 打包成單一 exe ＋ 冒煙測試。
#
# 這支只是包裝：真正的設定在 RO-Online-toolbox.spec，流程在 build_local.py。
# **不要**在這裡另外寫一份 PyInstaller 參數 —— 兩份設定遲早會不一致，
# 而且不一致的那天你會以為發出去的跟本機編的是同一顆。
#
# 用法：
#   .\scripts\build.ps1            # 編正式版 ＋ 冒煙測試
#   .\scripts\build.ps1 -Debug     # 帶主控台的除錯版（看得到 traceback）
#   .\scripts\build.ps1 -Run       # 通過後把 GUI 開起來眼睛確認

param(
    [switch]$Debug,
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$args = @()
if ($Debug) { $args += "--debug" }
if ($Run)   { $args += "--run" }

& "$root\.venv\Scripts\python.exe" "$root\build_local.py" @args
exit $LASTEXITCODE
