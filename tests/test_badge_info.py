"""Tests for badge_info module."""

import pytest

from ebadge_cli.badge_info import BadgeInfo, parse_badge_info


def test_parse_badge_info_valid():
    """Parse valid BadgeInfo payload."""
    # type=1, width/height 368 LE, memory LE in KB (匹配 Android 端 getMemory() + "KB")
    payload = [
        1,  # type
        112, 1,  # width 368 LE
        112, 1,  # height 368 LE
        112, 1,  # pictureWidth 368 LE
        112, 1,  # pictureHeigh 368 LE
        0x78, 0x56, 0x34, 0x12,  # memory 0x12345678 LE (bytes reversed)
    ]
    info = parse_badge_info(payload)
    assert info is not None
    assert info.width == 368
    assert info.height == 368
    assert info.picture_width == 368
    assert info.picture_height == 368
    assert info.memory == 0x12345678
    assert info.resolution == (368, 368)


def test_parse_badge_info_invalid_type():
    """Reject payload with type != 1."""
    payload = [0] + [0] * 12
    assert parse_badge_info(payload) is None


def test_parse_badge_info_too_short():
    """Reject payload that is too short."""
    assert parse_badge_info([]) is None
    assert parse_badge_info([1] * 10) is None


def test_parse_badge_info_default_resolution():
    """Resolution uses 368 when picture dimensions are 0."""
    payload = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    info = parse_badge_info(payload)
    assert info is not None
    assert info.resolution == (368, 368)
