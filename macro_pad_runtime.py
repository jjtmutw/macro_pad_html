#!/usr/bin/env python3
"""PC-side runtime for the phone macro pad PWA.

Responsibilities:
- Read the saved layout JSON.
- Publish the layout to MQTT for the phone PWA.
- Receive button actions from the phone and execute them on Windows.
- Serve the static PWA/config files and icon assets over HTTP.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import ctypes
from ctypes import wintypes
import json
import os
import platform
import shlex
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
import uuid
import winreg
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return runtime_root()


ROOT = runtime_root()
BUNDLED_ROOT = bundled_root()
DEFAULT_BASE_TOPIC = "jj/notebook1/macro_pad"
DEFAULT_LAYOUT = ROOT / "macro_pad_layout.json"
if not DEFAULT_LAYOUT.exists():
    DEFAULT_LAYOUT = BUNDLED_ROOT / "macro_pad_layout.json"
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_STARTUP_NAME = "MacroPadRuntime"
DEFAULT_MUSIC_DIR = Path.home() / "Music"
DEFAULT_VIDEO_DIR = Path.home() / "Videos"


def static_root() -> Path:
    """Prefer editable files next to the exe, then fall back to bundled assets."""
    if (ROOT / "config.html").exists() and (ROOT / "pwa").exists():
        return ROOT
    return BUNDLED_ROOT

MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
}

WM_APPCOMMAND = 0x0319
HWND_BROADCAST = 0xFFFF
APPCOMMANDS = {
    "next": 11,
    "previous": 12,
    "stop": 13,
    "play_pause": 14,
    "mute": 8,
    "volume_down": 9,
    "volume_up": 10,
}

KEY_ALIASES = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "cmd": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "+": 0xBB,
    "plus": 0xBB,
    "-": 0xBD,
    "minus": 0xBD,
}

for index in range(1, 25):
    KEY_ALIASES[f"f{index}"] = 0x70 + index - 1
for char in "abcdefghijklmnopqrstuvwxyz":
    KEY_ALIASES[char] = ord(char.upper())
for char in "0123456789":
    KEY_ALIASES[char] = ord(char)

MODIFIER_KEYS = {0x10, 0x11, 0x12, 0x5B}

OFFICE_ICON_TARGETS = {
    "wordicon.exe": "winword.exe",
    "xlicons.exe": "excel.exe",
    "pptico.exe": "powerpnt.exe",
}

ERROR_ALREADY_EXISTS = 183
_single_instance_mutex: int | None = None
_current_media_kind = "mp3"
_current_music_filename = ""
_current_video_filename = ""
_http_port = 8080


def load_layout(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def layout_base_topic(layout: dict[str, Any]) -> str:
    mqtt_settings = layout.get("mqtt")
    if isinstance(mqtt_settings, dict):
        value = mqtt_settings.get("baseTopic") or mqtt_settings.get("base_topic")
        if value:
            return str(value).strip()
    for key in ("mqttBaseTopic", "baseTopic", "base_topic"):
        value = layout.get(key)
        if value:
            return str(value).strip()
    return ""


def startup_executable() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve())]


def startup_command(args: argparse.Namespace, layout_path: Path, explicit_base_topic: str | None) -> str:
    command = [
        *startup_executable(),
        "--layout",
        str(layout_path),
        "--mqtt-host",
        str(args.mqtt_host),
        "--mqtt-port",
        str(args.mqtt_port),
        "--mqtt-transport",
        str(args.mqtt_transport),
        "--mqtt-websocket-path",
        str(args.mqtt_websocket_path),
        "--http-host",
        str(args.http_host),
        "--http-port",
        str(args.http_port),
    ]
    if not args.mqtt_tls:
        command.append("--no-mqtt-tls")
    if args.mqtt_user:
        command.extend(["--mqtt-user", str(args.mqtt_user)])
    if args.mqtt_password:
        command.extend(["--mqtt-password", str(args.mqtt_password)])
    if explicit_base_topic:
        command.extend(["--base-topic", explicit_base_topic])
    if args.no_http:
        command.append("--no-http")
    if args.allow_shell:
        command.append("--allow-shell")
    return subprocess.list2cmdline(command)


def install_startup(name: str, command: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


def uninstall_startup(name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return False


def startup_status(name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except FileNotFoundError:
        return ""


def normalize_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if isinstance(action, dict):
        normalized = dict(action)
    else:
        normalized = dict(payload)

    for key in ("targetPath", "path", "arguments", "workingDirectory", "command", "keys", "text", "filename"):
        if not normalized.get(key) and payload.get(key):
            normalized[key] = payload[key]
    for key in ("id", "label", "page", "slot"):
        if payload.get(key) is not None:
            normalized[f"_{key}"] = payload[key]
    return normalized


def action_context(action: dict[str, Any]) -> str:
    details = []
    for label, key in (("label", "_label"), ("page", "_page"), ("slot", "_slot"), ("id", "_id")):
        value = action.get(key)
        if value not in (None, ""):
            details.append(f"{label}={value!r}")
    return f" ({', '.join(details)})" if details else ""


def app_path_from_registry(exe_name: str) -> str:
    subkeys = (
        fr"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        fr"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
    )
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in subkeys:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        return os.path.expandvars(str(value))
            except OSError:
                continue
    return ""


def resolve_launch_target(target: str) -> str:
    expanded = os.path.expandvars(str(target))
    office_exe = OFFICE_ICON_TARGETS.get(Path(expanded).name.casefold())
    if not office_exe:
        return expanded

    resolved = app_path_from_registry(office_exe)
    if resolved:
        return resolved

    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if not base:
            continue
        candidate = Path(base) / "Microsoft Office" / "Office16" / office_exe.upper()
        if candidate.exists():
            return str(candidate)
    return expanded


def run_as_admin(target: str, args: str, cwd: str | None) -> None:
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        target,
        str(args or None),
        cwd or None,
        1,
    )
    if result <= 32:
        raise OSError(f"administrator launch failed with ShellExecute code {result}")


def run_launch(action: dict[str, Any]) -> None:
    target = action.get("targetPath") or action.get("path")
    if not target:
        raise ValueError(f"launch action missing targetPath{action_context(action)}")

    args = action.get("arguments") or ""
    cwd = action.get("workingDirectory") or None
    target = resolve_launch_target(str(target))

    try:
        if args:
            subprocess.Popen([target, *shlex.split(str(args), posix=False)], cwd=cwd or None)
        else:
            os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        if getattr(exc, "winerror", None) != 740:
            raise
        run_as_admin(target, str(args or ""), cwd)


def keybd(vk: int, key_up: bool = False) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 2 if key_up else 0, 0)


def key_spec(key: str) -> tuple[int, list[int]]:
    value = str(key).strip().lower()
    if not value:
        raise ValueError("empty hotkey key")
    if value in KEY_ALIASES:
        return KEY_ALIASES[value], []

    scan = ctypes.windll.user32.VkKeyScanW(value[0])
    if scan == -1:
        raise ValueError(f"unsupported hotkey key: {key}")

    vk = scan & 0xFF
    shift_state = (scan >> 8) & 0xFF
    modifiers = []
    if shift_state & 1:
        modifiers.append(KEY_ALIASES["shift"])
    if shift_state & 2:
        modifiers.append(KEY_ALIASES["ctrl"])
    if shift_state & 4:
        modifiers.append(KEY_ALIASES["alt"])
    return vk, modifiers


def press_virtual_key(vk: int, modifiers: list[int] | None = None) -> None:
    modifiers = modifiers or []
    for modifier in modifiers:
        keybd(modifier)
    keybd(vk)
    keybd(vk, key_up=True)
    for modifier in reversed(modifiers):
        keybd(modifier, key_up=True)


def run_media(action: dict[str, Any]) -> None:
    command = str(action.get("command") or "")
    vk = MEDIA_KEYS.get(command)
    app_command = APPCOMMANDS.get(command)
    if not vk and app_command is None:
        raise ValueError(f"unknown media command: {command}")
    if command in {"next", "previous"} and play_adjacent_media(command):
        return
    if command in {"next", "previous"} and app_command is not None:
        send_app_command(app_command)
        return
    press_virtual_key(vk)


def send_app_command(command: int) -> None:
    ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_APPCOMMAND, 0, command << 16)


def media_dir(kind: str) -> Path:
    return DEFAULT_VIDEO_DIR if kind == "mp4" else DEFAULT_MUSIC_DIR


def media_extension(kind: str) -> str:
    return ".mp4" if kind == "mp4" else ".mp3"


def media_files(kind: str, directory: Path | None = None) -> list[str]:
    directory = directory or media_dir(kind)
    extension = media_extension(kind)
    if not directory.exists():
        return []
    return sorted(
        (path.name for path in directory.iterdir() if path.is_file() and path.suffix.casefold() == extension),
        key=str.casefold,
    )


def music_files(music_dir: Path = DEFAULT_MUSIC_DIR) -> list[str]:
    return media_files("mp3", music_dir)


def video_files(video_dir: Path = DEFAULT_VIDEO_DIR) -> list[str]:
    return media_files("mp4", video_dir)


def resolve_media_file(kind: str, filename: str) -> Path:
    requested = Path(str(filename)).name
    if not requested:
        raise ValueError(f"{kind}_play action missing filename")
    directory = media_dir(kind)
    for name in media_files(kind, directory):
        if name.casefold() == requested.casefold():
            return directory / name
    raise FileNotFoundError(f"{media_extension(kind)} not found in {directory}: {requested}")


def resolve_music_file(filename: str, music_dir: Path = DEFAULT_MUSIC_DIR) -> Path:
    requested = Path(str(filename)).name
    if not requested:
        raise ValueError("music_play action missing filename")
    for name in music_files(music_dir):
        if name.casefold() == requested.casefold():
            return music_dir / name
    raise FileNotFoundError(f"mp3 not found in Music folder: {requested}")


def open_music_file(path: Path) -> None:
    global _current_media_kind, _current_music_filename
    _current_media_kind = "mp3"
    _current_music_filename = path.name
    os.startfile(str(path))


def open_video_file(path: Path) -> None:
    global _current_media_kind, _current_video_filename
    _current_media_kind = "mp4"
    _current_video_filename = path.name
    open_pc_video_player(path.name)


def run_music_play(action: dict[str, Any]) -> None:
    path = resolve_music_file(str(action.get("filename") or action.get("path") or ""))
    open_music_file(path)


def run_video_play(action: dict[str, Any]) -> None:
    path = resolve_media_file("mp4", str(action.get("filename") or action.get("path") or ""))
    open_video_file(path)


def play_adjacent_media(command: str) -> bool:
    if command not in {"next", "previous"}:
        return False
    kind = _current_media_kind
    current_filename = _current_video_filename if kind == "mp4" else _current_music_filename
    if not current_filename:
        return False
    files = media_files(kind)
    if not files:
        return False
    current = current_filename.casefold()
    try:
        index = next(i for i, name in enumerate(files) if name.casefold() == current)
    except StopIteration:
        return False
    offset = 1 if command == "next" else -1
    next_path = media_dir(kind) / files[(index + offset) % len(files)]
    if kind == "mp4":
        open_video_file(next_path)
    else:
        open_music_file(next_path)
    return True


def play_next_video_after(filename: str) -> str:
    global _current_media_kind, _current_video_filename
    files = video_files()
    current = Path(filename).name.casefold()
    try:
        index = next(i for i, name in enumerate(files) if name.casefold() == current)
    except StopIteration:
        return ""
    next_index = index + 1
    if next_index >= len(files):
        return ""
    next_name = files[next_index]
    _current_media_kind = "mp4"
    _current_video_filename = next_name
    return next_name


def open_pc_video_player(filename: str) -> None:
    url = f"http://127.0.0.1:{_http_port}/pc_video_player.html?file={quote(filename)}"
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for browser in candidates:
        if Path(browser).exists():
            subprocess.Popen([browser, "--start-fullscreen", "--autoplay-policy=no-user-gesture-required", url])
            return
    os.startfile(url)


def run_hotkey(action: dict[str, Any]) -> None:
    keys = action.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("hotkey action missing keys")

    modifiers: list[int] = []
    normal_keys: list[int] = []
    for key in keys:
        vk, implied_modifiers = key_spec(str(key))
        for modifier in implied_modifiers:
            if modifier not in modifiers:
                modifiers.append(modifier)
        if vk in MODIFIER_KEYS:
            if vk not in modifiers:
                modifiers.append(vk)
        else:
            normal_keys.append(vk)

    for modifier in modifiers:
        keybd(modifier)
    for vk in normal_keys:
        keybd(vk)
        keybd(vk, key_up=True)
    for modifier in reversed(modifiers):
        keybd(modifier, key_up=True)


def run_text(action: dict[str, Any]) -> None:
    text = str(action.get("text") or "")
    interval = float(action.get("interval") or 0)
    for char in text:
        vk, modifiers = key_spec(char)
        press_virtual_key(vk, modifiers)
        if interval > 0:
            time.sleep(interval)


def execute_action(action: dict[str, Any], allow_shell: bool) -> None:
    action_type = str(action.get("type") or "")
    if action_type == "launch":
        run_launch(action)
    elif action_type == "media":
        run_media(action)
    elif action_type == "music_play":
        run_music_play(action)
    elif action_type == "video_play":
        run_video_play(action)
    elif action_type == "hotkey":
        run_hotkey(action)
    elif action_type == "text":
        run_text(action)
    elif action_type == "shell" and allow_shell:
        subprocess.Popen(str(action.get("command") or ""), shell=True)
    else:
        raise ValueError(f"unsupported or disabled action type: {action_type}")


def guess_content_type(path: Path) -> str:
    if path.suffix.casefold() == ".mp4":
        return "video/mp4"
    return "application/octet-stream"


def start_http_server(host: str, port: int) -> ThreadingHTTPServer:
    global _http_port
    _http_port = port
    static_dir = static_root()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/pc_video_player.html":
                self.serve_pc_video_player()
                return
            if parsed.path.startswith("/media/mp4/"):
                filename = unquote(parsed.path.removeprefix("/media/mp4/"))
                self.serve_media_file("mp4", filename)
                return
            if parsed.path == "/media/video-ended":
                filename = parse_qs(parsed.query).get("file", [""])[0]
                next_name = play_next_video_after(filename)
                payload = json.dumps({"next": next_name}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            super().do_GET()

        def serve_pc_video_player(self) -> None:
            html = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PC MP4 Player</title>
  <style>
    html, body { margin: 0; width: 100%; height: 100%; background: #000; overflow: hidden; }
    video { width: 100vw; height: 100vh; object-fit: contain; background: #000; display: block; }
  </style>
</head>
<body>
  <video id="player" autoplay controls playsinline></video>
  <script>
    const params = new URLSearchParams(location.search);
    const file = params.get('file') || '';
    const player = document.getElementById('player');
    player.src = '/media/mp4/' + encodeURIComponent(file);
    player.addEventListener('loadedmetadata', async () => {
      try { await document.documentElement.requestFullscreen({ navigationUI: 'hide' }); } catch {}
      try { await player.play(); } catch {}
    });
    player.addEventListener('ended', async () => {
      const res = await fetch('/media/video-ended?file=' + encodeURIComponent(file));
      const data = await res.json();
      if (data.next) location.replace('/pc_video_player.html?file=' + encodeURIComponent(data.next));
      else window.close();
    });
  </script>
</body>
</html>"""
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def serve_media_file(self, kind: str, filename: str) -> None:
            try:
                path = resolve_media_file(kind, filename)
            except Exception:
                self.send_error(404)
                return
            size = path.stat().st_size
            range_header = self.headers.get("Range", "")
            start = 0
            end = size - 1
            status = 200
            if range_header.startswith("bytes="):
                status = 206
                start_text, _, end_text = range_header.removeprefix("bytes=").partition("-")
                start = int(start_text or 0)
                end = int(end_text or end)
                end = min(end, size - 1)
            if start > end or start >= size:
                self.send_error(416)
                return
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", guess_content_type(path))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as file:
                file.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def default_client_id() -> str:
    host = platform.node().strip().lower() or "pc"
    safe_host = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in host)[:24].strip("-") or "pc"
    suffix = uuid.uuid4().hex[:8]
    return f"macro-pad-{safe_host}-{suffix}"


def action_dedupe_key(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "id": payload.get("id"),
            "page": payload.get("page"),
            "slot": payload.get("slot"),
            "at": payload.get("at"),
            "action": payload.get("action"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def acquire_single_instance_lock() -> None:
    global _single_instance_mutex
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex_name = "Global\\MacroPadRuntimeSingleton"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError("failed to create single-instance mutex")
    if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError("macro_pad_runtime is already running. Please close the existing instance first.")
    _single_instance_mutex = handle


def release_single_instance_lock() -> None:
    global _single_instance_mutex
    if _single_instance_mutex:
        ctypes.windll.kernel32.CloseHandle(_single_instance_mutex)
        _single_instance_mutex = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the PC-side macro pad MQTT bridge.")
    parser.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    parser.add_argument("--mqtt-host", default="broker.emqx.io")
    parser.add_argument("--mqtt-port", type=int, default=8084)
    parser.add_argument("--mqtt-transport", choices=["tcp", "websockets"], default="websockets")
    parser.add_argument("--mqtt-websocket-path", default="/mqtt")
    parser.add_argument("--mqtt-tls", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mqtt-user", default="")
    parser.add_argument("--mqtt-password", default="")
    parser.add_argument(
        "--base-topic",
        default=None,
        help=f"MQTT base topic. Defaults to mqtt.baseTopic in the layout JSON, then {DEFAULT_BASE_TOPIC}.",
    )
    parser.add_argument("--client-id", default=default_client_id())
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--no-http", action="store_true", help="Disable the optional local static web server.")
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--startup-name", default=DEFAULT_STARTUP_NAME, help="Windows startup entry name.")
    parser.add_argument("--install-startup", action="store_true", help="Install this runtime to auto-start when Windows signs in.")
    parser.add_argument("--uninstall-startup", action="store_true", help="Remove this runtime from Windows auto-start.")
    parser.add_argument("--startup-status", action="store_true", help="Print the Windows auto-start command, if installed.")
    args = parser.parse_args()

    layout_path = args.layout.resolve()
    startup_layout = load_layout(layout_path)
    explicit_base_topic = args.base_topic.strip().strip("/") if args.base_topic else None
    args.base_topic = (explicit_base_topic or layout_base_topic(startup_layout) or DEFAULT_BASE_TOPIC).strip().strip("/")

    if args.install_startup:
        command = startup_command(args, layout_path, explicit_base_topic)
        install_startup(args.startup_name, command)
        print(f"Installed Windows startup entry: {args.startup_name}")
        print(command)
        return 0
    if args.uninstall_startup:
        removed = uninstall_startup(args.startup_name)
        print(f"{'Removed' if removed else 'No existing'} Windows startup entry: {args.startup_name}")
        return 0
    if args.startup_status:
        command = startup_status(args.startup_name)
        if command:
            print(f"Windows startup entry: {args.startup_name}")
            print(command)
        else:
            print(f"Windows startup entry is not installed: {args.startup_name}")
        return 0

    try:
        acquire_single_instance_lock()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    http_server = None if args.no_http else start_http_server(args.http_host, args.http_port)
    if http_server:
        print(f"PWA/config server: http://{args.http_host}:{args.http_port}/pwa/")
        print(f"Config editor:      http://{args.http_host}:{args.http_port}/config.html")
        print(f"Static files:       {static_root()}")
    print(f"Layout file:        {layout_path}")

    layout_topic = f"{args.base_topic}/layout"
    action_topic = f"{args.base_topic}/action"
    hello_topic = f"{args.base_topic}/hello"
    status_topic = f"{args.base_topic}/status"
    music_request_topic = f"{args.base_topic}/music/request"
    music_list_topic = f"{args.base_topic}/music/list"
    music_current_topic = f"{args.base_topic}/music/current"
    media_request_topic = f"{args.base_topic}/media/request"
    media_list_topic = f"{args.base_topic}/media/list"
    media_current_topic = f"{args.base_topic}/media/current"
    recent_actions: OrderedDict[str, float] = OrderedDict()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        transport=args.mqtt_transport,
    )
    if args.mqtt_transport == "websockets":
        client.ws_set_options(path=args.mqtt_websocket_path)
    if args.mqtt_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set(False)
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)

    def publish_layout() -> None:
        layout = load_layout(layout_path)
        layout.setdefault("mqtt", {})["baseTopic"] = args.base_topic
        client.publish(layout_topic, json.dumps(layout, ensure_ascii=False), qos=1, retain=True)
        print(f"Published layout to {layout_topic}")

    def current_filename(kind: str) -> str:
        return _current_video_filename if kind == "mp4" else _current_music_filename

    def publish_media_list(kind: str = "mp3") -> None:
        kind = "mp4" if kind == "mp4" else "mp3"
        payload = {
            "kind": kind,
            "directory": str(media_dir(kind)),
            "files": media_files(kind),
            "current": current_filename(kind),
            "at": int(time.time() * 1000),
        }
        client.publish(media_list_topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=False)
        if kind == "mp3":
            client.publish(music_list_topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=False)
        print(f"Published {kind} list to {media_list_topic}: {len(payload['files'])} files")

    def publish_music_list() -> None:
        publish_media_list("mp3")

    def publish_current_media() -> None:
        filename = current_filename(_current_media_kind)
        if not filename:
            return
        payload = {
            "kind": _current_media_kind,
            "directory": str(media_dir(_current_media_kind)),
            "filename": filename,
            "at": int(time.time() * 1000),
        }
        client.publish(media_current_topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=True)
        if _current_media_kind == "mp3":
            client.publish(music_current_topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=True)
        print(f"Published current {_current_media_kind} to {media_current_topic}: {filename}")

    def publish_current_music() -> None:
        publish_current_media()

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        if reason_code != 0:
            print(f"MQTT connect failed: {reason_code}", file=sys.stderr)
            return
        transport = f"{args.mqtt_transport}{' TLS' if args.mqtt_tls else ''}"
        print(f"MQTT connected: {args.mqtt_host}:{args.mqtt_port} ({transport})")
        print(f"MQTT Base Topic: {args.base_topic}")
        print(f"  Layout Topic: {layout_topic}")
        print(f"  Action Topic: {action_topic}")
        print(f"  Hello Topic:  {hello_topic}")
        print(f"  Status Topic: {status_topic}")
        print(f"  Music Topic:  {music_list_topic}")
        print(f"  Media Topic:  {media_list_topic}")
        print(f"  Current Song: {music_current_topic}")
        print(f"  Current Media:{media_current_topic}")
        client.subscribe([(action_topic, 1), (hello_topic, 1), (music_request_topic, 0), (media_request_topic, 0)])
        publish_layout()
        publish_media_list("mp3")
        publish_media_list("mp4")
        publish_current_music()

    def on_message(client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            if msg.topic == hello_topic:
                publish_layout()
                publish_media_list("mp3")
                publish_media_list("mp4")
                publish_current_music()
                return
            if msg.topic == music_request_topic:
                publish_music_list()
                publish_current_music()
                return
            if msg.topic == media_request_topic:
                request = json.loads(msg.payload.decode("utf-8") or "{}")
                publish_media_list(str(request.get("kind") or "mp3"))
                publish_current_media()
                return
            payload = json.loads(msg.payload.decode("utf-8"))
            dedupe_key = action_dedupe_key(payload)
            now = time.monotonic()
            while recent_actions:
                oldest_key, oldest_at = next(iter(recent_actions.items()))
                if now - oldest_at <= 8:
                    break
                recent_actions.pop(oldest_key)
            if dedupe_key in recent_actions:
                client.publish(status_topic, json.dumps({"ok": True, "id": payload.get("id"), "duplicate": True}), qos=0)
                print(f"Skipped duplicate action: {payload.get('label', '')}")
                return
            recent_actions[dedupe_key] = now
            action = normalize_action(payload)
            execute_action(action, allow_shell=args.allow_shell)
            publish_current_media()
            client.publish(status_topic, json.dumps({"ok": True, "id": payload.get("id")}), qos=0)
            print(f"Executed: {action.get('type')} {action.get('_label') or payload.get('label', '')}")
        except Exception as exc:
            error = {"ok": False, "error": str(exc)}
            client.publish(status_topic, json.dumps(error, ensure_ascii=False), qos=0)
            print(f"Action failed: {exc}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        print(f"Connecting MQTT: {args.mqtt_host}:{args.mqtt_port}")
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
        while True:
            rc = client.loop_forever(retry_first_connection=True)
            print(f"MQTT loop ended with code {rc}; reconnecting in 5 seconds.", file=sys.stderr)
            time.sleep(5)
    except (ConnectionRefusedError, OSError) as exc:
        print(f"MQTT connection failed: {exc}", file=sys.stderr)
        print(
            "Check the broker address/port, or run with local Mosquitto settings such as "
            "--mqtt-host 127.0.0.1 --mqtt-port 1883 --mqtt-transport tcp --no-mqtt-tls.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Stopping runtime.")
    finally:
        if http_server:
            http_server.shutdown()
        release_single_instance_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
