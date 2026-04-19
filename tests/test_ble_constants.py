from ebadge_cli.ble_constants import SERVICE_C2E6, CHAR_NOTIFY_C2E6, CHAR_WRITE_C2E6


def test_constants_are_uuid_strings():
    assert SERVICE_C2E6.startswith("C2E6FD00")
    assert CHAR_NOTIFY_C2E6.startswith("C2E6FD01")
    assert CHAR_WRITE_C2E6.startswith("C2E6FD02")
