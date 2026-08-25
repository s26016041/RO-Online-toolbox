# 啟動 RO Toolbox（使用專案 venv 的 Python）
#
#   .\scripts\run.ps1           一般啟動
#   .\scripts\run.ps1 -Admin    以系統管理員身分啟動（封包擷取必須）

param([switch]$Admin)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$entry = Join-Path $root "main.py"

if (-not (Test-Path $python)) {
    Write-Host "找不到 venv：$python" -ForegroundColor Red
    Write-Host "請先建立環境："
    Write-Host "    py -3.12 -m venv .venv"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install -e .[dev,packet,memory]"
    exit 1
}

if ($Admin) {
    Write-Host "以系統管理員身分啟動…"
    Start-Process -FilePath $python -ArgumentList $entry -WorkingDirectory $root -Verb RunAs
} else {
    & $python $entry
}
