from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

_BLEAK_MODULE: Any | None = None


def _load_bleak() -> Any:
    global _BLEAK_MODULE
    if _BLEAK_MODULE is None:
        try:
            _BLEAK_MODULE = importlib.import_module("bleak")
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("bleak is required; install requirements.txt") from exc
    return _BLEAK_MODULE


def _get_bleak() -> tuple[Any, Any]:
    module = _load_bleak()
    return module.BleakClient, module.BleakScanner


@dataclass
class BleMode:
    service: str
    write_char: str
    notify_char: str


def accumulate_9e_then_callback(inner: Callable[[bytearray], None]) -> Callable[[bytearray], None]:
    """包裝 on_notify：累積 0x9E 分包，收到完整幀時才呼叫 inner。非 0x9E 幀直接傳遞。"""

    buf: bytearray = bytearray()

    def wrapper(data: bytearray) -> None:
        nonlocal buf
        if len(data) >= 1 and data[0] != 0x9E:
            inner(data)
            return
        buf.extend(data)
        while len(buf) >= 6 and buf[0] == 0x9E:
            plen = buf[4] | (buf[5] << 8)
            total = 6 + plen
            if len(buf) < total:
                return
            frame = bytearray(buf[:total])
            del buf[:total]
            inner(frame)

    return wrapper


async def _request_mtu_if_supported(client: Any) -> None:
    """連線後嘗試取得/協商 MTU。平台不支援時靜默略過。"""
    try:
        backend = getattr(client, "_backend", client)
        if hasattr(backend, "_acquire_mtu"):
            await backend._acquire_mtu()
    except Exception:
        pass


def _name_matches(device_name: Optional[str], filter_name: str) -> bool:
    """匹配設備名稱：完全匹配或前綴匹配（設備可能廣播為 E87 或 E87-xxx）。"""
    if device_name is None:
        return False
    return device_name == filter_name or device_name.startswith(filter_name + "-") or device_name.startswith(filter_name + " ")


async def find_device(name: Optional[str], address: Optional[str], timeout: float):
    _, scanner_cls = _get_bleak()
    if address:
        return await scanner_cls.find_device_by_address(address, timeout=timeout)
    if name:
        def match(device, _):
            return _name_matches(device.name, name)
        return await scanner_cls.find_device_by_filter(match, timeout=timeout)
    return await scanner_cls.find_device_by_filter(lambda device, _: device.name is not None, timeout=timeout)


async def run_session(
    mode: BleMode,
    name: Optional[str],
    address: Optional[str],
    timeout: float,
    write_bytes: list[int],
    on_notify: Callable[[bytearray], None],
    device: Any = None,
):
    """若傳入 device (BLEDevice) 則跳過重新掃描，直接建立 client."""
    if device is None:
        device = await find_device(name, address, timeout)
    if device is None:
        raise RuntimeError("device not found")

    client_cls, _ = _get_bleak()
    wrapped = accumulate_9e_then_callback(on_notify)
    client = client_cls(device)
    await client.connect()
    try:
        # 確保 service 發現完成（macOS 上 connect() 有時在 discoverServices 完成前返回）
        for _ in range(10):
            try:
                _ = client.services
                if _ is not None and len(list(_)) > 0:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        await _request_mtu_if_supported(client)
        await client.start_notify(mode.notify_char, lambda _, data: wrapped(bytearray(data)))
        await client.write_gatt_char(mode.write_char, bytes(write_bytes), response=True)
        await asyncio.sleep(timeout)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_write_only(
    mode: BleMode,
    name: Optional[str],
    address: Optional[str],
    timeout: float,
    write_bytes: list[int],
    device: Any = None,
) -> None:
    if device is None:
        device = await find_device(name, address, timeout)
    if device is None:
        raise RuntimeError("device not found")

    client_cls, _ = _get_bleak()
    client = client_cls(device)
    await client.connect()
    try:
        for _ in range(10):
            try:
                _ = client.services
                if _ is not None and len(list(_)) > 0:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        await client.write_gatt_char(mode.write_char, bytes(write_bytes), response=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def scan_devices(timeout: float, name: Optional[str]) -> list[dict[str, object]]:
    _, scanner_cls = _get_bleak()
    devices_adv = await scanner_cls.discover(timeout=timeout, return_adv=True)
    results: list[dict[str, object]] = []
    for device, adv in devices_adv.values():
        if name and not _name_matches(device.name, name):
            continue
        results.append({
            "name": device.name,
            "address": device.address,
            "rssi": adv.rssi if adv else None,
            "device": device,
        })
    return results


async def get_device_info(
    name: Optional[str],
    address: Optional[str],
    timeout: float,
) -> dict[str, object]:
    device = await find_device(name, address, timeout)
    if device is None:
        raise RuntimeError("device not found")
    return {
        "name": device.name,
        "address": device.address,
        "rssi": device.rssi,
    }
