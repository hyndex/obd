import logging
import os
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
    logger.addHandler(handler)
    logger.propagate = False

    def info(msg, *args, **kwargs):
        formatted = msg.replace("%%", "%") % args
        logger._log(logging.INFO, formatted, (), **kwargs)

    logger.info = info  # type: ignore[assignment]

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
    expected = (
        f"id=0x{msg.arbitration_id:03X} raw={msg.data.hex()} decoded={expected_decoded}"
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
    expected = f"id=0x{msg.arbitration_id:03X} raw={msg.data.hex()} decoded=None"
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
    expected = f"id=0x{msg.arbitration_id:03X} raw={msg.data.hex()} decoded=None"
    assert expected in contents
    assert get_metrics()["decoding_failures"] == 1


def test_load_dbc_missing_file(caplog, monkeypatch):
    def warn(msg, *args, **kwargs):
        logging.getLogger()._log(
            logging.WARNING, msg.replace("%%", "%") % args, (), **kwargs
        )

    monkeypatch.setattr(logging, "warning", warn)

    with caplog.at_level(logging.WARNING):
        db = load_dbc("does_not_exist.dbc")
    assert db is None
    assert "DBC file not found" in caplog.text
