from __future__ import annotations

import argparse
import asyncio
import json
import locale
import platform
import re
import sys
from datetime import datetime
from typing import AsyncGenerator, Optional

from ebadge_cli.badge_frame import build_frame, parse_frame
from ebadge_cli.badge_info import parse_badge_info
from ebadge_cli.battery_command import build_request, parse_frame_bytes
from ebadge_cli.bind_response import parse_bind_response
from ebadge_cli.ble_constants import (
    SERVICE_AE00,
    CHAR_NOTIFY_AE00,
    CHAR_WRITE_AE00,
    SERVICE_C2E6,
    CHAR_NOTIFY_C2E6,
    CHAR_WRITE_C2E6,
)
from ebadge_cli.ble_session import BleMode, run_session, run_write_only, scan_devices, get_device_info


class BatteryReadError(RuntimeError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebadge-cli")
    sub = parser.add_subparsers(dest="command", required=True)

    battery = sub.add_parser("battery")
    battery.add_argument("--name", default="E87")
    battery.add_argument("--uuid", dest="address")
    battery.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    battery.add_argument("--timeout", type=float, default=5.0)
    battery.add_argument("--retries", type=int, default=3)
    battery.add_argument("--repeat", type=int, default=1)
    battery.add_argument("--interval", type=float, default=2.0)
    battery.add_argument("--verbose", action="store_true")
    battery.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan")
    scan.add_argument("--name")
    scan.add_argument("--timeout", type=float, default=5.0)
    scan.add_argument("--watch", action="store_true")
    scan.add_argument("--interval", type=float, default=2.0)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--verbose", action="store_true")

    devices = sub.add_parser("devices")
    devices.add_argument("--name")
    devices.add_argument("--timeout", type=float, default=5.0)
    devices.add_argument("--watch", action="store_true")
    devices.add_argument("--interval", type=float, default=2.0)
    devices.add_argument("--json", action="store_true")
    devices.add_argument("--verbose", action="store_true")

    info = sub.add_parser("info")
    info.add_argument("--name", default="E87")
    info.add_argument("--uuid", dest="address")
    info.add_argument("--timeout", type=float, default=5.0)
    info.add_argument("--json", action="store_true")
    info.add_argument("--verbose", action="store_true")

    raw = sub.add_parser("raw")
    raw.add_argument("--name", default="E87")
    raw.add_argument("--uuid", dest="address")
    raw.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    raw.add_argument("--flag", type=int, default=0)
    raw.add_argument("--cmd", type=int, required=True)
    raw.add_argument("--payload", default="")
    raw.add_argument("--timeout", type=float, default=5.0)
    raw.add_argument("--retries", type=int, default=3)
    raw.add_argument("--parse", action="store_true")
    raw.add_argument("--json", action="store_true")
    raw.add_argument("--verbose", action="store_true")

    bind = sub.add_parser("bind")
    bind.add_argument("--name", default="E87")
    bind.add_argument("--uuid", dest="address")
    bind.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    bind.add_argument("--lang", choices=["zh", "en"], default=None)
    bind.add_argument("--hour12", action="store_true")
    bind.add_argument("--hour24", action="store_true")
    bind.add_argument("--device-id", type=int, dest="device_id")
    bind.add_argument("--timeout", type=float, default=5.0)
    bind.add_argument("--retries", type=int, default=3)
    bind.add_argument("--json", action="store_true")
    bind.add_argument("--verbose", action="store_true")

    time_sync = sub.add_parser("time-sync")
    time_sync.add_argument("--name", default="E87")
    time_sync.add_argument("--uuid", dest="address")
    time_sync.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    time_sync.add_argument("--timeout", type=float, default=5.0)
    time_sync.add_argument("--json", action="store_true")
    time_sync.add_argument("--verbose", action="store_true")

    badge_info = sub.add_parser("badge-info")
    badge_info.add_argument("--name", default="E87")
    badge_info.add_argument("--uuid", dest="address")
    badge_info.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    badge_info.add_argument("--timeout", type=float, default=5.0)
    badge_info.add_argument("--retries", type=int, default=3)
    badge_info.add_argument("--json", action="store_true")
    badge_info.add_argument("--verbose", action="store_true")

    push_image = sub.add_parser("push-image")
    push_image.add_argument("--file", "-f", required=True, help="圖片路徑 (png/jpg/avi)")
    push_image.add_argument("--name", default="E87")
    push_image.add_argument("--uuid", dest="address")
    push_image.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_image.add_argument("--timeout", type=float, default=30.0)
    push_image.add_argument("--width", type=int, default=368)
    push_image.add_argument("--height", type=int, default=368)
    push_image.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay", help="Delay between chunks in ms (default 0)")
    push_image.add_argument("--json", action="store_true")
    push_image.add_argument("--verbose", action="store_true")

    push_video = sub.add_parser("push-video")
    push_video.add_argument("--file", "-f", required=True, help="影片路徑 (mp4/avi/...) 或圖片目錄")
    push_video.add_argument("--name", default="E87")
    push_video.add_argument("--uuid", dest="address")
    push_video.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_video.add_argument("--timeout", type=float, default=60.0)
    push_video.add_argument("--width", type=int, default=368)
    push_video.add_argument("--height", type=int, default=368)
    push_video.add_argument("--fps", type=int, default=12)
    push_video.add_argument("--duration", type=float, default=None, help="截取時長 (秒)，不設定則完整轉換")
    push_video.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay", help="Delay between chunks in ms (default 0)")
    push_video.add_argument("--json", action="store_true")
    push_video.add_argument("--verbose", action="store_true")

    push_danmaku = sub.add_parser("push-danmaku")
    push_danmaku.add_argument("--text", "-t", required=True, help="跑馬燈文字")
    push_danmaku.add_argument("--name", default="E87")
    push_danmaku.add_argument("--uuid", dest="address")
    push_danmaku.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_danmaku.add_argument("--timeout", type=float, default=60.0)
    push_danmaku.add_argument("--width", type=int, default=368)
    push_danmaku.add_argument("--height", type=int, default=368)
    push_danmaku.add_argument("--fps", type=int, default=12)
    push_danmaku.add_argument("--duration", type=float, default=5.0, help="動畫時長 (秒)")
    push_danmaku.add_argument("--font-size", type=int, default=64, dest="font_size", help="字體大小 (像素)")
    push_danmaku.add_argument("--font-color", default="FFFFFF", dest="font_color", help="字體顏色 (RRGGBB)")
    push_danmaku.add_argument("--bg-color", default="000000", dest="bg_color", help="背景顏色 (RRGGBB)")
    push_danmaku.add_argument("--font", default=None, dest="font_path", help="字體檔案路徑 (.ttf/.ttc/.otf)")
    push_danmaku.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay")
    push_danmaku.add_argument("--json", action="store_true")
    push_danmaku.add_argument("--verbose", action="store_true")

    push_images = sub.add_parser("push-images")
    push_images.add_argument("--files", "-f", required=True, help="圖片路徑 (逗號分隔)")
    push_images.add_argument("--name", default="E87")
    push_images.add_argument("--uuid", dest="address")
    push_images.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_images.add_argument("--timeout", type=float, default=60.0)
    push_images.add_argument("--width", type=int, default=368)
    push_images.add_argument("--height", type=int, default=368)
    push_images.add_argument("--fps", type=int, default=12)
    push_images.add_argument("--duration", type=float, default=2.0, help="每張圖片顯示時長 (秒)")
    push_images.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay")
    push_images.add_argument("--json", action="store_true")
    push_images.add_argument("--verbose", action="store_true")

    push_qr = sub.add_parser("push-qr")
    push_qr.add_argument("--data", "-d", required=True, help="QR Code 內容 (URL 或文字)")
    push_qr.add_argument("--name", default="E87")
    push_qr.add_argument("--uuid", dest="address")
    push_qr.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_qr.add_argument("--timeout", type=float, default=30.0)
    push_qr.add_argument("--width", type=int, default=368)
    push_qr.add_argument("--height", type=int, default=368)
    push_qr.add_argument("--fg-color", default="000000", dest="fg_color", help="前景色 (RRGGBB)")
    push_qr.add_argument("--bg-color", default="FFFFFF", dest="bg_color", help="背景色 (RRGGBB)")
    push_qr.add_argument("--zoom", type=float, default=1.0, help="縮放 (0.85~1.45)")
    push_qr.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay")
    push_qr.add_argument("--json", action="store_true")
    push_qr.add_argument("--verbose", action="store_true")

    push_pattern = sub.add_parser("push-pattern")
    push_pattern.add_argument("--pattern", "-p", default="gradient",
                              choices=["gradient", "pulse", "checker", "rainbow", "wave"],
                              help="圖案類型")
    push_pattern.add_argument("--name", default="E87")
    push_pattern.add_argument("--uuid", dest="address")
    push_pattern.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    push_pattern.add_argument("--timeout", type=float, default=60.0)
    push_pattern.add_argument("--width", type=int, default=368)
    push_pattern.add_argument("--height", type=int, default=368)
    push_pattern.add_argument("--fps", type=int, default=12)
    push_pattern.add_argument("--frames", type=int, default=60, help="總幀數 (10~300)")
    push_pattern.add_argument("--color1", default="FF0000", help="主色 (RRGGBB)")
    push_pattern.add_argument("--color2", default="0000FF", help="副色 (RRGGBB)")
    push_pattern.add_argument("--inter-chunk-delay", type=int, default=0, dest="inter_chunk_delay")
    push_pattern.add_argument("--json", action="store_true")
    push_pattern.add_argument("--verbose", action="store_true")

    unbind = sub.add_parser("unbind")
    unbind.add_argument("--name", default="E87")
    unbind.add_argument("--uuid", dest="address")
    unbind.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    unbind.add_argument("--timeout", type=float, default=5.0)
    unbind.add_argument("--json", action="store_true")
    unbind.add_argument("--verbose", action="store_true")

    list_files = sub.add_parser("list-files")
    list_files.add_argument("--name", default="E87")
    list_files.add_argument("--uuid", dest="address")
    list_files.add_argument("--timeout", type=float, default=30.0)
    list_files.add_argument("--json", action="store_true")
    list_files.add_argument("--verbose", action="store_true")

    ota_check = sub.add_parser("ota-check")
    ota_check.add_argument("--serial", "-s", required=True, help="設備序號 (bind 回應的 serialNumber)")
    ota_check.add_argument("--version", "-v", required=True, help="當前韌體版本 (bind 回應的 firmwaVersion)")
    ota_check.add_argument("--model", default="dev", help="OTA 模型路徑 (預設 dev)")
    ota_check.add_argument("--json", action="store_true")

    ota_update = sub.add_parser("ota-update")
    ota_update.add_argument("--file", "-f", help="本地韌體檔案路徑")
    ota_update.add_argument("--url", "-u", help="韌體下載 URL (來自 ota-check 的 download_address)")
    ota_update.add_argument("--serial", "-s", help="設備序號 (與 --version 搭配自動 ota-check)")
    ota_update.add_argument("--version", "-v", help="當前韌體版本 (與 --serial 搭配自動 ota-check)")
    ota_update.add_argument("--name", default="E87")
    ota_update.add_argument("--uuid", dest="address")
    ota_update.add_argument("--mode", choices=["c2e6", "ae00"], default="c2e6")
    ota_update.add_argument("--timeout", type=float, default=120.0)
    ota_update.add_argument("--json", action="store_true")
    ota_update.add_argument("--verbose", action="store_true")

    return parser


def _mode_from_args(mode: str) -> BleMode:
    if mode == "ae00":
        return BleMode(SERVICE_AE00, CHAR_WRITE_AE00, CHAR_NOTIFY_AE00)
    return BleMode(SERVICE_C2E6, CHAR_WRITE_C2E6, CHAR_NOTIFY_C2E6)


def _parse_color(hex_str: str) -> tuple[int, int, int]:
    """Parse RRGGBB hex string to (R, G, B) tuple."""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) != 6:
        raise ValueError(f"invalid color: {hex_str} (expected RRGGBB)")
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


def _print_verbose(verbose: bool, message: str) -> None:
    if verbose:
        print(message)


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _validate_byte(value: int, label: str) -> int:
    if value < 0 or value > 0xFF:
        raise ValueError(f"{label} out of range: {value}")
    return value


def _parse_payload(payload: str) -> list[int]:
    if not payload.strip():
        return []
    tokens = [token for token in re.split(r"[ ,]+", payload.strip()) if token]
    values: list[int] = []
    for token in tokens:
        base = 16 if token.lower().startswith("0x") or re.search(r"[a-f][0-9a-f]+", token, re.IGNORECASE) else 10
        try:
            value = int(token, base)
        except ValueError as exc:
            raise ValueError(f"invalid byte: {token}") from exc
        values.append(_validate_byte(value, "payload byte"))
    return values


def _bytes_to_hex(values: list[int]) -> str:
    return "".join(f"{value:02X}" for value in values)


_SERIAL_COUNTER = 0


def _java_string_hash(value: str) -> int:
    result = 0
    for char in value:
        result = (31 * result + ord(char)) & 0xFFFFFFFF
    if result & 0x80000000:
        result -= 0x100000000
    return result


def _default_device_id() -> int:
    parts = platform.uname()
    signature = "35" + "".join(str(len(part) % 10) for part in [
        parts.system,
        parts.node,
        parts.release,
        parts.version,
        parts.machine,
        parts.processor,
    ])
    return _java_string_hash(signature)


def _long_to_6_bytes(value: int) -> list[int]:
    if value < 0:
        value = (1 << 64) + value
    return [(value >> (8 * i)) & 0xFF for i in range(6)]


def _next_flag(payload_len: int) -> int:
    global _SERIAL_COUNTER
    serial = _SERIAL_COUNTER
    if serial == 16:
        serial = 0
    _SERIAL_COUNTER = serial + 1
    is_long = payload_len + 6 > 20
    flag = (serial << 3) | (int(is_long) << 2) | (1 << 1)
    return flag & 0xFF


def _build_command_frame(cmd: int, payload: list[int]) -> list[int]:
    flag = _next_flag(len(payload))
    return build_frame(flag=flag, cmd=cmd, payload=payload)


def _bind_payload(lang: Optional[str], hour12: Optional[bool], device_id: Optional[int]) -> list[int]:
    if lang is None:
        default_locale = locale.getdefaultlocale()[0] if locale.getdefaultlocale() else None
        lang = "zh" if (default_locale or "").startswith("zh") else "en"
    if hour12 is None:
        hour12 = False
    lang_flag = 0 if lang == "zh" else 1
    hour_flag = 1 if hour12 else 0
    header = ((hour_flag << 2) | (lang_flag << 1)) & 0xFF
    device_value = _default_device_id() if device_id is None else device_id
    body = _long_to_6_bytes(device_value)
    return [header] + body + body


async def _run_battery(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    retries: int,
    verbose: bool,
) -> dict[str, object]:
    response: list[int] | None = None
    last_error = "timeout waiting for notify"

    def on_notify(data: bytearray) -> None:
        nonlocal response
        response = list(data)
        _print_verbose(verbose, f"notify bytes={response}")

    for attempt in range(1, retries + 1):
        response = None
        _print_verbose(verbose, f"attempt={attempt}")
        await run_session(mode, name, address, timeout, build_request(), on_notify)
        if response is None:
            last_error = "timeout waiting for notify"
            _print_verbose(verbose, last_error)
            continue
        parsed = parse_frame_bytes(response)
        if parsed:
            return {
                "percent": parsed["percent"],
                "mode": parsed["mode"],
                "attempt": attempt,
            }
        last_error = "invalid response"
        _print_verbose(verbose, "invalid response, retrying")
    raise BatteryReadError(f"{last_error} after {retries} attempts", retries)


async def _run_scan(
    name: Optional[str],
    timeout: float,
    verbose: bool,
) -> list[dict[str, object]]:
    _print_verbose(verbose, f"scan timeout={timeout}s")
    return await scan_devices(timeout=timeout, name=name)


def _rssi_value(item: dict[str, object]) -> int:
    value = item.get("rssi")
    return value if isinstance(value, int) else -999


async def _run_devices(
    name: Optional[str],
    timeout: float,
    verbose: bool,
) -> list[dict[str, object]]:
    results = await _run_scan(name, timeout, verbose)
    return sorted(results, key=_rssi_value, reverse=True)


async def _run_battery_repeat(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    retries: int,
    repeat: int,
    interval: float,
    verbose: bool,
) -> AsyncGenerator[dict[str, object], None]:
    for index in range(1, repeat + 1):
        result = await _run_battery(name, address, mode, timeout, retries, verbose)
        result_with_index = {"iteration": index, **result}
        yield result_with_index
        if index < repeat:
            await asyncio.sleep(interval)


async def _run_scan_watch(
    name: Optional[str],
    timeout: float,
    interval: float,
    verbose: bool,
) -> AsyncGenerator[list[dict[str, object]], None]:
    while True:
        results = await _run_scan(name, timeout, verbose)
        yield results
        await asyncio.sleep(interval)


async def _run_info(
    name: Optional[str],
    address: Optional[str],
    timeout: float,
    verbose: bool,
) -> dict[str, object]:
    _print_verbose(verbose, f"info timeout={timeout}s")
    return await get_device_info(name, address, timeout)


async def _run_raw(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    retries: int,
    flag: int,
    cmd: int,
    payload: list[int],
    parse: bool,
    verbose: bool,
) -> dict[str, object]:
    response: list[int] | None = None
    last_error = "timeout waiting for notify"

    def on_notify(data: bytearray) -> None:
        nonlocal response
        response = list(data)
        _print_verbose(verbose, f"notify bytes={response}")

    frame = build_frame(flag=flag, cmd=cmd, payload=payload)
    for attempt in range(1, retries + 1):
        response = None
        _print_verbose(verbose, f"attempt={attempt}")
        await run_session(mode, name, address, timeout, frame, on_notify)
        if response is None:
            last_error = "timeout waiting for notify"
            _print_verbose(verbose, last_error)
            continue
        if parse:
            parsed = parse_frame(response)
            if parsed and parsed["checksum_valid"]:
                return {
                    "attempt": attempt,
                    "frame": frame,
                    "response": response,
                    "parsed": parsed,
                }
            last_error = "invalid response"
            _print_verbose(verbose, "invalid response, retrying")
            continue
        return {
            "attempt": attempt,
            "frame": frame,
            "response": response,
        }
    raise RuntimeError(f"{last_error} after {retries} attempts")


async def _run_bind(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    retries: int,
    lang: Optional[str],
    hour12: Optional[bool],
    device_id: Optional[int],
    verbose: bool,
) -> dict[str, object]:
    response: list[int] | None = None
    last_error = "timeout waiting for notify"

    def on_notify(data: bytearray) -> None:
        nonlocal response
        response = list(data)
        _print_verbose(verbose, f"notify bytes={response}")

    payload = _bind_payload(lang, hour12, device_id)
    frame = _build_command_frame(cmd=0x60, payload=payload)
    for attempt in range(1, retries + 1):
        response = None
        _print_verbose(verbose, f"attempt={attempt}")
        await run_session(mode, name, address, timeout, frame, on_notify)
        if response is None:
            last_error = "timeout waiting for notify"
            _print_verbose(verbose, last_error)
            continue
        parsed = parse_frame(response)
        if parsed and parsed["checksum_valid"] and parsed["cmd"] == 0x61:
            bind_resp = parse_bind_response(parsed.get("payload", []))
            result = {
                "attempt": attempt,
                "frame": frame,
                "response": response,
                "response_hex": _bytes_to_hex(response),
                "parsed": parsed,
            }
            if bind_resp:
                result["bind"] = {
                    "state": bind_resp.state,
                    "pactVersion": bind_resp.pact_version,
                    "firmwaVersion": bind_resp.firmwa_version,
                    "platform": bind_resp.platform,
                    "serialNumber": bind_resp.serial_number,
                    "functionConfig": bind_resp.function_config,
                    "functionConfig1": bind_resp.function_config1,
                    "functionConfig2": bind_resp.function_config2,
                    "uiVersion": bind_resp.ui_version,
                    "functionBytes": bind_resp.function_bytes,
                }
            return result
        if parsed:
            return {
                "attempt": attempt,
                "frame": frame,
                "response": response,
                "response_hex": _bytes_to_hex(response),
                "parsed": parsed,
                "error": f"cmd=0x{parsed['cmd']:02X}, checksum_valid={parsed['checksum_valid']}, payload_len={len(parsed.get('payload', []))}"
            }
        last_error = "invalid response"
        _print_verbose(verbose, "invalid response, retrying")
    raise RuntimeError(f"{last_error} after {retries} attempts")


async def _run_badge_info(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    retries: int,
    verbose: bool,
) -> dict[str, object]:
    """查詢吧唧顯示參數 (cmd 0xC7)。設備可能主動推送，或需發送請求觸發。"""
    response: list[int] | None = None
    last_error = "timeout waiting for notify"

    def on_notify(data: bytearray) -> None:
        nonlocal response
        response = list(data)
        _print_verbose(verbose, f"notify bytes={response}")

    # 請求 BadgeInfo: cmd 0xC6 (COMMAND_REQ_BADGE_INFO), payload [0x01]
    # 設備回應 cmd 0xC7 (COMMAND_REP_BADGE_INFO)
    payload = [0x01]
    frame = _build_command_frame(cmd=0xC6, payload=payload)
    for attempt in range(1, retries + 1):
        response = None
        _print_verbose(verbose, f"attempt={attempt}")
        await run_session(mode, name, address, timeout, frame, on_notify)
        if response is None:
            last_error = "timeout waiting for notify"
            _print_verbose(verbose, last_error)
            continue
        parsed = parse_frame(response)
        if parsed and parsed["checksum_valid"] and parsed["cmd"] == 0xC7:
            info = parse_badge_info(parsed.get("payload", []))
            if info:
                return {
                    "attempt": attempt,
                    "width": info.width,
                    "height": info.height,
                    "picture_width": info.picture_width,
                    "picture_height": info.picture_height,
                    "memory": info.memory,
                    "resolution": f"{info.picture_width or 368}x{info.picture_height or 368}",
                }
        last_error = "invalid or unexpected response"
        _print_verbose(verbose, last_error)
    raise RuntimeError(f"{last_error} after {retries} attempts")


async def _run_time_sync(
    name: Optional[str],
    address: Optional[str],
    mode: BleMode,
    timeout: float,
    verbose: bool,
) -> dict[str, object]:
    now = datetime.now()
    payload = [
        now.year & 0xFF,
        (now.year >> 8) & 0xFF,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
    ]
    frame = _build_command_frame(cmd=0x02, payload=payload)
    _print_verbose(verbose, f"time-sync frame={frame}")
    await run_write_only(mode, name, address, timeout, frame)
    return {
        "frame": frame,
        "payload": payload,
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "battery":
            mode = _mode_from_args(args.mode)
            if args.repeat < 1:
                raise ValueError("repeat must be >= 1")
            if args.interval < 0:
                raise ValueError("interval must be >= 0")
            if args.repeat > 1:
                async def _watch_battery() -> None:
                    async for result in _run_battery_repeat(
                        args.name,
                        args.address,
                        mode,
                        args.timeout,
                        args.retries,
                        args.repeat,
                        args.interval,
                        args.verbose,
                    ):
                        if args.json:
                            _emit_json({"command": "battery", "status": "ok", **result})
                        else:
                            print(result["percent"])
                asyncio.run(_watch_battery())
                return 0
            result = asyncio.run(
                _run_battery(args.name, args.address, mode, args.timeout, args.retries, args.verbose)
            )
            if args.json:
                _emit_json({"command": "battery", "status": "ok", **result})
            else:
                print(result["percent"])
            return 0
        if args.command == "scan":
            if args.watch:
                async def _watch() -> None:
                    async for results in _run_scan_watch(args.name, args.timeout, args.interval, args.verbose):
                        if args.json:
                            _emit_json({"command": "scan", "status": "ok", "devices": results})
                        else:
                            if not results:
                                print("(no devices)")
                                continue
                            for item in results:
                                name = item.get("name") or "-"
                                rssi = item.get("rssi")
                                print(f"{name} {item['address']} rssi={rssi}")
                asyncio.run(_watch())
                return 0
            results = asyncio.run(_run_scan(args.name, args.timeout, args.verbose))
            if args.json:
                _emit_json({"command": "scan", "status": "ok", "devices": results})
            else:
                for item in results:
                    name = item.get("name") or "-"
                    rssi = item.get("rssi")
                    print(f"{name} {item['address']} rssi={rssi}")
            return 0 if results else 2
        if args.command == "devices":
            if args.watch:
                async def _watch_devices() -> None:
                    async for results in _run_scan_watch(args.name, args.timeout, args.interval, args.verbose):
                        results_sorted = sorted(results, key=_rssi_value, reverse=True)
                        if args.json:
                            _emit_json({"command": "devices", "status": "ok", "devices": results_sorted})
                        else:
                            if not results_sorted:
                                print("(no devices)")
                                continue
                            for index, item in enumerate(results_sorted, start=1):
                                name = item.get("name") or "-"
                                rssi = item.get("rssi")
                                print(f"{index}. {name} {item['address']} rssi={rssi}")
                asyncio.run(_watch_devices())
                return 0
            results = asyncio.run(_run_devices(args.name, args.timeout, args.verbose))
            if args.json:
                _emit_json({"command": "devices", "status": "ok", "devices": results})
            else:
                for index, item in enumerate(results, start=1):
                    name = item.get("name") or "-"
                    rssi = item.get("rssi")
                    print(f"{index}. {name} {item['address']} rssi={rssi}")
            return 0 if results else 2
        if args.command == "info":
            result = asyncio.run(_run_info(args.name, args.address, args.timeout, args.verbose))
            if args.json:
                _emit_json({"command": "info", "status": "ok", **result})
            else:
                name = result.get("name") or "-"
                rssi = result.get("rssi")
                print(f"{name} {result['address']} rssi={rssi}")
            return 0
        if args.command == "raw":
            mode = _mode_from_args(args.mode)
            flag = _validate_byte(args.flag, "flag")
            cmd = _validate_byte(args.cmd, "cmd")
            payload = _parse_payload(args.payload)
            result = asyncio.run(
                _run_raw(
                    args.name,
                    args.address,
                    mode,
                    args.timeout,
                    args.retries,
                    flag,
                    cmd,
                    payload,
                    args.parse,
                    args.verbose,
                )
            )
            if args.json:
                _emit_json({"command": "raw", "status": "ok", **result})
            else:
                print(result["response"])
            return 0
        if args.command == "bind":
            mode = _mode_from_args(args.mode)
            if args.hour12 and args.hour24:
                raise ValueError("use only one of --hour12 or --hour24")
            hour12 = True if args.hour12 else False if args.hour24 else None
            result = asyncio.run(
                _run_bind(
                    args.name,
                    args.address,
                    mode,
                    args.timeout,
                    args.retries,
                    args.lang,
                    hour12,
                    args.device_id,
                    args.verbose,
                )
            )
            if args.json:
                _emit_json({"command": "bind", "status": "ok", **result})
            else:
                print(result["response"])
            return 0
        if args.command == "time-sync":
            mode = _mode_from_args(args.mode)
            result = asyncio.run(_run_time_sync(args.name, args.address, mode, args.timeout, args.verbose))
            if args.json:
                _emit_json({"command": "time-sync", "status": "ok", **result})
            else:
                print(result["payload"])
            return 0
        if args.command == "push-image":
            try:
                from ebadge_cli.image_converter import prepare_image

                file_bytes = prepare_image(args.file, (args.width, args.height))
            except FileNotFoundError as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-image", "status": "error", "error": str(e)})
                else:
                    print(f"error: {e}", file=sys.stderr)
                return 2
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-image", "status": "error", "error": str(e)})
                else:
                    print(f"error: 圖片轉換失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload, TransferResult

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="image",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-image",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print("push-image completed")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "push-video":
            try:
                from ebadge_cli.image_converter import prepare_video

                file_bytes = prepare_video(args.file, (args.width, args.height), args.fps, getattr(args, "duration", None))
            except FileNotFoundError as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-video", "status": "error", "error": str(e)})
                else:
                    print(f"error: {e}", file=sys.stderr)
                return 2
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-video", "status": "error", "error": str(e)})
                else:
                    print(f"error: 影片轉換失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload, TransferResult

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="video",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-video",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print("push-video completed")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "push-danmaku":
            try:
                from ebadge_cli.image_converter import prepare_danmaku

                font_color = _parse_color(args.font_color)
                bg_color = _parse_color(args.bg_color)
                file_bytes = prepare_danmaku(
                    text=args.text,
                    target_size=(args.width, args.height),
                    fps=args.fps,
                    duration=args.duration,
                    font_size=args.font_size,
                    font_color=font_color,
                    bg_color=bg_color,
                    font_path=getattr(args, "font_path", None),
                )
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-danmaku", "status": "error", "error": str(e)})
                else:
                    print(f"error: 跑馬燈生成失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="video",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-danmaku",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print("push-danmaku completed")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "push-images":
            try:
                from ebadge_cli.image_converter import prepare_slideshow

                file_list = [p.strip() for p in args.files.split(",") if p.strip()]
                if not file_list:
                    raise ValueError("no files specified")
                file_bytes = prepare_slideshow(
                    file_paths=file_list,
                    target_size=(args.width, args.height),
                    fps=args.fps,
                    duration_per_image=args.duration,
                )
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-images", "status": "error", "error": str(e)})
                else:
                    print(f"error: 圖片合成失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="video",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-images",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print(f"push-images completed ({len(file_list)} images)")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "push-qr":
            try:
                from ebadge_cli.image_converter import prepare_qr

                fg_color = _parse_color(args.fg_color)
                bg_color = _parse_color(args.bg_color)
                file_bytes = prepare_qr(
                    data=args.data,
                    target_size=(args.width, args.height),
                    fg_color=fg_color,
                    bg_color=bg_color,
                    zoom=args.zoom,
                )
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-qr", "status": "error", "error": str(e)})
                else:
                    print(f"error: QR Code 生成失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="qr",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-qr",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print("push-qr completed")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "push-pattern":
            try:
                from ebadge_cli.image_converter import prepare_pattern

                color1 = _parse_color(args.color1)
                color2 = _parse_color(args.color2)
                file_bytes = prepare_pattern(
                    pattern=args.pattern,
                    target_size=(args.width, args.height),
                    fps=args.fps,
                    frame_count=args.frames,
                    color1=color1,
                    color2=color2,
                )
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "push-pattern", "status": "error", "error": str(e)})
                else:
                    print(f"error: 圖案生成失敗: {e}", file=sys.stderr)
                return 2
            from ebadge_cli.rcsp_transfer import run_e87_upload

            result = asyncio.run(
                run_e87_upload(
                    name=args.name,
                    address=args.address,
                    file_bytes=file_bytes,
                    upload_mode="pattern",
                    timeout=args.timeout,
                    inter_chunk_delay_ms=getattr(args, "inter_chunk_delay", 0),
                    verbose=args.verbose,
                    on_progress=lambda msg: print(msg) if args.verbose else None,
                )
            )
            if args.json:
                _emit_json({
                    "command": "push-pattern",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print(f"push-pattern completed ({args.pattern})")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
        if args.command == "unbind":
            mode = _mode_from_args(args.mode)
            frame = build_frame(flag=0x00, cmd=0x62, payload=[0x01])
            _print_verbose(args.verbose, f"unbind frame={frame}")
            await_result: dict[str, object] = {"frame": frame}
            try:
                asyncio.run(run_write_only(mode, args.name, args.address, args.timeout, frame))
                await_result["success"] = True
            except Exception as e:
                await_result["success"] = False
                await_result["error"] = str(e)
            if args.json:
                _emit_json({"command": "unbind", "status": "ok" if await_result.get("success") else "error", **await_result})
            else:
                if await_result.get("success"):
                    print("unbind completed")
                else:
                    print(f"error: {await_result.get('error')}", file=sys.stderr)
            return 0 if await_result.get("success") else 2
        if args.command == "list-files":
            from ebadge_cli.file_browse import run_file_browse

            try:
                files = asyncio.run(
                    run_file_browse(
                        name=args.name,
                        address=args.address,
                        timeout=args.timeout,
                        verbose=args.verbose,
                    )
                )
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "list-files", "status": "error", "error": str(e)})
                else:
                    print(f"error: {e}", file=sys.stderr)
                return 2
            if args.json:
                _emit_json({"command": "list-files", "status": "ok", "files": files})
            else:
                if not files:
                    print("(no files)")
                else:
                    for entry in files:
                        kind = "F" if entry.get("is_file") else "D"
                        display_name = entry.get("path", entry.get("name", "?"))
                        print(f"[{kind}] {display_name}")
            return 0
        if args.command == "badge-info":
            mode = _mode_from_args(args.mode)
            result = asyncio.run(
                _run_badge_info(
                    args.name,
                    args.address,
                    mode,
                    args.timeout,
                    args.retries,
                    args.verbose,
                )
            )
            if args.json:
                _emit_json({"command": "badge-info", "status": "ok", **result})
            else:
                print(f"resolution: {result['resolution']}")
                print(f"picture: {result['picture_width']}x{result['picture_height']}")
                mem_kb = result['memory']
                print(f"memory: {mem_kb} KB ({mem_kb / 1024:.2f} MB)")
            return 0
        if args.command == "ota-check":
            from ebadge_cli.ota_api import ota_check

            try:
                result = ota_check(args.serial, args.version, args.model)
            except Exception as e:
                if getattr(args, "json", False):
                    _emit_json({"command": "ota-check", "status": "error", "error": str(e)})
                else:
                    print(f"error: {e}", file=sys.stderr)
                return 2
            if args.json:
                _emit_json({"command": "ota-check", "status": "ok", **result})
            else:
                code = result.get("code", "")
                body = result.get("body", {})
                if code == "200" and body.get("download_address"):
                    print(f"有新版本: {body.get('upgrade_version', '')}")
                    print(f"下載: {body.get('download_address', '')}")
                    print(f"大小: {body.get('upgrade_size', '')}")
                elif code == "200":
                    print("已是最新版本")
                else:
                    print(f"檢查失敗: {result.get('msg', code)}")
            return 0
        if args.command == "ota-update":
            firmware_bytes: bytes
            if args.file:
                firmware_bytes = open(args.file, "rb").read()
            elif args.url:
                from ebadge_cli.ota_api import download_firmware

                firmware_bytes = download_firmware(args.url)
            elif args.serial and args.version:
                from ebadge_cli.ota_api import ota_check, download_firmware

                result = ota_check(args.serial, args.version)
                url = (result.get("body") or {}).get("download_address", "")
                if not url:
                    err = result.get("msg", "no download address") or "no download address"
                    if getattr(args, "json", False):
                        _emit_json({"command": "ota-update", "status": "error", "error": err})
                    else:
                        print(f"error: {err}", file=sys.stderr)
                    return 2
                firmware_bytes = download_firmware(url)
            else:
                if getattr(args, "json", False):
                    _emit_json({"command": "ota-update", "status": "error", "error": "need --file, --url, or (--serial and --version)"})
                else:
                    print("error: need --file, --url, or (--serial and --version)", file=sys.stderr)
                return 2
            mode = _mode_from_args(args.mode)
            from ebadge_cli.ota_update import run_ota_update

            result = asyncio.run(
                run_ota_update(
                    mode,
                    args.name,
                    args.address,
                    firmware_bytes,
                    timeout=args.timeout,
                    verbose=args.verbose,
                )
            )
            if args.json:
                _emit_json({
                    "command": "ota-update",
                    "status": "ok" if result.success else "error",
                    "success": result.success,
                    "error": result.error,
                })
            else:
                if result.success:
                    print("ota-update completed")
                else:
                    print(f"error: {result.error}", file=sys.stderr)
            return 0 if result.success else 2
    except BatteryReadError as exc:
        if getattr(args, "json", False):
            _emit_json({
                "command": "battery",
                "status": "error",
                "error": str(exc),
                "attempts": exc.attempts,
            })
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        if getattr(args, "json", False):
            _emit_json({"status": "error", "error": str(exc)})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
