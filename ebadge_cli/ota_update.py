"""OTA 韌體更新傳輸。

逆向自 UpdateManager.java。使用 0x9E 幀格式。
- 0xC0: COMMAND_REQ_UPDATE 發送 27 位元組 header
- 0xC1: COMMAND_RET_UPDATE 設備請求 [status, allowSendLength:4 BE, offset:4 BE]
- 0xC2: COMMAND_SEND_UPDATE_DATA 發送 [len:4 LE, offset:4 LE, data...]
- 0xC3: COMMAND_RET_UPDATE_DATA 設備進度 [status, offset:4 BE]
- 0xC5: COMMAND_RET_UPDATE_RESULT 設備結果 1 位元組
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ebadge_cli.badge_frame import build_frame
from ebadge_cli.ble_session import (
    BleMode,
    _request_mtu_if_supported,
    accumulate_9e_then_callback,
    find_device,
)


CMD_REQ_UPDATE = 0xC0
CMD_RET_UPDATE = 0xC1
CMD_SEND_UPDATE_DATA = 0xC2
CMD_RET_UPDATE_DATA = 0xC3
CMD_REQ_UPDATE_CON = 0xC4
CMD_RET_UPDATE_RESULT = 0xC5

_SERIAL = 0


def _next_flag(is_long: bool) -> int:
    global _SERIAL
    s = _SERIAL
    if s == 16:
        s = 0
    _SERIAL = s + 1
    return ((s << 3) | (int(is_long) << 2) | 1) & 0xFF


def _build_ota_frame(cmd: int, payload: list[int]) -> list[int]:
    flag = _next_flag(len(payload) + 6 > 20)
    return build_frame(flag=flag, cmd=cmd, payload=payload)


@dataclass
class OtaUpdateResult:
    success: bool
    error: Optional[str] = None
    progress: float = 0.0


async def run_ota_update(
    mode: BleMode,
    name: Optional[str],
    address: Optional[str],
    firmware_bytes: bytes,
    timeout: float = 120.0,
    verbose: bool = False,
    on_progress: Optional[Callable[[float], None]] = None,
) -> OtaUpdateResult:
    """執行 OTA 韌體更新傳輸。"""
    if len(firmware_bytes) < 28:
        return OtaUpdateResult(success=False, error="firmware too small (need >= 28 bytes)")

    header = list(firmware_bytes[:27])
    data = list(firmware_bytes[27:])
    data_total = len(data)

    _log = lambda msg: print(msg) if verbose else None
    device = await find_device(name, address, timeout)
    if device is None:
        return OtaUpdateResult(success=False, error="device not found")

    from bleak import BleakClient

    queue: asyncio.Queue[bytearray] = asyncio.Queue()
    state: dict[str, int] = {"allow_send_length": 0, "offset_position": 0}
    result_holder: list[OtaUpdateResult] = []

    def put_frame(frame: bytearray) -> None:
        queue.put_nowait(frame)

    on_notify = accumulate_9e_then_callback(put_frame)

    def send_data_chunk(offset: int, max_len: int) -> Optional[list[int]]:
        length = min(max_len, data_total - offset)
        if length <= 0:
            return None
        chunk = data[offset : offset + length]
        payload = [
            length & 0xFF,
            (length >> 8) & 0xFF,
            (length >> 16) & 0xFF,
            (length >> 24) & 0xFF,
            offset & 0xFF,
            (offset >> 8) & 0xFF,
            (offset >> 16) & 0xFF,
            (offset >> 24) & 0xFF,
        ] + chunk
        return _build_ota_frame(CMD_SEND_UPDATE_DATA, payload)

    write_char = mode.write_char
    notify_char = mode.notify_char

    try:
        async with BleakClient(device) as client:
            await _request_mtu_if_supported(client)
            await client.start_notify(
                notify_char, lambda _, data: on_notify(bytearray(data))
            )

            # 1. 發送 header
            frame = _build_ota_frame(CMD_REQ_UPDATE, header)
            await client.write_gatt_char(write_char, bytes(frame), response=True)
            _log("sent COMMAND_REQ_UPDATE (header)")

            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    result_holder.append(OtaUpdateResult(success=False, error="timeout"))
                    break

                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    continue

                arr = list(raw)
                if len(arr) < 7 or arr[0] != 0x9E:
                    continue

                cmd = arr[3]
                payload = arr[6:] if len(arr) > 6 else []

                if cmd == CMD_RET_UPDATE and len(payload) >= 9:
                    status = payload[0] & 0xFF
                    state["allow_send_length"] = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
                    state["offset_position"] = (payload[5] << 24) | (payload[6] << 16) | (payload[7] << 8) | payload[8]
                    _log(f"CMD_RET_UPDATE status={status} allow={state['allow_send_length']} offset={state['offset_position']}")

                    if status == 1:
                        frame = send_data_chunk(state["offset_position"], state["allow_send_length"])
                        if frame:
                            await client.write_gatt_char(write_char, bytes(frame), response=True)
                            pct = 100.0 * state["offset_position"] / data_total if data_total else 0
                            if on_progress:
                                on_progress(pct)
                            _log(f"sent chunk offset={state['offset_position']} progress={pct:.1f}%")
                    else:
                        result_holder.append(OtaUpdateResult(success=False, error="device rejected update"))
                        break

                elif cmd == CMD_RET_UPDATE_DATA and len(payload) >= 5:
                    status = payload[0] & 0xFF
                    state["offset_position"] = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
                    _log(f"CMD_RET_UPDATE_DATA status={status} offset={state['offset_position']}")

                    if status == 0:
                        frame = send_data_chunk(state["offset_position"], state["allow_send_length"])
                        if frame:
                            await client.write_gatt_char(write_char, bytes(frame), response=True)
                            pct = 100.0 * state["offset_position"] / data_total if data_total else 0
                            if on_progress:
                                on_progress(pct)
                            _log(f"sent chunk offset={state['offset_position']} progress={pct:.1f}%")
                        elif state["offset_position"] >= data_total:
                            _log("transfer complete, waiting for result")
                        else:
                            result_holder.append(OtaUpdateResult(success=False, error="unexpected offset"))
                            break
                    else:
                        result_holder.append(OtaUpdateResult(success=False, error="device error"))
                        break

                elif cmd == CMD_RET_UPDATE_RESULT and len(payload) >= 1:
                    res = payload[0] & 0xFF
                    _log(f"CMD_RET_UPDATE_RESULT result={res}")
                    if res == 0:
                        result_holder.append(OtaUpdateResult(success=True, progress=100.0))
                    else:
                        result_holder.append(OtaUpdateResult(success=False, error=f"update failed code={res}"))
                    break

            if result_holder:
                return result_holder[-1]
            return OtaUpdateResult(success=False, error="no result")

    except Exception as e:
        return OtaUpdateResult(success=False, error=str(e))
