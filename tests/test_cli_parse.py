from ebadge_cli.cli import build_parser


def test_cli_defaults():
    parser = build_parser()
    args = parser.parse_args(["battery"])
    assert args.command == "battery"
    assert args.mode == "c2e6"
    assert args.repeat == 1
    assert args.interval == 2.0
    assert args.json is False


def test_cli_scan_defaults():
    parser = build_parser()
    args = parser.parse_args(["scan"])
    assert args.command == "scan"
    assert args.timeout == 5.0
    assert args.watch is False
    assert args.interval == 2.0
    assert args.json is False


def test_cli_info_defaults():
    parser = build_parser()
    args = parser.parse_args(["info"])
    assert args.command == "info"
    assert args.name == "E87"
    assert args.json is False


def test_cli_raw_defaults():
    parser = build_parser()
    args = parser.parse_args(["raw", "--cmd", "39"])
    assert args.command == "raw"
    assert args.flag == 0
    assert args.cmd == 39
    assert args.payload == ""
    assert args.parse is False


def test_cli_devices_defaults():
    parser = build_parser()
    args = parser.parse_args(["devices"])
    assert args.command == "devices"
    assert args.timeout == 5.0
    assert args.watch is False
    assert args.interval == 2.0
    assert args.json is False


def test_cli_bind_defaults():
    parser = build_parser()
    args = parser.parse_args(["bind"])
    assert args.command == "bind"
    assert args.mode == "c2e6"
    assert args.timeout == 5.0
    assert args.retries == 3
    assert args.lang is None


def test_cli_time_sync_defaults():
    parser = build_parser()
    args = parser.parse_args(["time-sync"])
    assert args.command == "time-sync"
    assert args.mode == "c2e6"
    assert args.timeout == 5.0
