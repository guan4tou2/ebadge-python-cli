from ebadge_cli.battery_command import build_request, parse_frame_bytes


def test_battery_request_frame():
    assert build_request() == [0x9E, 0x29, 0x00, 0x27, 0x01, 0x00, 0x01]


def test_battery_parse_frame():
    frame = [0x9E, 0x8D, 0x00, 0x27, 0x02, 0x00, 0x00, 0x64]
    result = parse_frame_bytes(frame)
    assert result == {"mode": 0x00, "percent": 0x64}
