"""E87 probe：跑完整 RCSP auth 握手，再送後續查詢指令 (bind / battery)。

給 OTA / 電量 用，讓我們不依賴 pre-auth 指令（裝置不接受）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ebadge_cli.ble_session import _load_bleak, accumulate_9e_then_callback
from ebadge_cli.bind_response import BindResponse, parse_bind_response
from ebadge_cli.jl_auth import get_encrypted_auth_data, get_random_auth_data
from ebadge_cli.rcsp_frame import build_e87_frame, parse_e87_frame


async def probe_battery(
    device: Any = None,
    address: Optional[str] = None,
    on_log=None,
) -> Optional[int]:
    """連線 → auth → cmd 0x07 (GetSysInfo) → 解析 AttrBean type=0 取得電量%."""
    log = on_log or (lambda _m: None)
    bleak = _load_bleak()
    BleakClient = bleak.BleakClient
    if device is None and address is None:
        raise ValueError("device or address required")

    log(f"battery probe: connecting (device={'yes' if device else 'no'})")
    client = BleakClient(device) if device is not None else BleakClient(address)
    try:
        await client.connect()
    except Exception as exc:
        log(f"connect failed: {exc}")
        return None
    try:
        # service discovery
        svc_list = []
        for _ in range(20):
            try:
                s = client.services
                svc_list = list(s) if s else []
                if svc_list:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)

        write_char = None
        control_char = None
        notify_chars = []
        for service in svc_list:
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if "ae01" in uuid:
                    write_char = char
                elif "fd02" in uuid:
                    control_char = char
                elif any(x in uuid for x in ("ae02", "fd01", "fd03", "fd05")):
                    props = [p.lower() for p in char.properties]
                    if "notify" in props or "indicate" in props:
                        notify_chars.append(char)
        if not write_char or not control_char or not notify_chars:
            log("characteristics not found"); return None

        # notify queue
        from ebadge_cli.badge_frame import parse_frame as parse_9e
        raw_queue: asyncio.Queue[bytearray] = asyncio.Queue()
        frame_queue: asyncio.Queue = asyncio.Queue()  # 9E frames

        def on_9e_frame(frame: bytearray) -> None:
            parsed = parse_9e(list(frame))
            if parsed is not None:
                frame_queue.put_nowait(parsed)

        inner = accumulate_9e_then_callback(on_9e_frame)

        def notify_cb(_, data: bytearray) -> None:
            raw_queue.put_nowait(bytearray(data))
            inner(bytearray(data))

        for nc in notify_chars:
            try:
                await client.start_notify(nc.uuid, notify_cb)
            except Exception:
                pass

        async def write_ae01(data: bytes) -> None:
            await client.write_gatt_char(write_char.uuid, data, response=False)

        async def write_fd02(data: bytes) -> None:
            await client.write_gatt_char(control_char.uuid, data, response=True)

        async def wait_raw(predicate, timeout_ms: int):
            end = asyncio.get_event_loop().time() + timeout_ms / 1000.0
            while asyncio.get_event_loop().time() < end:
                try:
                    d = await asyncio.wait_for(
                        raw_queue.get(),
                        timeout=end - asyncio.get_event_loop().time(),
                    )
                    if predicate(d):
                        return d
                except asyncio.TimeoutError:
                    break
            raise TimeoutError("predicate not satisfied")

        # AUTH (on ae01)
        log("AUTH: starting...")
        await write_ae01(get_random_auth_data())
        await wait_raw(lambda r: len(r) == 17 and r[0] == 0x01, 5000)
        await write_ae01(bytes([0x02, 0x70, 0x61, 0x73, 0x73]))
        challenge = await wait_raw(lambda r: len(r) == 17 and r[0] == 0x00, 5000)
        await write_ae01(get_encrypted_auth_data(challenge))
        await wait_raw(
            lambda r: len(r) >= 5 and r[0] == 0x02
            and r[1] == 0x70 and r[2] == 0x61 and r[3] == 0x73 and r[4] == 0x73,
            5000,
        )
        log("AUTH SUCCESS")

        # 清空 frame_queue (auth 過程雜訊)
        while not frame_queue.empty():
            try: frame_queue.get_nowait()
            except Exception: break

        # Upload-style bootstrap — 裝置需要這個鋪墊才會 push 狀態
        from datetime import datetime
        log("bootstrap Phase1: cmd 0x06")
        await write_ae01(build_e87_frame(0xC0, 0x06, bytes([0x02, 0x00, 0x01])))
        await asyncio.sleep(0.2)
        try:
            await write_fd02(bytes([0x9E, 0xBD, 0x0B, 0x60, 0x0D, 0x00, 0x03]))
        except Exception: pass
        await asyncio.sleep(0.3)

        log("bootstrap Phase2: time sync")
        now = datetime.now()
        await write_fd02(bytes([0x9E, 0x45, 0x08, 0x02, 0x07, 0x00,
                                now.year & 0xFF, (now.year >> 8) & 0xFF,
                                now.month, now.day, 0x00,
                                now.hour, now.minute]))
        await asyncio.sleep(0.2)

        log("bootstrap Phase3: cmd 0x03 getTargetInfo")
        await write_ae01(build_e87_frame(0xC0, 0x03,
                                          bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])))
        await asyncio.sleep(0.2)
        try:
            await write_fd02(bytes([0x9E, 0xD3, 0x0B, 0xC6, 0x01, 0x00, 0x01]))
        except Exception: pass
        await asyncio.sleep(0.3)

        log("bootstrap Phase4: cmd 0x07 getSysInfo")
        await write_ae01(build_e87_frame(0xC0, 0x07,
                                          bytes([0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])))
        await asyncio.sleep(0.3)

        # 清 queue — 只取 Phase5 之後的 frame (避免抓到 Phase3 的 BadgeInfo C7 被誤判)
        while not frame_queue.empty():
            try: frame_queue.get_nowait()
            except Exception: break

        log("bootstrap Phase5: cmd 0x29 [0x80] → FD02 (trigger state push)")
        await write_fd02(bytes([0x9E, 0xB5, 0x0B, 0x29, 0x01, 0x00, 0x80]))

        # 等 cmd 0x27 battery push
        end = asyncio.get_event_loop().time() + 6.0
        while asyncio.get_event_loop().time() < end:
            try:
                f = await asyncio.wait_for(
                    frame_queue.get(),
                    timeout=end - asyncio.get_event_loop().time(),
                )
                if isinstance(f, dict) and f.get("cmd") == 0x27:
                    pl = f.get("payload", [])
                    if len(pl) >= 2:
                        log(f"電量: {pl[1]}% (mode={pl[0]})")
                        return pl[1]
            except asyncio.TimeoutError:
                break
        log("cmd 0x27 timeout after bootstrap")
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def probe_device(
    device: Any = None,
    address: Optional[str] = None,
    timeout: float = 20.0,
    on_log=None,
) -> Optional[BindResponse]:
    """連線 → auth → cmd 0x60 → 回傳 bind response (含 fw 版本、serial)."""
    log = on_log or (lambda _m: None)

    bleak = _load_bleak()
    BleakClient = bleak.BleakClient
    if device is None and address is None:
        raise ValueError("device or address required")

    log(f"probe: connecting (device={'yes' if device else 'no'}, addr={address})")
    client = BleakClient(device) if device is not None else BleakClient(address)
    try:
        await client.connect()
    except Exception as exc:
        log(f"connect failed: {exc}")
        return None
    log("connected, waiting for service discovery...")
    try:
        # 等 service discovery
        svc_list = []
        for i in range(20):
            try:
                s = client.services
                svc_list = list(s) if s is not None else []
                if svc_list:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        log(f"discovered {len(svc_list)} services")

        # ── 找 characteristics ──
        write_char = None      # AE01 (E87 auth/data)
        control_char = None    # FD02 (9E 格式控制通道)
        notify_chars: list = []
        for service in svc_list:
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if "ae01" in uuid:
                    write_char = char
                elif "fd02" in uuid:
                    control_char = char
                elif any(x in uuid for x in ("ae02", "fd01", "fd03", "fd05")):
                    props = [p.lower() for p in char.properties]
                    if "notify" in props or "indicate" in props:
                        notify_chars.append(char)
        log(f"write_char={write_char.uuid if write_char else None}, "
            f"control_char={control_char.uuid if control_char else None}, "
            f"notify_chars={[c.uuid for c in notify_chars]}")
        if write_char is None:
            log("AE01 write char not found"); return None
        if control_char is None:
            log("FD02 control char not found"); return None
        if not notify_chars:
            log("no notify chars found"); return None

        # ── notify queue ──
        raw_queue: asyncio.Queue[bytearray] = asyncio.Queue()
        frame_queue: asyncio.Queue = asyncio.Queue()  # 9E 格式 frame

        def on_raw(data: bytearray) -> None:
            raw_queue.put_nowait(data)

        from ebadge_cli.badge_frame import parse_frame as parse_9e_frame

        def on_9e_frame(frame: bytearray) -> None:
            # frame 是完整的 9E...payload，轉成 list 給 parse_9e_frame
            parsed = parse_9e_frame(list(frame))
            if parsed is not None:
                frame_queue.put_nowait(parsed)

        inner = accumulate_9e_then_callback(on_9e_frame)

        def notify_cb(_, data: bytearray) -> None:
            on_raw(bytearray(data))
            inner(bytearray(data))

        started = 0
        for nc in notify_chars:
            try:
                await client.start_notify(nc.uuid, notify_cb)
                started += 1
            except Exception as exc:
                log(f"start_notify {nc.uuid} failed: {exc}")
        log(f"started notify on {started}/{len(notify_chars)} chars")

        async def write_ae01(data: bytes) -> None:
            # AE01 是 write-without-response (response=True 會被拒 "Write Not Permitted")
            await client.write_gatt_char(write_char.uuid, data, response=False)

        async def write_fd02(data: bytes) -> None:
            # FD02 是 9E 格式控制通道，支援 write-with-response
            await client.write_gatt_char(control_char.uuid, data, response=True)

        async def wait_raw(predicate, timeout_ms: int) -> bytearray:
            end = asyncio.get_event_loop().time() + timeout_ms / 1000.0
            while asyncio.get_event_loop().time() < end:
                try:
                    data = await asyncio.wait_for(raw_queue.get(),
                                                   timeout=end - asyncio.get_event_loop().time())
                    if predicate(data):
                        return data
                except asyncio.TimeoutError:
                    break
            raise TimeoutError("predicate not satisfied")

        async def wait_frame(predicate, timeout_ms: int):
            end = asyncio.get_event_loop().time() + timeout_ms / 1000.0
            while asyncio.get_event_loop().time() < end:
                try:
                    f = await asyncio.wait_for(frame_queue.get(),
                                                timeout=end - asyncio.get_event_loop().time())
                    if f is not None and predicate(f):
                        return f
                except asyncio.TimeoutError:
                    break
            raise TimeoutError("frame predicate not satisfied")

        # ── AUTH ──
        log("AUTH: starting handshake...")
        try:
            await write_ae01(get_random_auth_data())
        except Exception as exc:
            log(f"AUTH TX1 write failed: {exc}")
            return None

        try:
            device_response = await wait_raw(
                lambda raw: len(raw) == 17 and raw[0] == 0x01, 5000)
        except TimeoutError:
            log("AUTH RX1 timeout: no [0x01, ...] response from device")
            return None
        log(f"AUTH RX device response")

        await write_ae01(bytes([0x02, 0x70, 0x61, 0x73, 0x73]))
        device_challenge = await wait_raw(
            lambda raw: len(raw) == 17 and raw[0] == 0x00, 5000)
        log(f"AUTH RX challenge")

        await write_ae01(get_encrypted_auth_data(device_challenge))
        await wait_raw(
            lambda raw: len(raw) >= 5 and raw[0] == 0x02
            and raw[1] == 0x70 and raw[2] == 0x61
            and raw[3] == 0x73 and raw[4] == 0x73, 5000)
        log("AUTH SUCCESS")

        # ── Post-auth: 複製上傳的 sequence 到 bind 觸發 ──
        # 1) cmd 0x06 via AE01 (E87 格式) reset auth flag
        log("TX cmd=0x06 (reset auth)")
        try:
            await write_ae01(build_e87_frame(0xC0, 0x06, bytes([0x02, 0x00, 0x01])))
        except Exception as exc:
            log(f"cmd 0x06 write failed: {exc}")

        await asyncio.sleep(0.15)

        # 2) 完整 bind frame (9E 格式) via FD02
        # payload: [header(lang|hour12), dev_id*6, dev_id*6] = 13 bytes
        import os as _os
        from ebadge_cli.badge_frame import build_frame as build_9e_frame
        dev_id = _os.urandom(6)
        header = 0x00  # lang=zh hour=24
        payload_bytes = [header] + list(dev_id) + list(dev_id)
        # flag: 參考 CLI _next_flag (短 payload 用 0xC0 / 0xBD)
        frame_bind = build_9e_frame(flag=0xBD, cmd=0x60, payload=payload_bytes)
        log(f"TX bind frame ({len(frame_bind)} bytes) → FD02")
        try:
            await write_fd02(bytes(frame_bind))
        except Exception as exc:
            log(f"FD02 bind write failed: {exc}")
            return None

        # 3) 等 9E frame cmd == 0x61 (同時 dump 收到的任何 frame 方便 debug)
        async def wait_bind_response(timeout_ms: int):
            end = asyncio.get_event_loop().time() + timeout_ms / 1000.0
            seen: list[int] = []
            while asyncio.get_event_loop().time() < end:
                try:
                    f = await asyncio.wait_for(
                        frame_queue.get(),
                        timeout=end - asyncio.get_event_loop().time(),
                    )
                    if isinstance(f, dict):
                        seen.append(f.get("cmd", -1))
                        if f.get("cmd") == 0x61:
                            return f
                except asyncio.TimeoutError:
                    break
            log(f"沒收到 0x61，這段時間收到的 cmd: {[hex(c) for c in seen]}")
            raise TimeoutError("no 0x61")

        try:
            response = await wait_bind_response(8000)
        except TimeoutError:
            log("cmd 0x61 bind response timeout")
            return None

        payload = response.get("payload", [])
        log(f"0x61 payload ({len(payload)} bytes): " +
            " ".join(f"{b:02x}" for b in payload))
        bind = parse_bind_response(payload)
        if bind is None:
            log(f"bind parse failed (payload too short, need 39 bytes, got {len(payload)})")
            # 試著用較寬鬆的最小長度解析 (某些韌體版本 payload 較短)
            if len(payload) >= 18:
                try:
                    from ebadge_cli.bind_response import (
                        BindResponse, _bytes_to_int, _bytes_to_string,
                    )
                    state = payload[0]
                    pact = _bytes_to_string(payload, 1, 3)
                    fw = _bytes_to_string(payload, 4, 10)
                    platform = _bytes_to_int(payload, 14, 4) if len(payload) >= 18 else 0
                    serial = _bytes_to_int(payload, 18, 4) if len(payload) >= 22 else 0
                    bind = BindResponse(
                        state=state, pact_version=pact, firmwa_version=fw,
                        platform=platform, serial_number=serial,
                        function_config=0, function_config1=0, function_config2=0,
                        ui_version="", function_bytes=payload[22:] if len(payload) > 22 else [],
                    )
                    log(f"partial parse: fw={fw} serial={serial} platform={platform}")
                except Exception as exc:
                    log(f"partial parse also failed: {exc}")
        else:
            log(f"bind: fw={bind.firmwa_version} serial={bind.serial_number}")
        return bind
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
