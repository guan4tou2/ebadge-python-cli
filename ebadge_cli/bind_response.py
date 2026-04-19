from dataclasses import dataclass
from typing import Optional


@dataclass
class BindResponse:
    state: int
    pact_version: str
    firmwa_version: str
    platform: int
    serial_number: int
    function_config: int
    function_config1: int
    function_config2: int
    ui_version: str
    function_bytes: list[int]


def _bytes_to_int(data: list[int], offset: int, length: int) -> int:
    if offset + length > len(data):
        raise ValueError(f"Cannot read {length} bytes at offset {offset} from data of length {len(data)}")
    result = 0
    for i in range(length):
        result |= data[offset + i] << (8 * (length - 1 - i))
    return result


def _bytes_to_long(data: list[int], offset: int, length: int) -> int:
    if offset + length > len(data):
        raise ValueError(f"Cannot read {length} bytes at offset {offset} from data of length {len(data)}")
    result = 0
    for i in range(length):
        result |= data[offset + i] << (8 * (length - 1 - i))
    return result


def _bytes_to_string(data: list[int], offset: int, max_length: int) -> str:
    if offset >= len(data):
        return ""
    actual_length = min(max_length, len(data) - offset)
    null_idx = offset
    for i in range(offset, offset + actual_length):
        if data[i] == 0:
            null_idx = i
            break
        null_idx = i + 1
    null_idx = min(null_idx, offset + actual_length)
    try:
        return bytes(data[offset:null_idx]).decode("ascii", errors="ignore")
    except UnicodeDecodeError:
        return ""


def parse_bind_response(data: list[int]) -> Optional[BindResponse]:
    if not data:
        return None
    offset = 0
    min_length = 39
    if len(data) < min_length:
        return None
    state = data[offset]
    offset += 1
    pact_version = _bytes_to_string(data, offset, 3)
    offset += 3
    firmwa_version = _bytes_to_string(data, offset, 10)
    offset += 10
    platform = _bytes_to_int(data, offset, 4)
    offset += 4
    serial_number = _bytes_to_int(data, offset, 4)
    offset += 4
    function_config = _bytes_to_int(data, offset, 4)
    offset += 4
    function_config1 = _bytes_to_int(data, offset, 4)
    offset += 4
    function_config2 = _bytes_to_long(data, offset, 8)
    offset += 8
    ui_version = _bytes_to_string(data, offset, 5)
    offset += 5
    function_bytes = data[offset:] if offset < len(data) else []
    return BindResponse(
        state=state,
        pact_version=pact_version,
        firmwa_version=firmwa_version,
        platform=platform,
        serial_number=serial_number,
        function_config=function_config,
        function_config1=function_config1,
        function_config2=function_config2,
        ui_version=ui_version,
        function_bytes=function_bytes,
    )
