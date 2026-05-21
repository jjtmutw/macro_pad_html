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
import ctypes
import json
import os
import shlex
import ssl
import subprocess
import sys
import threading
import winreg
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

try:
    import pyautogui
except Exception:  # pragma: no cover - reported at runtime.
    pyautogui = None


ROOT = Path(__file__).resolve().parent
DEFAULT_LAYOUT = ROOT / "macro_pad_layout.json"

MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
}

OFFICE_ICON_TARGETS = {
    "wordicon.exe": "winword.exe",
    "xlicons.exe": "excel.exe",
    "pptico.exe": "powerpnt.exe",
}


def load_layout(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


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


def run_launch(action: dict[str, Any]) -> None:
    target = action.get("targetPath") or action.get("path")
    if not target:
        raise ValueError(f"launch action missing targetPath{action_context(action)}")

    args = action.get("arguments") or ""
    cwd = action.get("workingDirectory") or None
    target = resolve_launch_target(str(target))

    if args:
        subprocess.Popen([target, *shlex.split(str(args), posix=False)], cwd=cwd or None)
    else:
        os.startfile(target)  # type: ignore[attr-defined]


def run_media(action: dict[str, Any]) -> None:
    command = str(action.get("command") or "")
    vk = MEDIA_KEYS.get(command)
    if not vk:
        raise ValueError(f"unknown media command: {command}")
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def run_hotkey(action: dict[str, Any]) -> None:
    if pyautogui is None:
        raise RuntimeError("pyautogui is not installed")
    keys = action.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("hotkey action missing keys")
    pyautogui.hotkey(*[str(key).lower() for key in keys])


def run_text(action: dict[str, Any]) -> None:
    if pyautogui is None:
        raise RuntimeError("pyautogui is not installed")
    text = str(action.get("text") or "")
    pyautogui.write(text, interval=float(action.get("interval") or 0))


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
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


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
    parser.add_argument("--base-topic", default="macro-pad")
    parser.add_argument("--client-id", default="macro-pad-pc-runtime")
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--no-http", action="store_true", help="Disable the optional local static web server.")
    parser.add_argument("--allow-shell", action="store_true")
    args = parser.parse_args()

    layout_path = args.layout.resolve()
    http_server = None if args.no_http else start_http_server(args.http_host, args.http_port)
    if http_server:
        print(f"PWA/config server: http://{args.http_host}:{args.http_port}/pwa/")
        print(f"Config editor:      http://{args.http_host}:{args.http_port}/config.html")
    print(f"Layout file:        {layout_path}")

    layout_topic = f"{args.base_topic}/layout"
    action_topic = f"{args.base_topic}/action"
    hello_topic = f"{args.base_topic}/hello"
    status_topic = f"{args.base_topic}/status"

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
        client.publish(layout_topic, json.dumps(layout, ensure_ascii=False), qos=1, retain=True)
        print(f"Published layout to {layout_topic}")

    def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        if reason_code != 0:
            print(f"MQTT connect failed: {reason_code}", file=sys.stderr)
            return
        transport = f"{args.mqtt_transport}{' TLS' if args.mqtt_tls else ''}"
        print(f"MQTT connected: {args.mqtt_host}:{args.mqtt_port} ({transport})")
        client.subscribe([(action_topic, 1), (hello_topic, 1)])
        publish_layout()

    def on_message(client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            if msg.topic == hello_topic:
                publish_layout()
                return
            payload = json.loads(msg.payload.decode("utf-8"))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
