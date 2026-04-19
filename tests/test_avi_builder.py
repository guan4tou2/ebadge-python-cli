"""Test MJPG AVI builder."""

import struct
from ebadge_cli.avi_builder import build_mjpg_avi


def _minimal_jpeg() -> bytes:
    """Minimal valid JPEG (SOI + EOI)."""
    return b"\xFF\xD8\xFF\xE0" + b"\x00" * 20 + b"\xFF\xD9"


def test_single_frame_riff_header():
    avi = build_mjpg_avi([_minimal_jpeg()])
    assert avi[:4] == b"RIFF"
    assert avi[8:12] == b"AVI "
    riff_size = struct.unpack_from("<I", avi, 4)[0]
    assert riff_size == len(avi) - 8


def test_contains_movi_and_idx1():
    avi = build_mjpg_avi([_minimal_jpeg(), _minimal_jpeg()])
    assert b"movi" in avi
    assert b"idx1" in avi
    assert b"00dc" in avi


def test_frame_count_in_header():
    frames = [_minimal_jpeg()] * 5
    avi = build_mjpg_avi(frames, fps=10)
    # avih is inside hdrl list; find dwTotalFrames at avih+20
    avih_pos = avi.index(b"avih")
    # avih chunk: fourcc(4) + size(4) + data; dwTotalFrames is at offset 16 in data
    total = struct.unpack_from("<I", avi, avih_pos + 8 + 16)[0]
    assert total == 5


def test_idx1_entry_count():
    frames = [_minimal_jpeg()] * 3
    avi = build_mjpg_avi(frames)
    idx1_pos = avi.index(b"idx1")
    idx1_size = struct.unpack_from("<I", avi, idx1_pos + 4)[0]
    assert idx1_size == 3 * 16  # 16 bytes per entry


def test_custom_dimensions():
    avi = build_mjpg_avi([_minimal_jpeg()], width=100, height=200, fps=24)
    # Check strh contains fps
    strh_pos = avi.index(b"strh")
    rate = struct.unpack_from("<I", avi, strh_pos + 8 + 24)[0]
    assert rate == 24


def test_empty_frames_raises():
    try:
        build_mjpg_avi([])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
