from __future__ import annotations

from ebadge_cli.badge_frame import build_frame, parse_frame


def build_request() -> list[int]:
    return build_frame(flag=0x00, cmd=0x27, payload=[0x01])


def parse_frame_bytes(data: list[int]) -> dict[str, int] | None:
    parsed = parse_frame(data)
    if not parsed or parsed["cmd"] != 0x27 or not parsed["checksum_valid"]:
        return None
    payload = parsed["payload"]
    if len(payload) < 2:
        return None
    return {"mode": payload[0], "percent": payload[1]}
