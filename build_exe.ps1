$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:PYTHONPATH = Join-Path $root ".pyinstaller_patch"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root ".pyinstaller_cache\pyinstaller"

python -m PyInstaller --clean --noconfirm --onedir --name app_icon_reporter --hidden-import win32com --hidden-import win32com.client --hidden-import pythoncom --hidden-import pywintypes app_icon_reporter.py
python -m PyInstaller --clean --noconfirm --onedir --name macro_pad_runtime --add-data "config.html;." --add-data "pwa;pwa" --add-data "macro_pad_layout.json;." macro_pad_runtime.py

Write-Host "Build complete:"
Write-Host "  dist\\app_icon_reporter\\app_icon_reporter.exe"
Write-Host "  dist\\macro_pad_runtime\\macro_pad_runtime.exe"
