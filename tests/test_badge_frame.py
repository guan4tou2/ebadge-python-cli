from ebadge_cli.badge_frame import build_frame, parse_frame


def test_build_battery_request():
    assert build_frame(flag=0x00, cmd=0x27, payload=[0x01]) == [
        0x9E,
        0x29,
        0x00,
        0x27,
        0x01,
        0x00,
        0x01,
    ]


def test_parse_battery_response():
    frame = [0x9E, 0x8D, 0x00, 0x27, 0x02, 0x00, 0x00, 0x64]
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed["cmd"] == 0x27
    assert parsed["payload"] == [0x00, 0x64]
    assert parsed["checksum_valid"] is True
