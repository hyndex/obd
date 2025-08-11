import os
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from canbus.setup import MockCommands, setup_interface  # noqa: E402


def test_setup_interface_builds_commands(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("canbus.setup._has_cap_net_admin", lambda: True)
    mock = MockCommands()
    setup_interface("can0", 250000, True, commands=mock)
    assert mock.commands == [
        "modprobe can",
        "modprobe can_raw",
        "ip link set can0 down",
        "ip link set can0 up type can bitrate 250000",
        "ip link set can0 type can listen-only on",
    ]


def test_setup_interface_requires_privileges(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr("canbus.setup._has_cap_net_admin", lambda: False)
    mock = MockCommands()
    with pytest.raises(RuntimeError, match="CAP_NET_ADMIN"):
        setup_interface("can0", 250000, commands=mock)
    assert mock.commands == []


def test_setup_interface_ip_permission_error(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("canbus.setup._has_cap_net_admin", lambda: True)
    mock = MockCommands()
    mock.ip_results = [(1, "RTNETLINK answers: Operation not permitted")]
    with pytest.raises(RuntimeError, match="Permission denied"):
        setup_interface("can0", 250000, commands=mock)


def test_setup_interface_invalid_bitrate(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("canbus.setup._has_cap_net_admin", lambda: True)
    mock = MockCommands()
    mock.ip_results = [
        (0, ""),
        (2, "RTNETLINK answers: Invalid argument"),
    ]
    with pytest.raises(ValueError, match="unsupported bitrate"):
        setup_interface("can0", 12345, commands=mock)
