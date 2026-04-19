"""E87 badge 檔案瀏覽功能。

使用 RCSP 協議的 opCode 0x0C (StartFileBrowse) 和 0x0D (StopFileBrowse)
列舉裝置存儲上的檔案。

Protocol flow:
  1. Connect + AUTH
  2. Send cmd 0x0C with path payload → device returns file count
  3. Device sends data frames (opCode 0x01, xmOpCode 0x0C) with file entries
  4. Device sends cmd 0x0D when done
  5. Parse accumulated file entry bytes
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from ebadge_cli.ble_session import find_device, _load_bleak
from ebadge_cli.jl_auth import get_encrypted_auth_data, get_random_auth_data
from ebadge_cli.rcsp_frame import (
    RCSPPacket,
    build_command,
    build_response,
    parse,
)


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data[:32])


def _parse_file_entries(data: list[int]) -> list[dict[str, object]]:
    """Parse accumulated file entry bytes into file info dicts."""
    entries: list[dict[str, object]] = []
    offset = 0
    while offset < len(data):
        if offset + 7 >= len(data):
            break
        header = data[offset]
        is_file = (header & 0x01) != 0
        is_unicode = (header & 0x02) == 0  # bit1=0 means unicode
        dev_index = (header >> 2) & 0x1F

        cluster = (
            (data[offset + 1] << 24)
            | (data[offset + 2] << 16)
            | (data[offset + 3] << 8)
            | data[offset + 4]
        )
        file_num = (data[offset + 5] << 8) | data[offset + 6]
        name_len = data[offset + 7]
        offset += 8

        if offset + name_len > len(data):
            break

        name_bytes = bytes(data[offset : offset + name_len])
        offset += name_len

        if is_unicode:
            try:
                name = name_bytes.decode("utf-16-le").rstrip("\x00")
            except UnicodeDecodeError:
                name = name_bytes.hex()
        else:
            try:
                name = name_bytes.decode("ascii").rstrip("\x00")
            except UnicodeDecodeError:
                name = name_bytes.hex()

        entries.append({
            "name": name,
            "is_file": is_file,
            "cluster": cluster,
            "file_num": file_num,
            "dev_index": dev_index,
        })

    return entries


async def run_file_browse(
    name: Optional[str],
    address: Optional[str],
    timeout: float = 30.0,
    verbose: bool = False,
) -> list[dict[str, object]]:
    """Connect to E87 badge and list files on device storage."""
    log = print if verbose else lambda *_: None

    device = await find_device(name, address, timeout)
    if device is None:
        raise RuntimeError("device not found")

    bleak = _load_bleak()
    BleakClient = bleak.BleakClient

    received_packets: list[bytes] = []
    event = asyncio.Event()

    def on_notify(_sender: Any, data: bytearray) -> None:
        received_packets.append(bytes(data))
        event.set()

    async def wait_packet(
        predicate: Callable[[RCSPPacket], bool],
        timeout_ms: int = 8000,
        label: str = "packet",
    ) -> RCSPPacket:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for i, raw in enumerate(received_packets):
                pkt = parse(list(raw))
                if pkt and predicate(pkt):
                    received_packets.pop(i)
                    return pkt
            event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.1))
            except asyncio.TimeoutError:
                pass
        raise TimeoutError(f"Timeout waiting for {label}")

    async def collect_data_packets(
        xm_op: int, timeout_ms: int = 15000
    ) -> list[int]:
        """Collect opCode 0x01 data packets until 0x0D stop."""
        all_data: list[int] = []
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for i, raw in enumerate(received_packets):
                pkt = parse(list(raw))
                if not pkt:
                    continue
                if pkt.op_code == 0x0D:
                    received_packets.pop(i)
                    sn = pkt.op_code_sn
                    resp = build_response(0x0D, sn, 0x00, [])
                    await write_ae01(bytes(resp))
                    log(f"StopFileBrowse (0x0D) received, sent ACK")
                    return all_data
                if pkt.op_code == 0x01 and pkt.xm_op_code == xm_op:
                    received_packets.pop(i)
                    all_data.extend(pkt.payload)
                    sn = pkt.op_code_sn
                    resp = build_response(0x01, sn, 0x00, [], xm_op_code=xm_op)
                    await write_ae01(bytes(resp))
                    break
            else:
                event.clear()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.1))
                except asyncio.TimeoutError:
                    pass
        return all_data

    async with BleakClient(device) as client:
        write_char = None
        notify_chars = []
        for service in client.services:
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if "ae01" in uuid:
                    write_char = char
                elif any(x in uuid for x in ("ae02", "fd01", "fd03", "fd05")):
                    props = [p.lower() for p in char.properties]
                    if "notify" in props or "indicate" in props:
                        notify_chars.append(char)

        if not write_char:
            raise RuntimeError("AE01 write characteristic not found")

        mtu = client.mtu_size
        max_wnr = mtu - 3

        async def write_ae01(data: bytes) -> None:
            if len(data) <= max_wnr:
                await client.write_gatt_char(write_char, data, response=False)
            else:
                off = 0
                while off < len(data):
                    end = min(off + max_wnr, len(data))
                    await client.write_gatt_char(write_char, data[off:end], response=False)
                    off = end

        for c in notify_chars:
            await client.start_notify(c, on_notify)
        log(f"Connected, {len(notify_chars)} notify chars")

        # AUTH
        async def wait_raw(
            predicate: Callable[[bytes], bool],
            timeout_ms: int = 5000,
            label: str = "raw",
        ) -> bytes:
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                for i, raw in enumerate(received_packets):
                    if predicate(raw):
                        received_packets.pop(i)
                        return raw
                event.clear()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.05))
                except asyncio.TimeoutError:
                    pass
            raise TimeoutError(f"Timeout waiting for {label}")

        log("AUTH...")
        await write_ae01(get_random_auth_data())
        try:
            await wait_raw(
                lambda r: len(r) == 17 and r[0] == 0x01,
                timeout_ms=5000, label="auth response [0x01]",
            )
            await write_ae01(bytes([0x02, 0x70, 0x61, 0x73, 0x73]))
            challenge = await wait_raw(
                lambda r: len(r) == 17 and r[0] == 0x00,
                timeout_ms=5000, label="auth challenge [0x00]",
            )
            encrypted = get_encrypted_auth_data(challenge)
            await write_ae01(encrypted)
            await wait_raw(
                lambda r: len(r) >= 5 and r[0] == 0x02 and r[1:5] == b"pass",
                timeout_ms=5000, label="auth pass confirmation",
            )
            log("AUTH SUCCESS")
        except Exception as e:
            raise RuntimeError(f"Auth failed: {e}") from e

        # ── Step 1: getSysInfo (0x07) to discover storage devHandler ──
        sn = 1
        sysinfo_payload = [
            0xFF,                    # function: get all
            0xFF, 0xFF, 0xFF, 0x00,  # mask (big-endian) — request storage info
        ]
        sysinfo_frame = build_command(0x07, sn, sysinfo_payload)
        log(f"TX getSysInfo (0x07): {_hex(bytes(sysinfo_frame))}")
        await write_ae01(bytes(sysinfo_frame))
        sn += 1

        dev_handler = [0x00, 0x00, 0x00, 0x00]  # default
        try:
            sysinfo_resp = await wait_packet(
                lambda p: p.op_code == 0x07 and not p.is_command,
                timeout_ms=8000,
                label="getSysInfo response",
            )
            log(f"SysInfo response: status={sysinfo_resp.status}, payload_len={len(sysinfo_resp.payload)}")
            log(f"  raw payload: {' '.join(f'{b:02x}' for b in sysinfo_resp.payload)}")
            # Parse storage device info from attributes
            dev_handler = _extract_dev_handler(sysinfo_resp.payload, log)
            log(f"Using devHandler: {' '.join(f'{b:02x}' for b in dev_handler)}")
        except TimeoutError:
            log("getSysInfo timeout, using default devHandler=0")

        # Also try to auto-ACK any device-initiated commands that arrived
        for i in range(len(received_packets) - 1, -1, -1):
            pkt = parse(list(received_packets[i]))
            if pkt and pkt.is_command and pkt.op_code not in (0x0C, 0x0D, 0x01):
                received_packets.pop(i)
                resp = build_response(pkt.op_code, pkt.op_code_sn, 0x00, [])
                await write_ae01(bytes(resp))

        # ── Step 2: Try StartFileBrowse (0x0C) with devHandler candidates ──
        # If getSysInfo found an online handler, try that first;
        # otherwise iterate common handler values.
        handler_candidates = [dev_handler]
        for candidate in [
            [0x00, 0x00, 0x00, 0x02],  # flash index 2
            [0x00, 0x00, 0x00, 0x01],  # flash index 1
            [0x00, 0x00, 0x00, 0x00],  # flash index 0
            [0x00, 0x00, 0x00, 0x05],  # flash index 5
            [0x00, 0x00, 0x02, 0x00],  # alternative encoding
        ]:
            if candidate not in handler_candidates:
                handler_candidates.append(candidate)

        all_entries: list[dict[str, object]] = []
        for handler in handler_candidates:
            browse_payload = [
                0x00,                    # type: folder
                0x20,                    # page size: 32
                0x00, 0x01,              # start index: 1 (1-indexed)
            ] + handler + [
                0x00,                    # reserved
                0x04,                    # path length: 4
                0x00, 0x00, 0x00, 0x00,  # root cluster
            ]
            cmd_frame = build_command(0x0C, sn, browse_payload)
            handler_hex = ' '.join(f'{b:02x}' for b in handler)
            log(f"TX StartFileBrowse devHandler=[{handler_hex}]")
            await write_ae01(bytes(cmd_frame))
            sn += 1

            try:
                browse_resp = await wait_packet(
                    lambda p: p.op_code == 0x0C and not p.is_command,
                    timeout_ms=5000,
                    label="StartFileBrowse response",
                )
                log(f"  response: status={browse_resp.status}, payload={browse_resp.payload}")
                if browse_resp.status != 0:
                    log(f"  status={browse_resp.status}, skipping")
                    continue
            except TimeoutError:
                log("  no response, skipping")
                continue

            file_data = await collect_data_packets(0x0C, timeout_ms=10000)
            log(f"  collected {len(file_data)} bytes")

            if file_data:
                entries = _parse_file_entries(file_data)
                log(f"  parsed {len(entries)} entries")
                all_entries.extend(entries)
                working_handler = handler
                break  # found the right handler
            else:
                log("  no file data")
        else:
            working_handler = None

        if not all_entries:
            log("No files found on any storage device")
            return all_entries

        # ── Step 3: Browse into subdirectories to find files ──
        dirs_to_browse = [e for e in all_entries if not e.get("is_file")]
        file_entries: list[dict[str, object]] = []
        file_entries.extend([e for e in all_entries if e.get("is_file")])

        for dir_entry in dirs_to_browse:
            cluster = dir_entry.get("cluster", 0)
            cluster_bytes = [
                (cluster >> 24) & 0xFF,
                (cluster >> 16) & 0xFF,
                (cluster >> 8) & 0xFF,
                cluster & 0xFF,
            ]
            # Browse files (type=0x01) inside this directory
            sub_payload = [
                0x01, 0x20, 0x00, 0x01,  # type=file, pageSize=32, startIndex=1
            ] + (working_handler or [0x00, 0x00, 0x00, 0x02]) + [
                0x00, 0x04,
            ] + cluster_bytes
            sub_frame = build_command(0x0C, sn, sub_payload)
            sn += 1
            dir_name = dir_entry.get("name", "?")
            log(f"TX browse subdir '{dir_name}' cluster={cluster}")
            await write_ae01(bytes(sub_frame))

            try:
                sub_resp = await wait_packet(
                    lambda p: p.op_code == 0x0C and not p.is_command,
                    timeout_ms=5000,
                    label=f"subdir {dir_name} response",
                )
                if sub_resp.status != 0:
                    log(f"  subdir browse failed: status={sub_resp.status}")
                    continue
            except TimeoutError:
                log(f"  subdir browse timeout")
                continue

            sub_data = await collect_data_packets(0x0C, timeout_ms=10000)
            if sub_data:
                log(f"  subdir raw data: {' '.join(f'{b:02x}' for b in sub_data[:64])}")
                sub_entries = _parse_file_entries(sub_data)
                log(f"  subdir '{dir_name}': {len(sub_entries)} entries")
                for e in sub_entries:
                    e["path"] = f"{dir_name}/{e.get('name', '?')}"
                    log(f"    {'F' if e.get('is_file') else 'D'} {e['path']} cluster={e.get('cluster')}")
                file_entries.extend(sub_entries)

        return file_entries if file_entries else all_entries


def _extract_dev_handler(payload: list[int], log: Callable) -> list[int]:
    """Parse getSysInfo payload to find the online storage devHandler.

    The payload contains attribute beans. We look for storage info attributes
    and pick the first online device (preferring index 2).
    """
    # The payload is a list of attribute beans.
    # Each attribute: [type(1), length(2 BE), data(length)]
    offset = 0
    handlers: list[tuple[int, list[int], bool]] = []  # (index, handle_bytes, is_online)

    while offset + 3 <= len(payload):
        attr_type = payload[offset]
        attr_len = (payload[offset + 1] << 8) | payload[offset + 2]
        offset += 3
        if offset + attr_len > len(payload):
            break
        attr_data = payload[offset : offset + attr_len]
        offset += attr_len

        # Storage info attribute — parse DevStorageState entries
        # Each entry: index(1) + handle(4 BE) + online(1)
        if attr_len >= 6 and attr_len % 6 == 0:
            for i in range(0, len(attr_data), 6):
                if i + 6 > len(attr_data):
                    break
                idx = attr_data[i]
                handle = attr_data[i + 1 : i + 5]
                is_online = attr_data[i + 5] != 0
                handlers.append((idx, handle, is_online))
                log(f"  Storage[{idx}]: handle={' '.join(f'{b:02x}' for b in handle)}, online={is_online}")

    if not handlers:
        # Fallback: try parsing entire payload as flat storage entries
        log("No structured attributes found, trying flat parse...")
        data = payload
        for i in range(0, len(data) - 5, 6):
            idx = data[i]
            handle = data[i + 1 : i + 5]
            is_online = data[i + 5] != 0
            if idx <= 5:  # valid storage index range
                handlers.append((idx, handle, is_online))
                log(f"  Storage[{idx}]: handle={' '.join(f'{b:02x}' for b in handle)}, online={is_online}")

    # Prefer index 2, then first online
    for idx, handle, online in handlers:
        if idx == 2 and online:
            return handle
    for idx, handle, online in handlers:
        if online:
            return handle

    log("No online storage found, using default devHandler=0")
    return [0x00, 0x00, 0x00, 0x00]
