#!/usr/bin/env python3
"""
Build a selectable Windows app icon report.

What it does:
1. Scans Start Menu and Desktop shortcuts.
2. Resolves each app shortcut target, arguments, working directory, and icon source.
3. Extracts 256x256 PNG icons through Windows Shell.
4. Generates:
   - installed_apps_icons_report.html
   - installed_apps_icons.csv
   - icons/*.png

The HTML report lets you search, check apps, and export selected apps into a
new selected-apps-output folder with a selected HTML report, CSV, and icons.

Requirements:
- Windows
- Python 3.8+
- Windows PowerShell, included with Windows
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path.cwd() / "installed-app-icons-report"


POWERSHELL_COLLECTOR = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8

$outDir = '__OUT_DIR__'
$iconDir = Join-Path $outDir 'icons'
New-Item -ItemType Directory -Force -Path $iconDir | Out-Null

$code = @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

[ComImport]
[Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellItemImageFactory
{
    void GetImage(SIZE size, SIIGBF flags, out IntPtr phbm);
}

[StructLayout(LayoutKind.Sequential)]
struct SIZE { public int cx; public int cy; }

[Flags]
enum SIIGBF
{
    SIIGBF_RESIZETOFIT = 0x00,
    SIIGBF_BIGGERSIZEOK = 0x01,
    SIIGBF_MEMORYONLY = 0x02,
    SIIGBF_ICONONLY = 0x04,
    SIIGBF_THUMBNAILONLY = 0x08,
    SIIGBF_INCACHEONLY = 0x10
}

public class ShellIconExtractor
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        [MarshalAs(UnmanagedType.LPStruct)] Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory ppv);

    [DllImport("gdi32.dll")]
    static extern bool DeleteObject(IntPtr hObject);

    public static bool SaveIcon(string path, string output, int size)
    {
        try
        {
            var iid = new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
            IShellItemImageFactory factory;
            SHCreateItemFromParsingName(path, IntPtr.Zero, iid, out factory);
            IntPtr hbm;
            factory.GetImage(new SIZE { cx = size, cy = size }, SIIGBF.SIIGBF_ICONONLY | SIIGBF.SIIGBF_BIGGERSIZEOK, out hbm);
            try
            {
                using (var bmp = Image.FromHbitmap(hbm))
                {
                    bmp.Save(output, ImageFormat.Png);
                }
            }
            finally { DeleteObject(hbm); }
            return true;
        }
        catch { return false; }
    }
}
'@
Add-Type -TypeDefinition $code -ReferencedAssemblies System.Drawing

function Convert-ToSafeFileName([string]$text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0,16)
    }
    finally { $sha.Dispose() }
}

function Resolve-IconPath([string]$iconLocation, [string]$targetPath, [string]$shortcutPath) {
    if (![string]::IsNullOrWhiteSpace($iconLocation)) {
        $raw = $iconLocation.Trim()
        $pathPart = $raw
        if ($raw -match '^(.*),(\-?\d+)\s*$') { $pathPart = $Matches[1] }
        $pathPart = $pathPart.Trim().Trim('"')
        if (![string]::IsNullOrWhiteSpace($pathPart)) {
            return [Environment]::ExpandEnvironmentVariables($pathPart)
        }
    }
    if (![string]::IsNullOrWhiteSpace($targetPath)) {
        return [Environment]::ExpandEnvironmentVariables($targetPath)
    }
    return $shortcutPath
}

function Read-UrlShortcut([string]$path) {
    $url = ''
    try {
        foreach ($line in [System.IO.File]::ReadLines($path)) {
            if ($line.StartsWith('URL=', [System.StringComparison]::OrdinalIgnoreCase)) {
                $url = $line.Substring(4)
                break
            }
        }
    } catch {}
    return $url
}

$includeUrlShortcuts = __INCLUDE_URL_SHORTCUTS__

$scanRoots = @(
    [Environment]::GetFolderPath('CommonStartMenu'),
    [Environment]::GetFolderPath('StartMenu'),
    [Environment]::GetFolderPath('CommonDesktopDirectory'),
    [Environment]::GetFolderPath('DesktopDirectory')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$shell = New-Object -ComObject WScript.Shell
$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$items = New-Object System.Collections.Generic.List[object]

foreach ($scanRoot in $scanRoots) {
    $shortcutFiles = New-Object System.Collections.Generic.List[object]
    Get-ChildItem -LiteralPath $scanRoot -Recurse -File -Filter '*.lnk' -ErrorAction SilentlyContinue | ForEach-Object {
        $null = $shortcutFiles.Add($_)
    }
    if ($includeUrlShortcuts) {
        Get-ChildItem -LiteralPath $scanRoot -Recurse -File -Filter '*.url' -ErrorAction SilentlyContinue | ForEach-Object {
            $null = $shortcutFiles.Add($_)
        }
    }
    $shortcutFiles | ForEach-Object {
        try {
            $name = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
            $target = ''
            $args = ''
            $workingDirectory = ''
            $iconLocation = ''

            if ($_.Extension -ieq '.lnk') {
                $sc = $shell.CreateShortcut($_.FullName)
                $target = $sc.TargetPath
                $args = $sc.Arguments
                $workingDirectory = $sc.WorkingDirectory
                $iconLocation = $sc.IconLocation
            } else {
                $target = Read-UrlShortcut $_.FullName
            }

            $dedupeKey = (($name + '|' + $target + '|' + $args + '|' + $_.FullName).ToLowerInvariant())
            if ($seen.Add($dedupeKey)) {
                $iconSource = Resolve-IconPath $iconLocation $target $_.FullName
                $hash = Convert-ToSafeFileName ($_.FullName + '|' + $target + '|' + $iconSource)
                $iconFile = Join-Path $iconDir ($hash + '.png')
                $null = $items.Add([PSCustomObject]@{
                    AppName = $name
                    ShortcutPath = $_.FullName
                    TargetPath = $target
                    Arguments = $args
                    WorkingDirectory = $workingDirectory
                    IconSource = $iconSource
                    IconPng = $iconFile
                })
            }
        } catch {}
    }
}

$items.ToArray() | Sort-Object AppName | ConvertTo-Json -Depth 5 -Compress
"""


POWERSHELL_ICON_EXTRACTOR = r"""
$ErrorActionPreference = 'Stop'
$source = '__SOURCE__'
$fallback = '__FALLBACK__'
$output = '__OUTPUT__'

$code = @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

[ComImport]
[Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellItemImageFactory
{
    void GetImage(SIZE size, SIIGBF flags, out IntPtr phbm);
}

[StructLayout(LayoutKind.Sequential)]
struct SIZE { public int cx; public int cy; }

[Flags]
enum SIIGBF
{
    SIIGBF_RESIZETOFIT = 0x00,
    SIIGBF_BIGGERSIZEOK = 0x01,
    SIIGBF_MEMORYONLY = 0x02,
    SIIGBF_ICONONLY = 0x04,
    SIIGBF_THUMBNAILONLY = 0x08,
    SIIGBF_INCACHEONLY = 0x10
}

public class SingleShellIconExtractor
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        [MarshalAs(UnmanagedType.LPStruct)] Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory ppv);

    [DllImport("gdi32.dll")]
    static extern bool DeleteObject(IntPtr hObject);

    public static bool SaveIcon(string path, string output, int size)
    {
        try
        {
            var iid = new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
            IShellItemImageFactory factory;
            SHCreateItemFromParsingName(path, IntPtr.Zero, iid, out factory);
            IntPtr hbm;
            factory.GetImage(new SIZE { cx = size, cy = size }, SIIGBF.SIIGBF_ICONONLY | SIIGBF.SIIGBF_BIGGERSIZEOK, out hbm);
            try
            {
                using (var bmp = Image.FromHbitmap(hbm))
                {
                    bmp.Save(output, ImageFormat.Png);
                }
            }
            finally { DeleteObject(hbm); }
            return true;
        }
        catch { return false; }
    }
}
'@
Add-Type -TypeDefinition $code -ReferencedAssemblies System.Drawing

$ok = $false
if (![string]::IsNullOrWhiteSpace($source) -and (Test-Path -LiteralPath $source)) {
    $ok = [SingleShellIconExtractor]::SaveIcon($source, $output, 256)
}
if (-not $ok -and ![string]::IsNullOrWhiteSpace($fallback) -and (Test-Path -LiteralPath $fallback)) {
    $ok = [SingleShellIconExtractor]::SaveIcon($fallback, $output, 256)
}
if ($ok) { 'OK' } else { 'FAIL' }
"""


POWERSHELL_METADATA_COLLECTOR = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
$outDir = '__OUT_DIR__'
$iconDir = Join-Path $outDir 'icons'
$includeUrlShortcuts = __INCLUDE_URL_SHORTCUTS__
New-Item -ItemType Directory -Force -Path $iconDir | Out-Null

function Convert-ToSafeFileName([string]$text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0,16)
    }
    finally { $sha.Dispose() }
}

function Resolve-IconPath([string]$iconLocation, [string]$targetPath, [string]$shortcutPath) {
    if (![string]::IsNullOrWhiteSpace($iconLocation)) {
        $raw = $iconLocation.Trim()
        $pathPart = $raw
        if ($raw -match '^(.*),(\-?\d+)\s*$') { $pathPart = $Matches[1] }
        $pathPart = $pathPart.Trim().Trim('"')
        if (![string]::IsNullOrWhiteSpace($pathPart)) {
            return [Environment]::ExpandEnvironmentVariables($pathPart)
        }
    }
    if (![string]::IsNullOrWhiteSpace($targetPath)) {
        return [Environment]::ExpandEnvironmentVariables($targetPath)
    }
    return $shortcutPath
}

function Read-UrlShortcut([string]$path) {
    $url = ''
    try {
        foreach ($line in [System.IO.File]::ReadLines($path)) {
            if ($line.StartsWith('URL=', [System.StringComparison]::OrdinalIgnoreCase)) {
                $url = $line.Substring(4)
                break
            }
        }
    } catch {}
    return $url
}

$scanRoots = @(
    [Environment]::GetFolderPath('CommonStartMenu'),
    [Environment]::GetFolderPath('StartMenu'),
    [Environment]::GetFolderPath('CommonDesktopDirectory'),
    [Environment]::GetFolderPath('DesktopDirectory')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$shell = New-Object -ComObject WScript.Shell
$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$items = New-Object System.Collections.Generic.List[object]

foreach ($scanRoot in $scanRoots) {
    $patterns = if ($includeUrlShortcuts) { @('*.lnk','*.url') } else { @('*.lnk') }
    Get-ChildItem -LiteralPath $scanRoot -Recurse -File -Include $patterns -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $name = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
            $target = ''
            $args = ''
            $workingDirectory = ''
            $iconLocation = ''

            if ($_.Extension -ieq '.lnk') {
                $sc = $shell.CreateShortcut($_.FullName)
                $target = $sc.TargetPath
                $args = $sc.Arguments
                $workingDirectory = $sc.WorkingDirectory
                $iconLocation = $sc.IconLocation
            } else {
                $target = Read-UrlShortcut $_.FullName
            }

            $dedupeKey = (($name + '|' + $target + '|' + $args + '|' + $_.FullName).ToLowerInvariant())
            if ($seen.Add($dedupeKey)) {
                $iconSource = Resolve-IconPath $iconLocation $target $_.FullName
                $hash = Convert-ToSafeFileName ($_.FullName + '|' + $target + '|' + $iconSource)
                $iconFile = Join-Path $iconDir ($hash + '.png')
                $items.Add([PSCustomObject]@{
                    AppName = $name
                    ShortcutPath = $_.FullName
                    TargetPath = $target
                    Arguments = $args
                    WorkingDirectory = $workingDirectory
                    IconSource = $iconSource
                    IconPng = $iconFile
                })
            }
        } catch {}
    }
}

$items | Sort-Object AppName | ConvertTo-Json -Depth 5 -Compress
"""


def shortcut_roots() -> list[Path]:
    roots = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu",
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
        Path.home() / "Desktop",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root or not root.exists():
            continue
        key = str(root).casefold()
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def read_url_shortcut(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.upper().startswith("URL="):
                return line[4:]
    except OSError:
        return ""
    return ""


def resolve_icon_source(icon_location: str, target_path: str, shortcut_path: str) -> str:
    raw = (icon_location or "").strip().strip('"')
    if raw:
        if "," in raw:
            raw = raw.rsplit(",", 1)[0].strip().strip('"')
        return os.path.expandvars(raw)
    if target_path:
        return os.path.expandvars(target_path)
    return shortcut_path


def safe_icon_file_name(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16] + ".png"


def collect_shortcuts(output_dir: Path, include_url_shortcuts: bool) -> list[dict[str, str]]:
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "This script needs pywin32 to read Windows .lnk shortcuts. "
            "Install it with: python -m pip install pywin32"
        ) from exc

    shell = win32com.client.Dispatch("WScript.Shell")
    icon_dir = output_dir / "icons"
    seen: set[str] = set()
    items: list[dict[str, str]] = []

    for root in shortcut_roots():
        patterns = ["*.lnk"]
        if include_url_shortcuts:
            patterns.append("*.url")
        for pattern in patterns:
            for shortcut in root.rglob(pattern):
                try:
                    name = shortcut.stem
                    target_path = ""
                    arguments = ""
                    working_directory = ""
                    icon_location = ""

                    if shortcut.suffix.lower() == ".lnk":
                        link = shell.CreateShortcut(str(shortcut))
                        target_path = str(link.TargetPath or "")
                        arguments = str(link.Arguments or "")
                        working_directory = str(link.WorkingDirectory or "")
                        icon_location = str(link.IconLocation or "")
                    else:
                        target_path = read_url_shortcut(shortcut)

                    dedupe_key = f"{name}|{target_path}|{arguments}|{shortcut}".casefold()
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)

                    icon_source = resolve_icon_source(icon_location, target_path, str(shortcut))
                    icon_png = str(icon_dir / safe_icon_file_name(f"{shortcut}|{target_path}|{icon_source}"))
                    items.append(
                        {
                            "AppName": name,
                            "ShortcutPath": str(shortcut),
                            "TargetPath": target_path,
                            "Arguments": arguments,
                            "WorkingDirectory": working_directory,
                            "IconSource": icon_source,
                            "IconPng": icon_png,
                        }
                    )
                except Exception:
                    continue

    items.sort(key=lambda item: item.get("AppName", "").casefold())
    return items


def ps_quote(value: str) -> str:
    return value.replace("'", "''")


def extract_one_icon(source: str, fallback: str, output: Path, timeout: int) -> bool:
    script = (
        POWERSHELL_ICON_EXTRACTOR
        .replace("__SOURCE__", ps_quote(source or ""))
        .replace("__FALLBACK__", ps_quote(fallback or ""))
        .replace("__OUTPUT__", ps_quote(str(output)))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass
    return completed.returncode == 0 and "OK" in completed.stdout and output.exists()


def extract_icons_batch(items: list[dict[str, str]]) -> None:
    payload = [
        {
            "source": item.get("IconSource", ""),
            "fallback": item.get("TargetPath", ""),
            "output": item.get("IconPng", ""),
        }
        for item in items
        if item.get("IconPng")
    ]
    if not payload:
        return

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8-sig") as json_file:
        json.dump(payload, json_file, ensure_ascii=False)
        json_path = Path(json_file.name)

    script = POWERSHELL_ICON_EXTRACTOR.replace("$source = '__SOURCE__'", "$source = ''")
    script = script.replace("$fallback = '__FALLBACK__'", "$fallback = ''")
    script = script.replace("$output = '__OUTPUT__'", "$output = ''")
    script = script.replace(
        "$ok = $false\n"
        "if (![string]::IsNullOrWhiteSpace($source) -and (Test-Path -LiteralPath $source)) {\n"
        "    $ok = [SingleShellIconExtractor]::SaveIcon($source, $output, 256)\n"
        "}\n"
        "if (-not $ok -and ![string]::IsNullOrWhiteSpace($fallback) -and (Test-Path -LiteralPath $fallback)) {\n"
        "    $ok = [SingleShellIconExtractor]::SaveIcon($fallback, $output, 256)\n"
        "}\n"
        "if ($ok) { 'OK' } else { 'FAIL' }",
        f"""
$items = Get-Content -LiteralPath '{ps_quote(str(json_path))}' -Raw -Encoding UTF8 | ConvertFrom-Json
$done = 0
foreach ($item in $items) {{
    $ok = $false
    if (![string]::IsNullOrWhiteSpace($item.source) -and (Test-Path -LiteralPath $item.source)) {{
        $ok = [SingleShellIconExtractor]::SaveIcon($item.source, $item.output, 256)
    }}
    if (-not $ok -and ![string]::IsNullOrWhiteSpace($item.fallback) -and (Test-Path -LiteralPath $item.fallback)) {{
        $ok = [SingleShellIconExtractor]::SaveIcon($item.fallback, $item.output, 256)
    }}
    $done++
    if (($done % 25) -eq 0 -or $done -eq $items.Count) {{ Write-Host "Extracted icons: $done/$($items.Count)" }}
}}
""",
    )

    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("Batch icon extraction timed out. Re-run with --icon-timeout 4 for safer per-icon extraction.")
    finally:
        for path in (json_path, script_path):
            try:
                path.unlink()
            except OSError:
                pass

    for item in items:
        icon_path = item.get("IconPng", "")
        if icon_path and not Path(icon_path).exists():
            item["IconPng"] = ""


def extract_icons(items: list[dict[str, str]], timeout_per_icon: int) -> None:
    if timeout_per_icon <= 0:
        if timeout_per_icon == 0:
            for item in items:
                item["IconPng"] = ""
        else:
            extract_icons_batch(items)
        return
    total = len(items)
    for index, item in enumerate(items, start=1):
        icon_path = item.get("IconPng", "")
        if not icon_path:
            continue
        output = Path(icon_path)
        source = item.get("IconSource", "")
        fallback = item.get("TargetPath", "")
        ok = extract_one_icon(source, fallback, output, timeout_per_icon)
        if not ok:
            item["IconPng"] = ""
            if output.exists():
                try:
                    output.unlink()
                except OSError:
                    pass
        if index % 25 == 0 or index == total:
            print(f"Extracted icons: {index}/{total}")


def write_csv(items: list[dict[str, str]], csv_path: Path) -> None:
    fieldnames = [
        "AppName",
        "ShortcutPath",
        "TargetPath",
        "Arguments",
        "WorkingDirectory",
        "IconSource",
        "IconPng",
        "IconData",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            row = {key: item.get(key, "") for key in fieldnames}
            row["IconData"] = icon_data_url(item.get("IconPng", ""))
            writer.writerow(row)


def icon_data_url(icon_path: str) -> str:
    if not icon_path:
        return ""
    path = Path(icon_path)
    if not path.exists():
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def js_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def build_rows(items: list[dict[str, str]], output_dir: Path) -> str:
    rows: list[str] = []
    for index, item in enumerate(items):
        app_name = item.get("AppName", "")
        target_path = item.get("TargetPath", "")
        arguments = item.get("Arguments", "")
        shortcut_path = item.get("ShortcutPath", "")
        working_dir = item.get("WorkingDirectory", "")
        icon_source = item.get("IconSource", "")
        icon_png = item.get("IconPng", "")
        icon_file_name = Path(icon_png).name if icon_png else ""
        icon_src = f"icons/{icon_file_name}" if icon_file_name else ""
        icon_data = icon_data_url(icon_png)
        image_src = icon_data or icon_src
        image_html = f'<img src="{html.escape(image_src)}" alt="">' if image_src else ""

        rows.append(
            f"""<tr data-index="{index}"
    data-app-name={js_string(app_name)}
    data-target-path={js_string(target_path)}
    data-arguments={js_string(arguments)}
    data-shortcut-path={js_string(shortcut_path)}
    data-working-directory={js_string(working_dir)}
    data-icon-source={js_string(icon_source)}
    data-icon-file-name={js_string(icon_file_name)}
    data-icon-png={js_string(icon_png)}
    data-icon-data={js_string(icon_data)}>
<td class="select"><input type="checkbox" class="rowcheck" aria-label="選擇 {html.escape(app_name)}"></td>
<td class="icon">{image_html}</td>
<td class="name">{html.escape(app_name)}</td>
<td><code>{html.escape(target_path)}</code></td>
<td><code>{html.escape(arguments)}</code></td>
<td><code>{html.escape(shortcut_path)}</code></td>
<td><code>{html.escape(working_dir)}</code></td>
<td><code>{html.escape(icon_source)}</code></td>
<td><code>{html.escape(icon_png)}</code></td>
</tr>"""
        )
    return "\n".join(rows)


def build_html(items: list[dict[str, str]], output_dir: Path) -> str:
    rows = build_rows(items, output_dir)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = len(items)
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>已安裝 APP 圖示與連結清單</title>
<style>
:root{{font-family:"Microsoft JhengHei UI","Segoe UI",Arial,sans-serif;color:#1f2937;background:#f6f7f9}}body{{margin:0;padding:24px}}.wrap{{max-width:1680px;margin:0 auto}}h1{{font-size:28px;margin:0 0 8px}}.meta{{color:#5b6472;margin:0 0 20px}}.tools{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 16px}}input[type="search"]{{width:min(560px,100%);font-size:15px;padding:10px 12px;border:1px solid #c9ced6;border-radius:8px;background:white}}button,.buttonlink{{font:inherit;font-size:14px;padding:10px 12px;border:1px solid #c9ced6;border-radius:8px;background:white;color:#1f2937;text-decoration:none;cursor:pointer}}button.primary{{background:#0f766e;border-color:#0f766e;color:white}}button:disabled{{opacity:.55;cursor:not-allowed}}a{{color:#075985}}.tablebox{{overflow:auto;background:white;border:1px solid #d9dde5;border-radius:8px}}table{{border-collapse:collapse;width:100%;min-width:1520px}}th,td{{border-bottom:1px solid #e5e7eb;padding:10px 12px;text-align:left;vertical-align:middle}}th{{position:sticky;top:0;background:#eef2f7;font-size:13px;color:#374151;z-index:1}}tr:hover{{background:#f8fafc}}tr.selected{{background:#ecfdf5}}.select{{width:46px;text-align:center}}.select input{{width:18px;height:18px}}.icon{{width:64px;text-align:center}}.icon img{{width:48px;height:48px;object-fit:contain}}.name{{font-weight:700;min-width:180px}}code{{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;color:#111827}}.hint,.status{{font-size:13px;color:#687386;margin-top:12px}}.status strong{{color:#0f766e}}.stickybar{{position:sticky;top:0;background:#f6f7f9;padding-top:2px;z-index:3}}
</style>
</head>
<body>
<div class="wrap">
<h1>已安裝 APP 圖示與連結清單</h1>
<p class="meta">產生時間：{html.escape(now)}　項目數：{count}　來源：開始功能表與桌面捷徑</p>
<div class="stickybar">
<div class="tools">
<input id="q" type="search" placeholder="搜尋 APP 名稱、程式位置、捷徑位置...">
<button id="selectVisible" type="button">勾選目前搜尋結果</button>
<button id="clearVisible" type="button">取消目前搜尋結果</button>
<button id="clearAll" type="button">全部取消</button>
<button id="exportSelected" class="primary" type="button">輸出已勾選</button>
<a class="buttonlink" href="installed_apps_icons.csv">下載完整 CSV</a>
</div>
<div class="status" id="status">已勾選 <strong>0</strong> 個 APP。</div>
</div>
<div class="tablebox"><table id="apps"><thead><tr><th class="select">選取</th><th>Icon</th><th>APP</th><th>執行連結位置</th><th>執行參數</th><th>捷徑位置</th><th>工作資料夾</th><th>Icon 來源</th><th>Icon PNG 位置</th></tr></thead><tbody>
{rows}
</tbody></table></div>
<p class="hint">按「輸出已勾選」後，若瀏覽器支援本機資料夾寫入，請選擇輸出位置；系統會建立 selected-apps-output 資料夾，裡面包含新的 HTML 清單、CSV，以及已勾選 APP 的 icons 子資料夾。若瀏覽器不支援，會改成下載 HTML 與 CSV，icon 會內嵌在 HTML 清單中。</p>
</div>
<script>
const searchBox = document.getElementById('q');
const rows = [...document.querySelectorAll('#apps tbody tr')];
const statusEl = document.getElementById('status');
const exportButton = document.getElementById('exportSelected');

function rowData(row) {{
  const data = row.dataset;
  return {{
    appName: data.appName || '',
    targetPath: data.targetPath || '',
    arguments: data.arguments || '',
    shortcutPath: data.shortcutPath || '',
    workingDirectory: data.workingDirectory || '',
    iconSource: data.iconSource || '',
    iconFileName: data.iconFileName || '',
    iconPng: data.iconPng || '',
    iconData: data.iconData || ''
  }};
}}

function visibleRows() {{
  return rows.filter(row => row.style.display !== 'none');
}}

function selectedRows() {{
  return rows.filter(row => row.querySelector('.rowcheck').checked);
}}

function refreshStatus() {{
  const selected = selectedRows().length;
  statusEl.innerHTML = `已勾選 <strong>${{selected}}</strong> 個 APP。`;
  exportButton.disabled = selected === 0;
  rows.forEach(row => row.classList.toggle('selected', row.querySelector('.rowcheck').checked));
}}

searchBox.addEventListener('input', () => {{
  const text = searchBox.value.toLowerCase();
  rows.forEach(row => {{
    row.style.display = row.innerText.toLowerCase().includes(text) ? '' : 'none';
  }});
}});

document.addEventListener('change', event => {{
  if (event.target.classList.contains('rowcheck')) refreshStatus();
}});

document.getElementById('selectVisible').addEventListener('click', () => {{
  visibleRows().forEach(row => row.querySelector('.rowcheck').checked = true);
  refreshStatus();
}});

document.getElementById('clearVisible').addEventListener('click', () => {{
  visibleRows().forEach(row => row.querySelector('.rowcheck').checked = false);
  refreshStatus();
}});

document.getElementById('clearAll').addEventListener('click', () => {{
  rows.forEach(row => row.querySelector('.rowcheck').checked = false);
  refreshStatus();
}});

function csvEscape(value) {{
  return `"${{String(value ?? '').replaceAll('"', '""')}}"`;
}}

function buildCsv(items) {{
  const headers = ['AppName','TargetPath','Arguments','ShortcutPath','WorkingDirectory','IconSource','IconFileName','OriginalIconPng','IconData'];
  const lines = [headers.map(csvEscape).join(',')];
  for (const item of items) {{
    lines.push([
      item.appName,
      item.targetPath,
      item.arguments,
      item.shortcutPath,
      item.workingDirectory,
      item.iconSource,
      item.iconFileName,
      item.iconPng,
      item.iconData
    ].map(csvEscape).join(','));
  }}
  return '\\uFEFF' + lines.join('\\r\\n');
}}

function htmlEscape(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}

function buildSelectedHtml(items, iconMode) {{
  const date = new Date().toLocaleString();
  const bodyRows = items.map(item => {{
    const src = iconMode === 'folder' && item.iconFileName ? `icons/${{htmlEscape(item.iconFileName)}}` : item.iconData;
    return `<tr><td class="icon">${{src ? `<img src="${{src}}" alt="">` : ''}}</td><td class="name">${{htmlEscape(item.appName)}}</td><td><code>${{htmlEscape(item.targetPath)}}</code></td><td><code>${{htmlEscape(item.arguments)}}</code></td><td><code>${{htmlEscape(item.shortcutPath)}}</code></td><td><code>${{htmlEscape(item.workingDirectory)}}</code></td><td><code>${{htmlEscape(item.iconSource)}}</code></td><td><code>${{htmlEscape(iconMode === 'folder' && item.iconFileName ? 'icons/' + item.iconFileName : item.iconPng)}}</code></td></tr>`;
  }}).join('\\n');
  return `<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>已勾選 APP 圖示與連結清單</title>
<style>:root{{font-family:"Microsoft JhengHei UI","Segoe UI",Arial,sans-serif;color:#1f2937;background:#f6f7f9}}body{{margin:0;padding:24px}}.wrap{{max-width:1500px;margin:0 auto}}h1{{font-size:28px;margin:0 0 8px}}.meta{{color:#5b6472;margin:0 0 20px}}.tablebox{{overflow:auto;background:white;border:1px solid #d9dde5;border-radius:8px}}table{{border-collapse:collapse;width:100%;min-width:1320px}}th,td{{border-bottom:1px solid #e5e7eb;padding:10px 12px;text-align:left;vertical-align:middle}}th{{position:sticky;top:0;background:#eef2f7;font-size:13px;color:#374151}}.icon{{width:64px;text-align:center}}.icon img{{width:48px;height:48px;object-fit:contain}}.name{{font-weight:700;min-width:180px}}code{{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-all;color:#111827}}</style>
</head><body><div class="wrap"><h1>已勾選 APP 圖示與連結清單</h1><p class="meta">輸出時間：${{htmlEscape(date)}}　項目數：${{items.length}}</p><div class="tablebox"><table><thead><tr><th>Icon</th><th>APP</th><th>執行連結位置</th><th>執行參數</th><th>捷徑位置</th><th>工作資料夾</th><th>Icon 來源</th><th>Icon 檔案</th></tr></thead><tbody>${{bodyRows}}</tbody></table></div></div></body></html>`;
}}

function dataUrlToBytes(dataUrl) {{
  const base64 = dataUrl.split(',')[1] || '';
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}}

async function writeTextFile(dirHandle, name, text, type) {{
  const fileHandle = await dirHandle.getFileHandle(name, {{ create: true }});
  const writable = await fileHandle.createWritable();
  await writable.write(new Blob([text], {{ type }}));
  await writable.close();
}}

function downloadFile(name, content, type) {{
  const blob = new Blob([content], {{ type }});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}}

document.getElementById('exportSelected').addEventListener('click', async () => {{
  const items = selectedRows().map(rowData);
  if (!items.length) return;
  exportButton.disabled = true;
  try {{
    if ('showDirectoryPicker' in window) {{
      const root = await window.showDirectoryPicker({{ mode: 'readwrite' }});
      const output = await root.getDirectoryHandle('selected-apps-output', {{ create: true }});
      const icons = await output.getDirectoryHandle('icons', {{ create: true }});
      await writeTextFile(output, 'selected_apps_icons.csv', buildCsv(items), 'text/csv;charset=utf-8');
      await writeTextFile(output, 'selected_apps_icons_report.html', buildSelectedHtml(items, 'folder'), 'text/html;charset=utf-8');
      for (const item of items) {{
        if (!item.iconFileName || !item.iconData) continue;
        const fileHandle = await icons.getFileHandle(item.iconFileName, {{ create: true }});
        const writable = await fileHandle.createWritable();
        await writable.write(new Blob([dataUrlToBytes(item.iconData)], {{ type: 'image/png' }}));
        await writable.close();
      }}
      statusEl.innerHTML = `完成輸出 <strong>${{items.length}}</strong> 個 APP：selected-apps-output 資料夾已建立。`;
    }} else {{
      downloadFile('selected_apps_icons.csv', buildCsv(items), 'text/csv;charset=utf-8');
      downloadFile('selected_apps_icons_report.html', buildSelectedHtml(items, 'embedded'), 'text/html;charset=utf-8');
      statusEl.innerHTML = `瀏覽器不支援直接建立資料夾，已下載 <strong>${{items.length}}</strong> 個 APP 的 HTML/CSV；HTML 已內嵌 icon。`;
    }}
  }} catch (error) {{
    if (error && error.name === 'AbortError') {{
      statusEl.innerHTML = `已取消輸出，仍勾選 <strong>${{items.length}}</strong> 個 APP。`;
    }} else {{
      statusEl.textContent = '輸出失敗：' + (error && error.message ? error.message : error);
    }}
  }} finally {{
    refreshStatus();
  }}
}});

refreshStatus();
</script>
</body>
</html>
"""


def build_report(
    output_dir: Path,
    open_report: bool,
    include_url_shortcuts: bool,
    timeout_per_icon: int,
) -> tuple[Path, Path, int]:
    if output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
    icons_dir = output_dir / "icons"
    if icons_dir.exists():
        shutil.rmtree(icons_dir)
    icons_dir.mkdir(parents=True, exist_ok=True)

    items = collect_shortcuts(output_dir, include_url_shortcuts)
    extract_icons(items, timeout_per_icon)

    csv_path = output_dir / "installed_apps_icons.csv"
    html_path = output_dir / "installed_apps_icons_report.html"
    write_csv(items, csv_path)
    html_path.write_text(build_html(items, output_dir), encoding="utf-8-sig")

    if open_report:
        os.startfile(html_path)  # type: ignore[attr-defined]
    return html_path, csv_path, len(items)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a selectable report of Windows app shortcuts, icon files, and launch paths."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output folder. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the generated HTML report automatically.",
    )
    parser.add_argument(
        "--include-url-shortcuts",
        action="store_true",
        help="Also scan .url web shortcuts. Disabled by default because some web shortcut icons can be slow.",
    )
    parser.add_argument(
        "--icon-timeout",
        type=int,
        default=-1,
        help="Seconds to wait for each icon extraction before skipping it. Default: -1 uses faster batch extraction. Use 0 to skip icons.",
    )
    return parser.parse_args()


def main() -> int:
    if os.name != "nt":
        print("This script is designed for Windows.", file=sys.stderr)
        return 1

    args = parse_args()
    output_dir = args.output.resolve()
    print(f"Scanning app shortcuts and extracting icons into: {output_dir}")
    try:
        html_path, csv_path, count = build_report(
            output_dir,
            open_report=not args.no_open,
            include_url_shortcuts=args.include_url_shortcuts,
            timeout_per_icon=args.icon_timeout,
        )
    except subprocess.CalledProcessError as exc:
        print("PowerShell collection failed.", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode or 1

    print(f"Done. Found {count} app entries.")
    print(f"HTML report: {html_path}")
    print(f"CSV list:    {csv_path}")
    print(f"Icons:       {output_dir / 'icons'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
