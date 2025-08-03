import logging
import os
import threading
import time
import uuid
from unittest.mock import patch

import can
import pytest

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from can_monitor import load_dbc, monitor  # noqa: E402
from metrics import get_metrics, reset_metrics  # noqa: E402

if not hasattr(can.bus.BusState, "BUS_OFF"):
    setattr(can.bus.BusState, "BUS_OFF", can.bus.BusState.ERROR)


@pytest.fixture(autouse=True)
def _reset_metrics():
    reset_metrics()


@pytest.fixture
def log_setup(tmp_path):
    """Provide a logger writing to a temporary file."""
    log_file = tmp_path / "can.log"
    logger = logging.getLogger(f"test_{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

    try:
        yield logger, log_file
    finally:
        logger.removeHandler(handler)


dbc_path = os.path.join(os.path.dirname(__file__), "..", "src", "OBD.dbc")


@pytest.mark.parametrize("bitrate", [125000, 500000])
def test_monitor_decodes_extended_ids(bitrate, log_setup):
    logger, log_file = log_setup
    db = load_dbc(dbc_path)
    bus = can.interface.Bus(
        bustype="virtual", bitrate=bitrate, receive_own_messages=True
    )

    msg = can.Message(
        arbitration_id=db.messages[0].frame_id,
        is_extended_id=True,
        data=bytes([10, 20, 0, 0, 0, 0, 0, 0]),
    )
    bus.send(msg)

    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, db, logger)

    contents = log_file.read_text()
    expected_decoded = db.decode_message(msg.arbitration_id, msg.data)
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = (
        f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()} "
        f"decoded={expected_decoded}"
    )
    assert expected in contents


def test_bus_off_raises_can_error(log_setup, monkeypatch):
    logger, _ = log_setup
    monkeypatch.setattr(
        can.bus.BusState, "BUS_OFF", can.bus.BusState.ERROR, raising=False
    )
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    monkeypatch.setattr(
        bus.__class__, "state", property(lambda self: can.bus.BusState.BUS_OFF)
    )
    msg = can.Message(arbitration_id=0x18FF50E5, is_extended_id=True, data=bytes(8))

    def fake_recv(timeout=1.0):
        return msg

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, None, logger)

    assert get_metrics()["bus_errors"] == 1


def test_monitor_handles_missing_dbc(log_setup):
    logger, log_file = log_setup
    db = None
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    msg = can.Message(
        arbitration_id=0x18FF50E5,
        is_extended_id=True,
        data=bytes([1, 2, 0, 0, 0, 0, 0, 0]),
    )
    bus.send(msg)
    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, db, logger)

    contents = log_file.read_text()
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()} decoded=None"
    assert expected in contents


def test_monitor_handles_malformed_frame(log_setup):
    logger, log_file = log_setup
    db = load_dbc(dbc_path)
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    msg = can.Message(
        arbitration_id=db.messages[0].frame_id, is_extended_id=True, data=bytes([1])
    )
    bus.send(msg)
    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, db, logger)

    contents = log_file.read_text()
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()} decoded=None"
    assert expected in contents
    assert get_metrics()["decoding_failures"] == 1


def test_monitor_continues_with_slow_transport(log_setup):
    logger, _ = log_setup
    db = load_dbc(dbc_path)
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )

    msg = can.Message(
        arbitration_id=db.messages[0].frame_id,
        is_extended_id=True,
        data=bytes([1, 2, 3, 4, 5, 6, 7, 8]),
    )
    bus.send(msg)
    bus.send(msg)

    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls < 2:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    class SlowTransport:
        def __init__(self) -> None:
            self.count = 0
            self.event = threading.Event()

        def send(self, payload: str) -> None:  # pragma: no cover - timing dependent
            self.count += 1
            if self.count == 1:
                self.event.wait()

    transport = SlowTransport()

    errors: list[Exception] = []

    def run_monitor() -> None:
        try:
            monitor(bus, db, logger, serializer="json", transport=transport)
        except Exception as exc:  # pragma: no cover - we capture for assertion
            errors.append(exc)

    with patch.object(bus, "recv", side_effect=fake_recv):
        t = threading.Thread(target=run_monitor)
        t.start()
        while transport.count < 1:
            time.sleep(0.01)
        while calls < 2:
            time.sleep(0.01)
        assert transport.count == 1
        transport.event.set()
        t.join(1)

    assert errors and isinstance(errors[0], can.CanError)
    assert transport.count == 2


def test_load_dbc_missing_file(caplog):
    with caplog.at_level(logging.WARNING):
        db = load_dbc("does_not_exist.dbc")
    assert db is None
    assert "DBC file not found" in caplog.text
