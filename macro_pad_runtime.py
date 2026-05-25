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

    for key in ("targetPath", "path", "arguments", "workingDirectory", "command", "keys", "text"):
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
    if not vk:
        raise ValueError(f"unknown media command: {command}")
    press_virtual_key(vk)


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
    elif action_type == "hotkey":
        run_hotkey(action)
    elif action_type == "text":
        run_text(action)
    elif action_type == "shell" and allow_shell:
        subprocess.Popen(str(action.get("command") or ""), shell=True)
    else:
        raise ValueError(f"unsupported or disabled action type: {action_type}")


def start_http_server(host: str, port: int) -> ThreadingHTTPServer:
    static_dir = static_root()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

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
        client.subscribe([(action_topic, 1), (hello_topic, 1)])
        publish_layout()

    def on_message(client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            if msg.topic == hello_topic:
                publish_layout()
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
        client.loop_forever()
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
