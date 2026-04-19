"""Test Jieli RCSP auth crypto against captured BLE exchange."""

from ebadge_cli.jl_auth import (
    MAGIC,
    STATIC_KEY,
    function_e1_test,
    get_encrypted_auth_data,
    get_random_auth_data,
)


def test_function_e1_test_captured_vector():
    """Verify against captured BLE auth exchange from web-bluetooth-e87."""
    challenge = [
        0xB6, 0xE0, 0x80, 0xEC, 0xAF, 0xF3, 0x22, 0x91,
        0x6D, 0x88, 0xFA, 0xD5, 0xAA, 0x34, 0xC2, 0xAC,
    ]
    expected = [
        0x1D, 0x88, 0x97, 0xAC, 0x46, 0x04, 0xD3, 0x32,
        0xE8, 0x17, 0x5E, 0x81, 0xBB, 0x29, 0x25, 0x24,
    ]
    result = function_e1_test(MAGIC, challenge, STATIC_KEY)
    assert result == expected, f"got {[hex(b) for b in result]}"


def test_get_random_auth_data_format():
    data = get_random_auth_data()
    assert len(data) == 17
    assert data[0] == 0x00


def test_get_encrypted_auth_data_format():
    device_challenge = bytes([0x00] + [
        0xB6, 0xE0, 0x80, 0xEC, 0xAF, 0xF3, 0x22, 0x91,
        0x6D, 0x88, 0xFA, 0xD5, 0xAA, 0x34, 0xC2, 0xAC,
    ])
    result = get_encrypted_auth_data(device_challenge)
    assert len(result) == 17
    assert result[0] == 0x01
    expected_encrypted = bytes([
        0x1D, 0x88, 0x97, 0xAC, 0x46, 0x04, 0xD3, 0x32,
        0xE8, 0x17, 0x5E, 0x81, 0xBB, 0x29, 0x25, 0x24,
    ])
    assert result[1:] == expected_encrypted


def test_random_auth_data_unique():
    a = get_random_auth_data()
    b = get_random_auth_data()
    assert a[1:] != b[1:]
