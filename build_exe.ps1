$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m PyInstaller --clean --noconfirm --onedir --name app_icon_reporter app_icon_reporter.py
python -m PyInstaller --clean --noconfirm --onedir --name macro_pad_runtime --add-data "config.html;." --add-data "pwa;pwa" --add-data "macro_pad_layout.json;." macro_pad_runtime.py

Write-Host "Build complete:"
Write-Host "  dist\\app_icon_reporter\\app_icon_reporter.exe"
Write-Host "  dist\\macro_pad_runtime\\macro_pad_runtime.exe"
