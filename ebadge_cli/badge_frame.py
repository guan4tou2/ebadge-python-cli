from __future__ import annotations

from typing import TypedDict


class FrameData(TypedDict):
    checksum: int
    flag: int
    cmd: int
    length: int
    payload: list[int]
    checksum_valid: bool


def _checksum(flag: int, cmd: int, len_l: int, len_h: int, payload: list[int]) -> int:
    total = flag + cmd + len_l + len_h + sum(payload)
    return total & 0xFF


def build_frame(flag: int, cmd: int, payload: list[int]) -> list[int]:
    length = len(payload)
    len_l = length & 0xFF
    len_h = (length >> 8) & 0xFF
    checksum = _checksum(flag, cmd, len_l, len_h, payload)
    return [0x9E, checksum, flag, cmd, len_l, len_h] + payload


def parse_frame(data: list[int]) -> FrameData | None:
    if len(data) < 6 or data[0] != 0x9E:
        return None
    checksum = data[1]
    flag = data[2]
    cmd = data[3]
    len_l = data[4]
    len_h = data[5]
    length = len_l | (len_h << 8)
    payload = data[6:] if len(data) > 6 else []
    checksum_valid = checksum == _checksum(flag, cmd, len_l, len_h, payload)
    if len(payload) != length:
        checksum_valid = False
    return {
        "checksum": checksum,
        "flag": flag,
        "cmd": cmd,
        "length": length,
        "payload": payload,
        "checksum_valid": checksum_valid,
    }
