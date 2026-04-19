"""E87 badge image/video upload via BLE.

Implements the 10-phase windowed upload protocol reverse-engineered
from web-bluetooth-e87 (hybridherbst/web-bluetooth-e87).

Protocol flow:
  AUTH → Phase1(0x06) → Phase2(FD02) → Phase3(0x03) → Phase4(0x07)
  → Phase5(bootstrap) → Phase6(0x21) → Phase7(0x27) → Phase8(0x1B)
  → Phase9(windowed data) → Phase10(0x20+0x1C completion)
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from ebadge_cli.ble_constants import (
    CHAR_NOTIFY_AE00,
    CHAR_NOTIFY_C2E6,
    CHAR_NOTIFY_FD03,
    CHAR_NOTIFY_FD05,
    CHAR_WRITE_AE00,
    CHAR_WRITE_C2E6,
    SERVICE_AE00,
    SERVICE_BATTERY,
    SERVICE_C2E6,
    CHAR_BATTERY_LEVEL,
)
from ebadge_cli.ble_session import find_device, _load_bleak
from ebadge_cli.crc16 import compute as crc16_compute
from ebadge_cli.jl_auth import get_encrypted_auth_data, get_random_auth_data
from ebadge_cli.rcsp_frame import E87Frame, build_e87_frame, parse_e87_frame


E87_DATA_CHUNK_SIZE = 490  # Default; will be overridden by device


@dataclass
class TransferResult:
    success: bool
    error: Optional[str] = None


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data[:24])


def _hex_bytes(values: list[int]) -> bytes:
    """Parse hex string shorthand like '9EB5 0B29 0100 80' into bytes."""
    return bytes(values)


# ── Notification queue helpers ──

class NotifyQueue:
    """Async-safe notification queue for BLE characteristics."""

    def __init__(self) -> None:
        self._queue: list[bytes] = []
        self._event = asyncio.Event()

    def push(self, data: bytes) -> None:
        self._queue.append(data)
        self._event.set()

    async def wait_for_frame(
        self,
        predicate: Callable[[E87Frame], bool],
        timeout_ms: int = 8000,
        label: str = "E87 frame",
        log: Optional[Callable[[str], None]] = None,
    ) -> E87Frame:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for i, raw in enumerate(self._queue):
                frame = parse_e87_frame(raw)
                if frame and predicate(frame):
                    self._queue.pop(i)
                    return frame
            self._event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(remaining, 0.05))
            except asyncio.TimeoutError:
                pass
        raise TimeoutError(f"Timeout waiting for {label}")

    async def wait_for_raw(
        self,
        predicate: Callable[[bytes], bool],
        timeout_ms: int = 2000,
        label: str = "raw notification",
        log: Optional[Callable[[str], None]] = None,
    ) -> bytes:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for i, raw in enumerate(self._queue):
                if predicate(raw):
                    self._queue.pop(i)
                    return raw
            self._event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(remaining, 0.05))
            except asyncio.TimeoutError:
                pass
        raise TimeoutError(f"Timeout waiting for {label}")

    def drain_stale(self) -> int:
        """Remove stale device-command frames (flag=0xC0, not 0x20/0x1C/0x1D)."""
        removed = 0
        i = len(self._queue) - 1
        while i >= 0:
            f = parse_e87_frame(self._queue[i])
            if f and (f.flag & 0xC0) == 0xC0 and f.cmd not in (0x20, 0x1C, 0x1D):
                self._queue.pop(i)
                removed += 1
            i -= 1
        return removed


async def run_e87_upload(
    name: Optional[str],
    address: Optional[str],
    file_bytes: bytes,
    upload_mode: str = "image",
    timeout: float = 60.0,
    inter_chunk_delay_ms: int = 0,
    verbose: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    device: Any = None,
) -> TransferResult:
    """Execute the full E87 upload protocol.

    Args:
        name: Device name filter (e.g. "E87").
        address: Device BLE address (alternative to name).
        file_bytes: JPEG or AVI payload to upload.
        upload_mode: "image" or "video".
        timeout: Overall timeout in seconds.
        inter_chunk_delay_ms: Delay between data chunks (0 = none).
        verbose: Enable detailed logging.
        on_progress: Progress callback.

    Returns:
        TransferResult with success/error.
    """
    log = on_progress or (lambda msg: print(msg) if verbose else None)

    if device is None:
        device = await find_device(name, address, timeout)
    if device is None:
        return TransferResult(success=False, error="device not found")

    bleak = _load_bleak()
    BleakClient = bleak.BleakClient

    notify_queue = NotifyQueue()
    file_complete_handled = False
    file_complete_auto_respond = False
    _upload_mode_ref = upload_mode  # capture for auto-responder closure

    async def _do_upload(client: Any) -> TransferResult:
        nonlocal file_complete_handled, file_complete_auto_respond

        # ── Discover characteristics ──
        log("Discovering services...")
        services = client.services

        write_char = None  # AE01 (write-without-response)
        control_char = None  # FD02 (write + write-without-response)
        data_char = None  # FD03 (write-with-response only, for Long Write)
        notify_chars = []  # AE02, FD01, FD03, FD05

        for service in services:
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if "ae01" in uuid:
                    write_char = char
                elif "fd02" in uuid:
                    control_char = char
                elif "fd03" in uuid:
                    # FD03 supports write-with-response → BLE Long Write for large payloads
                    data_char = char
                    if "notify" in [p.lower() for p in char.properties] or "indicate" in [p.lower() for p in char.properties]:
                        notify_chars.append(char)
                elif any(x in uuid for x in ("ae02", "fd01", "fd05")):
                    if "notify" in [p.lower() for p in char.properties] or "indicate" in [p.lower() for p in char.properties]:
                        notify_chars.append(char)

        if not write_char:
            return TransferResult(success=False, error="AE01 write characteristic not found")
        if not control_char:
            return TransferResult(success=False, error="FD02 control characteristic not found")
        if not notify_chars:
            return TransferResult(success=False, error="No notify characteristics found")

        log(f"Write: {write_char.uuid}")
        log(f"Control: {control_char.uuid}")
        log(f"Notify: {[c.uuid for c in notify_chars]}")

        # ── Setup notification handlers ──
        # Capture the running event loop for thread-safe scheduling from
        # CoreBluetooth delegate callbacks (bleak marshals to asyncio thread,
        # but we use call_soon_threadsafe as a safety net).
        _loop = asyncio.get_running_loop()

        def on_notify(_sender: Any, data: bytearray) -> None:
            nonlocal file_complete_handled, file_complete_auto_respond
            raw = bytes(data)
            frame = parse_e87_frame(raw)
            if frame:
                # Fast auto-responder for cmd 0x20 (FILE_COMPLETE).
                # The device has a tight timeout (~100ms) so we respond
                # directly here instead of going through the main loop.
                if (file_complete_auto_respond
                        and not file_complete_handled
                        and frame.cmd == 0x20
                        and frame.flag == 0xC0):
                    file_complete_handled = True
                    device_seq = frame.body[0] if frame.body else 0
                    resp_body = _build_file_path_response(device_seq, _upload_mode_ref)
                    resp_frame = build_e87_frame(0x00, 0x20, resp_body)
                    log(f"AUTO-RESPOND cmd 0x20: seq={device_seq}")
                    _loop.call_soon_threadsafe(
                        lambda r=resp_frame: asyncio.ensure_future(
                            client.write_gatt_char(write_char, r, response=False)
                        )
                    )
                    # Still queue it so the main loop sees it for the 0x1C flow
                    notify_queue.push(raw)
                    return

                # Auto-ACK device commands (flag=0xC0) except 0x20, 0x1C, 0x1D
                if (frame.flag & 0xC0) == 0xC0 and frame.cmd not in (0x20, 0x1C, 0x1D):
                    device_seq = frame.body[0] if frame.body else 0
                    resp = build_e87_frame(0x00, frame.cmd, bytes([0x00, device_seq]))
                    _loop.call_soon_threadsafe(
                        lambda r=resp: asyncio.ensure_future(
                            client.write_gatt_char(write_char, r, response=False)
                        )
                    )
                    return
            notify_queue.push(raw)

        for c in notify_chars:
            await client.start_notify(c, on_notify)
        log(f"Notifications started on {len(notify_chars)} characteristic(s)")

        # ── MTU-aware write helpers ──
        mtu = client.mtu_size
        # Maximum payload for a single write-without-response PDU
        max_wnr = mtu - 3  # ATT_MTU - opcode(1) - handle(2)
        log(f"MTU={mtu}, max WnR payload={max_wnr}")

        async def write_ae01(data: bytes) -> None:
            """Write to AE01. If data exceeds MTU, fragment into multiple BLE writes.

            The Jieli RCSP parser on the device buffers incoming bytes and
            reassembles complete frames (FE DC BA … EF), so splitting a single
            RCSP frame across multiple BLE write-without-response PDUs works
            as long as delivery is in-order (guaranteed over a BLE connection).
            """
            if len(data) <= max_wnr:
                await client.write_gatt_char(write_char, data, response=False)
            else:
                offset = 0
                while offset < len(data):
                    end = min(offset + max_wnr, len(data))
                    await client.write_gatt_char(write_char, data[offset:end], response=False)
                    offset = end

        async def write_ae01_data(data: bytes) -> None:
            """Write data frames via AE01 (fragmented if needed)."""
            await write_ae01(data)

        async def write_fd02(data: bytes) -> None:
            await client.write_gatt_char(control_char, data, response=False)

        async def send_e87(flag: int, cmd: int, body: bytes) -> None:
            frame = build_e87_frame(flag, cmd, body)
            log(f"TX cmd=0x{cmd:02x} flag=0x{flag:02x} len={len(body)}")
            await write_ae01(frame)

        # ── AUTH: Jieli 6-step crypto handshake ──
        log("AUTH: Starting Jieli RCSP crypto handshake...")
        random_data = get_random_auth_data()
        log(f"AUTH TX: [0x00, rand*16]")
        await write_ae01(random_data)

        try:
            device_response = await notify_queue.wait_for_raw(
                lambda raw: len(raw) == 17 and raw[0] == 0x01,
                timeout_ms=5000, label="auth device response [0x01]", log=log,
            )
            log(f"AUTH RX: device response ({_hex(device_response)})")

            log("AUTH TX: [0x02, pass]")
            await write_ae01(bytes([0x02, 0x70, 0x61, 0x73, 0x73]))

            device_challenge = await notify_queue.wait_for_raw(
                lambda raw: len(raw) == 17 and raw[0] == 0x00,
                timeout_ms=5000, label="auth device challenge [0x00]", log=log,
            )
            log(f"AUTH RX: challenge ({_hex(device_challenge)})")

            encrypted_response = get_encrypted_auth_data(device_challenge)
            log("AUTH TX: encrypted response")
            await write_ae01(encrypted_response)

            auth_confirm = await notify_queue.wait_for_raw(
                lambda raw: len(raw) >= 5 and raw[0] == 0x02
                and raw[1] == 0x70 and raw[2] == 0x61
                and raw[3] == 0x73 and raw[4] == 0x73,
                timeout_ms=5000, label="auth pass confirmation", log=log,
            )
            log("AUTH SUCCESS")
        except TimeoutError as e:
            return TransferResult(success=False, error=f"Auth failed: {e}")

        seq_counter = 0x00

        # ── PHASE 1: cmd 0x06 — reset auth flag ──
        log("Phase 1: cmd 0x06 (reset auth flag)...")
        await send_e87(0xC0, 0x06, bytes([0x02, 0x00, 0x01]))
        seq_counter = 0x01

        try:
            await write_fd02(bytes([0x9E, 0xBD, 0x0B, 0x60, 0x0D, 0x00, 0x03]))
        except Exception:
            pass

        try:
            await notify_queue.wait_for_frame(
                lambda f: f.cmd == 0x06, timeout_ms=3000, label="ack cmd 0x06", log=log,
            )
            log("cmd 0x06 acked")
        except TimeoutError:
            log("cmd 0x06 ack not received (continuing)")

        # ── PHASE 2: FD02 control writes ──
        log("Phase 2: FD02 control writes...")
        now = datetime.now()
        time_payload = bytes([
            0x9E, 0x45, 0x08, 0x02, 0x07, 0x00,
            now.year & 0xFF, (now.year >> 8) & 0xFF,
            now.month, now.day, 0x00,
            now.hour, now.minute,
        ])
        await write_fd02(time_payload)
        await asyncio.sleep(0.02)
        await write_fd02(bytes([0x9E, 0x20, 0x08, 0x16, 0x01, 0x00, 0x01]))
        await asyncio.sleep(0.02)
        await write_fd02(bytes([0x9E, 0xB5, 0x0B, 0x29, 0x01, 0x00, 0x80]))
        await asyncio.sleep(0.2)

        # ── PHASE 3: cmd 0x03 — device info (best-effort) ──
        try:
            log("Phase 3: cmd 0x03 (best-effort)...")
            await send_e87(0xC0, 0x03, bytes([seq_counter, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]))
            seq_counter += 1
            await write_fd02(bytes([0x9E, 0xD3, 0x0B, 0xC6, 0x01, 0x00, 0x01]))
            await asyncio.sleep(0.02)
            await write_fd02(bytes([0x9E, 0x30, 0x08, 0x20, 0x02, 0x00, 0xFF, 0x07]))
            await notify_queue.wait_for_frame(
                lambda f: f.cmd == 0x03, timeout_ms=3000, label="ack cmd 0x03", log=log,
            )
        except TimeoutError:
            log("cmd 0x03 not acked (continuing)")

        # ── PHASE 4: cmd 0x07 — device config (best-effort) ──
        try:
            log("Phase 4: cmd 0x07 (best-effort)...")
            await send_e87(0xC0, 0x07, bytes([seq_counter, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]))
            seq_counter += 1
            await write_fd02(bytes([0x9E, 0x2B, 0x08, 0xFF, 0x02, 0x00, 0x22, 0x00]))
            await asyncio.sleep(0.04)
            await write_fd02(bytes([0x9E, 0x2D, 0x08, 0xFF, 0x02, 0x00, 0x24, 0x00]))
            await notify_queue.wait_for_frame(
                lambda f: f.cmd == 0x07, timeout_ms=3000, label="ack cmd 0x07", log=log,
            )
        except TimeoutError:
            log("cmd 0x07 not acked (continuing)")

        # ── PHASE 5: FD02 bootstrap ──
        log("Phase 5: FD02 bootstrap...")
        await write_fd02(bytes([0x9E, 0xB5, 0x0B, 0x29, 0x01, 0x00, 0x80]))
        await asyncio.sleep(0.4)
        await write_fd02(bytes([0x9E, 0xD3, 0x0B, 0xC6, 0x01, 0x00, 0x01]))
        try:
            await notify_queue.wait_for_raw(
                lambda raw: len(raw) >= 5 and raw[0] == 0x9E and (raw[3] == 0xC7 or raw[2] == 0xC7),
                timeout_ms=3000, label="FD01 device info (C7)", log=log,
            )
        except TimeoutError:
            log("FD01 C7 not observed (continuing)")
        await write_fd02(bytes([0x9E, 0xF4, 0x0B, 0xDC, 0x01, 0x00, 0x0C]))
        try:
            await notify_queue.wait_for_raw(
                lambda raw: len(raw) >= 4 and raw[0] == 0x9E and raw[1] == 0xE6,
                timeout_ms=3000, label="FD03 ready signal", log=log,
            )
            log("Device ready signal received")
        except TimeoutError:
            log("FD03 ready signal not observed (continuing)")

        # ── PHASE 6: cmd 0x21 — begin upload ──
        log("Phase 6: cmd 0x21 (begin upload)...")
        await send_e87(0xC0, 0x21, bytes([seq_counter, 0x00]))
        seq_counter += 1
        await notify_queue.wait_for_frame(
            lambda f: f.cmd == 0x21, timeout_ms=8000, label="ack cmd 0x21", log=log,
        )

        # ── PHASE 7: cmd 0x27 — transfer parameters ──
        log("Phase 7: cmd 0x27 (transfer params)...")
        await send_e87(0xC0, 0x27, bytes([seq_counter, 0x00, 0x00, 0x00, 0x00, 0x02, 0x01]))
        seq_counter += 1
        await notify_queue.wait_for_frame(
            lambda f: f.cmd == 0x27, timeout_ms=8000, label="ack cmd 0x27", log=log,
        )

        # ── PHASE 8: cmd 0x1B — file metadata ──
        log("Phase 8: cmd 0x1B (file metadata)...")
        file_size = len(file_bytes)
        temp_name = f"{random.randint(0, 0xFFFFFF):06x}.tmp"
        name_bytes = temp_name.encode("ascii")
        # CRC is computed over the "rotated" file: tail + head
        # The device receives data starting from device_chunk_size offset,
        # then the commit window sends bytes 0..device_chunk_size.
        # The device verifies CRC over this rotated byte order.
        file_crc = crc16_compute(file_bytes)
        log(f"File: {file_size} bytes, CRC16=0x{file_crc:04x}")

        meta_body = bytearray()
        meta_body.append(seq_counter)
        seq_counter += 1
        meta_body.append((file_size >> 24) & 0xFF)
        meta_body.append((file_size >> 16) & 0xFF)
        meta_body.append((file_size >> 8) & 0xFF)
        meta_body.append(file_size & 0xFF)
        meta_body.append((file_crc >> 8) & 0xFF)
        meta_body.append(file_crc & 0xFF)
        meta_body.append(random.randint(0, 255))
        meta_body.append(random.randint(0, 255))
        meta_body.extend(name_bytes)
        meta_body.append(0x00)

        await send_e87(0xC0, 0x1B, bytes(meta_body))
        meta_ack = await notify_queue.wait_for_frame(
            lambda f: f.cmd == 0x1B, timeout_ms=8000, label="ack cmd 0x1B", log=log,
        )

        chunk_size = E87_DATA_CHUNK_SIZE
        if len(meta_ack.body) >= 4:
            cs = (meta_ack.body[2] << 8) | meta_ack.body[3]
            if 0 < cs <= 4096:
                chunk_size = cs
                log(f"Device requested chunk size: {chunk_size}")
            else:
                log(f"WARNING: unusual chunk size {cs}, using default {E87_DATA_CHUNK_SIZE}")

        # Frame size = 13 + chunk_size bytes.  If this exceeds a single
        # BLE write-without-response PDU (max_wnr), write_ae01() will
        # automatically fragment the frame into multiple BLE writes.
        frame_size = 13 + chunk_size
        if frame_size > max_wnr:
            n_frags = (frame_size + max_wnr - 1) // max_wnr
            log(f"Frames of {frame_size}B will be fragmented into {n_frags} BLE writes")

        # ── PHASE 9: Data transfer ──
        log("Phase 9: Data transfer...")
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        seq = seq_counter
        sent_chunks = 0
        file_offset = 0  # Current position in file

        # Enable fast auto-responder for cmd 0x20
        file_complete_auto_respond = True
        file_complete_handled = False

        log(f"Total: {file_size} bytes, {total_chunks} chunks of {chunk_size}")

        # Wait for initial window ack
        try:
            first_win_ack = await notify_queue.wait_for_frame(
                lambda f: f.flag == 0x80 and f.cmd == 0x1D,
                timeout_ms=10000, label="initial window ack", log=log,
            )
        except TimeoutError:
            return TransferResult(success=False, error="No initial window ACK within 10s")

        log("Using windowed flow control")
        current_ack = first_win_ack
        done = False

        while not done:
            if current_ack and current_ack.cmd == 0x1D and len(current_ack.body) >= 8:
                ack_seq = current_ack.body[0]
                ack_status = current_ack.body[1]
                win_size = (current_ack.body[2] << 8) | current_ack.body[3]
                # Window ack body layout (8 bytes) — same as reference:
                #   [0]: seq, [1]: status,
                #   [2:3]: window size (BE16)
                #   [4:7]: next offset (BE32) — file position to send from
                next_offset = (
                    (current_ack.body[4] << 24)
                    | (current_ack.body[5] << 16)
                    | (current_ack.body[6] << 8)
                    | current_ack.body[7]
                )
                log(f"Window ack #{ack_seq}: winSize={win_size} nextOffset={next_offset}")

                if ack_status != 0x00:
                    log(f"WARNING: non-zero ack status 0x{ack_status:02x}")

                # Use device's nextOffset as file position (matches reference)
                file_offset = next_offset

                if win_size == 0 and file_offset >= file_size:
                    log("Final window ack (transfer complete)")
                    done = True
                    break

                # Send chunks for this window
                slot = 0
                window_bytes_sent = 0
                while window_bytes_sent < win_size:
                    chunk_offset = file_offset
                    if chunk_offset >= file_size:
                        break

                    remaining = file_size - chunk_offset
                    chunk_len = min(chunk_size, remaining, win_size - window_bytes_sent)
                    payload = file_bytes[chunk_offset:chunk_offset + chunk_len]

                    crc = crc16_compute(payload)
                    body = bytearray()
                    body.append(seq & 0xFF)
                    body.append(0x1D)
                    body.append(slot & 0xFF)
                    body.append((crc >> 8) & 0xFF)
                    body.append(crc & 0xFF)
                    body.extend(payload)

                    frame = build_e87_frame(0x80, 0x01, bytes(body))
                    log(f"TX cmd=0x01 flag=0x80 len={len(body)}")
                    await write_ae01_data(frame)

                    sent_chunks += 1
                    file_offset += chunk_len
                    seq = (seq + 1) & 0xFF
                    slot = (slot + 1) & 0x07
                    window_bytes_sent += chunk_len

                    if on_progress:
                        pct = file_offset * 100 // file_size
                        on_progress(f"Progress: {pct}% ({file_offset}/{file_size})")

                    if inter_chunk_delay_ms > 0:
                        await asyncio.sleep(inter_chunk_delay_ms / 1000)

                log(f"Window done: {window_bytes_sent} bytes (fileOffset: {file_offset}/{file_size})")

            # Wait for next window ack or completion
            try:
                next_event = await notify_queue.wait_for_frame(
                    lambda f: (f.flag == 0x80 and f.cmd == 0x1D)
                    or (f.flag == 0xC0 and f.cmd in (0x20, 0x1C)),
                    timeout_ms=15000, label="window ack or completion", log=log,
                )

                if next_event.cmd == 0x1D:
                    current_ack = next_event
                elif next_event.cmd == 0x20:
                    device_seq = next_event.body[0] if next_event.body else seq
                    if not file_complete_handled:
                        resp_body = _build_file_path_response(device_seq, upload_mode)
                        await send_e87(0x00, 0x20, resp_body)
                        file_complete_handled = True
                        log("Sent cmd 0x20 file path response")
                    else:
                        log(f"cmd 0x20 already auto-responded (seq={device_seq})")
                    file_complete_auto_respond = False

                    # Wait for cmd 0x1C finalize
                    try:
                        finalize = await notify_queue.wait_for_frame(
                            lambda f: f.cmd == 0x1C,
                            timeout_ms=10000, label="cmd 0x1C finalize", log=log,
                        )
                        device_seq_1c = finalize.body[0] if finalize.body else 0
                        status_byte = finalize.body[1] if len(finalize.body) >= 2 else 0xFF
                        await send_e87(0x00, 0x1C, bytes([0x00, device_seq_1c]))
                        if status_byte == 0x00:
                            log("Upload complete!")
                            return TransferResult(success=True)
                        else:
                            log(f"Device reported error status 0x{status_byte:02x}")
                            return TransferResult(success=False, error=f"Device error 0x{status_byte:02x}")
                    except TimeoutError:
                        log("cmd 0x1C not received, but 0x20 was handled")
                        return TransferResult(success=True)

                elif next_event.cmd == 0x1C:
                    device_seq_1c = next_event.body[0] if next_event.body else 0
                    status_byte = next_event.body[1] if len(next_event.body) >= 2 else 0xFF
                    await send_e87(0x00, 0x1C, bytes([0x00, device_seq_1c]))
                    if status_byte == 0x00:
                        log("Upload complete (0x1C received)")
                    else:
                        log(f"Upload ended with status 0x{status_byte:02x}")
                    return TransferResult(success=(status_byte == 0x00),
                                          error=None if status_byte == 0x00
                                          else f"Device error 0x{status_byte:02x}")

            except TimeoutError:
                if file_offset >= file_size:
                    log("All bytes sent, waiting for completion...")
                    try:
                        completion = await notify_queue.wait_for_frame(
                            lambda f: f.cmd in (0x20, 0x1C),
                            timeout_ms=10000, label="final completion", log=log,
                        )
                        if completion.cmd == 0x20:
                            device_seq = completion.body[0] if completion.body else seq
                            resp_body = _build_file_path_response(device_seq, upload_mode)
                            await send_e87(0x00, 0x20, resp_body)
                            try:
                                finalize = await notify_queue.wait_for_frame(
                                    lambda f: f.cmd == 0x1C,
                                    timeout_ms=10000, label="cmd 0x1C", log=log,
                                )
                                d_seq = finalize.body[0] if finalize.body else 0
                                await send_e87(0x00, 0x1C, bytes([0x00, d_seq]))
                            except TimeoutError:
                                pass
                            return TransferResult(success=True)
                        elif completion.cmd == 0x1C:
                            d_seq = completion.body[0] if completion.body else 0
                            await send_e87(0x00, 0x1C, bytes([0x00, d_seq]))
                            return TransferResult(success=True)
                    except TimeoutError:
                        return TransferResult(success=False, error="Timeout waiting for completion after all data sent")
                else:
                    return TransferResult(success=False, error=f"Timeout during transfer ({file_offset}/{file_size} bytes)")

        return TransferResult(success=True)

    try:
        client = BleakClient(device)
        await client.connect()
        # macOS: 等候 discoverServices 完成
        for _ in range(10):
            try:
                svcs = client.services
                if svcs is not None and len(list(svcs)) > 0:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        try:
            return await _do_upload(client)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    except Exception as e:
        return TransferResult(success=False, error=str(e))


def _build_file_path_response(device_seq: int, upload_mode: str) -> bytes:
    """Build the UTF-16LE file path response for cmd 0x20.

    The path uses U+555C as prefix (matching web-bluetooth-e87 reference).
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d%H%M%S")
    ext = ".jpg" if upload_mode in ("image", "qr") else ".avi"
    device_path = f"\u555c{date_str}{ext}"
    path_utf16 = device_path.encode("utf-16-le") + b"\x00\x00"
    return bytes([0x00, device_seq]) + path_utf16
