"""RCSP 傳輸幀格式 (FE DC BA ... EF)。

與 0x9E 幀不同，大檔案傳輸使用此格式。
參考: EbadgeCore/RCSPFrame.swift, RCSPParser.swift

E87Frame 系列函式對應 web-bluetooth-e87 的簡化幀格式:
  [FE DC BA] [flag] [cmd] [len_BE16] [body...] [EF]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RCSPPacket:
    """解析後的 RCSP 封包。"""

    is_command: bool
    has_response: bool
    op_code: int
    op_code_sn: int
    xm_op_code: Optional[int]
    status: Optional[int]
    payload: list[int]


def build_command(
    op_code: int,
    op_code_sn: int,
    payload: list[int],
    *,
    has_response: bool = True,
    xm_op_code: Optional[int] = None,
) -> list[int]:
    """建立 RCSP 命令幀。"""
    flag = 0x80  # is_command
    if has_response:
        flag |= 0x40
    param: list[int] = [op_code_sn]
    if op_code == 0x01 and xm_op_code is not None:
        param.append(xm_op_code)
    param.extend(payload)
    return _build_frame(flag, op_code, param)


def build_response(
    op_code: int,
    op_code_sn: int,
    status: int,
    payload: list[int],
    *,
    xm_op_code: Optional[int] = None,
) -> list[int]:
    """建立 RCSP 回應幀。"""
    flag = 0x00
    param: list[int] = [status, op_code_sn]
    if op_code == 0x01 and xm_op_code is not None:
        param.append(xm_op_code)
    param.extend(payload)
    return _build_frame(flag, op_code, param)


def _build_frame(flag: int, op_code: int, param: list[int]) -> list[int]:
    length = len(param)
    len_hi = (length >> 8) & 0xFF
    len_lo = length & 0xFF
    body = [flag, op_code, len_hi, len_lo] + param
    return [0xFE, 0xDC, 0xBA] + body + [0xEF]


def parse(data: list[int]) -> Optional[RCSPPacket]:
    """解析 RCSP 封包。"""
    if len(data) < 8:
        return None
    if data[0] != 0xFE or data[1] != 0xDC or data[2] != 0xBA:
        return None
    if data[-1] != 0xEF:
        return None
    flag = data[3]
    op_code = data[4]
    length = (data[5] << 8) | data[6]
    param_start = 7
    param_end = param_start + length
    if param_end > len(data) - 1:
        return None
    param = data[param_start:param_end]
    if not param:
        return None
    is_command = (flag & 0x80) != 0
    has_response = (flag & 0x40) != 0
    status: Optional[int] = None
    op_code_sn = 0
    xm_op_code: Optional[int] = None
    payload_start = 0
    if is_command:
        op_code_sn = param[0]
        payload_start = 1
        if op_code == 0x01 and len(param) >= 2:
            xm_op_code = param[1]
            payload_start = 2
    else:
        status = param[0]
        op_code_sn = param[1] if len(param) > 1 else 0
        payload_start = 2
        if op_code == 0x01 and len(param) >= 3:
            xm_op_code = param[2]
            payload_start = 3
    payload = param[payload_start:] if payload_start < len(param) else []
    return RCSPPacket(
        is_command=is_command,
        has_response=has_response,
        op_code=op_code,
        op_code_sn=op_code_sn,
        xm_op_code=xm_op_code,
        status=status,
        payload=payload,
    )


# ── E87 simplified frame format (web-bluetooth-e87 style) ──


@dataclass
class E87Frame:
    """Simplified FE DC BA frame used by web-bluetooth-e87."""

    flag: int
    cmd: int
    body: bytes


def build_e87_frame(flag: int, cmd: int, body: bytes) -> bytes:
    """Build [FE DC BA] [flag] [cmd] [len_BE16] [body...] [EF]."""
    length = len(body)
    header = bytes([0xFE, 0xDC, 0xBA, flag & 0xFF, cmd & 0xFF,
                    (length >> 8) & 0xFF, length & 0xFF])
    return header + body + b"\xEF"


def parse_e87_frame(data: bytes) -> Optional[E87Frame]:
    """Parse a simplified E87 frame. Returns None on invalid data."""
    if len(data) < 8:
        return None
    if data[0] != 0xFE or data[1] != 0xDC or data[2] != 0xBA:
        return None
    if data[-1] != 0xEF:
        return None
    flag = data[3]
    cmd = data[4]
    length = (data[5] << 8) | data[6]
    body = data[7:-1]
    if len(body) != length:
        return None
    return E87Frame(flag=flag, cmd=cmd, body=body)
