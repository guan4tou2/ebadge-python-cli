"""CRC16 (CCITT, poly 0x1021)。與 EbadgeCore CRC16 一致。"""

from __future__ import annotations


def compute(data: bytes | list[int], seed: int = 0) -> int:
    crc = seed & 0xFFFF
    for byte in data:
        b = byte if isinstance(byte, int) else byte
        crc = _update(crc, b & 0xFF)
    return crc & 0xFFFF


def _update(crc: int, byte: int) -> int:
    c = crc ^ (byte << 8)
    for _ in range(8):
        if (c & 0x8000) != 0:
            c = ((c << 1) ^ 0x1021) & 0xFFFF
        else:
            c = (c << 1) & 0xFFFF
    return c
