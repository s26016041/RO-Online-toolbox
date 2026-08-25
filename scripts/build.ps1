# 打包成單一 exe（功能完成後再用）
# 用法： .\scripts\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

& "$root\.venv\Scripts\python.exe" -m PyInstaller `
    --noconfirm `
    --windowed `
    --name "RO-Toolbox" `
    --icon "$root\src\ro_toolbox\ui\resources\icon.ico" `
    --paths "$root\src" `
    --add-data "$root\src\ro_toolbox\ui\resources;ro_toolbox/ui/resources" `
    "$root\src\ro_toolbox\__main__.py"

Write-Host "輸出：$root\dist\RO-Toolbox"
