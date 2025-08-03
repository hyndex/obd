import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from canbus.setup import MockCommands, setup_interface  # noqa: E402


def test_setup_interface_builds_commands():
    mock = MockCommands()
    setup_interface("can0", 250000, True, commands=mock)
    assert mock.commands == [
        "modprobe can",
        "modprobe can_raw",
        "ip link set can0 down",
        "ip link set can0 up type can bitrate 250000",
        "ip link set can0 type can listen-only on",
    ]
