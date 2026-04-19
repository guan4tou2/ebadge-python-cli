"""Test E87 frame building and parsing."""

from ebadge_cli.rcsp_frame import E87Frame, build_e87_frame, parse_e87_frame


def test_build_e87_frame_basic():
    frame = build_e87_frame(0xC0, 0x06, bytes([0x02, 0x00, 0x01]))
    assert frame == bytes([0xFE, 0xDC, 0xBA, 0xC0, 0x06, 0x00, 0x03, 0x02, 0x00, 0x01, 0xEF])


def test_parse_e87_frame_basic():
    raw = bytes([0xFE, 0xDC, 0xBA, 0xC0, 0x06, 0x00, 0x03, 0x02, 0x00, 0x01, 0xEF])
    f = parse_e87_frame(raw)
    assert f is not None
    assert f.flag == 0xC0
    assert f.cmd == 0x06
    assert f.body == bytes([0x02, 0x00, 0x01])


def test_roundtrip():
    body = bytes(range(20))
    frame = build_e87_frame(0x80, 0x01, body)
    parsed = parse_e87_frame(frame)
    assert parsed is not None
    assert parsed.flag == 0x80
    assert parsed.cmd == 0x01
    assert parsed.body == body


def test_parse_invalid_header():
    assert parse_e87_frame(bytes([0x00, 0xDC, 0xBA, 0xC0, 0x06, 0x00, 0x01, 0x00, 0xEF])) is None


def test_parse_too_short():
    assert parse_e87_frame(bytes([0xFE, 0xDC, 0xBA])) is None


def test_parse_length_mismatch():
    # Says length=5 but only 3 bytes of body
    assert parse_e87_frame(bytes([0xFE, 0xDC, 0xBA, 0xC0, 0x06, 0x00, 0x05, 0x01, 0x02, 0x03, 0xEF])) is None


def test_data_frame_format():
    """Test building a data frame matching web-bluetooth-e87 format."""
    # seq=0x06, subcmd=0x1D, slot=0, CRC=0xC0B8, + 4 bytes of data
    body = bytes([0x06, 0x1D, 0x00, 0xC0, 0xB8, 0xAA, 0xBB, 0xCC, 0xDD])
    frame = build_e87_frame(0x80, 0x01, body)
    parsed = parse_e87_frame(frame)
    assert parsed is not None
    assert parsed.flag == 0x80
    assert parsed.cmd == 0x01
    assert parsed.body[0] == 0x06  # seq
    assert parsed.body[1] == 0x1D  # subcmd
    assert parsed.body[2] == 0x00  # slot
