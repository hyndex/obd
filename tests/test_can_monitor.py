import json
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

from can_monitor import load_dbc, load_opendbc_dbs, monitor, apply_patches  # noqa: E402
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
            monitor(bus, db, logger, print_raw=True)

    contents = log_file.read_text()
    expected_decoded = db.decode_message(msg.arbitration_id, msg.data)
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = (
        f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()} "
        f"decoded={expected_decoded}"
    )
    assert expected in contents


def test_monitor_without_print_raw(log_setup):
    logger, log_file = log_setup
    db = load_dbc(dbc_path)
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
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
    assert "raw=" not in contents
    expected_decoded = db.decode_message(msg.arbitration_id, msg.data)
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = f"id=0x{msg.arbitration_id:{fmt}} decoded={expected_decoded}"
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
            monitor(bus, db, logger, print_raw=True)

    contents = log_file.read_text()
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()}"
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
            monitor(bus, db, logger, print_raw=True)

    contents = log_file.read_text()
    fmt = "08X" if msg.is_extended_id else "03X"
    expected = f"id=0x{msg.arbitration_id:{fmt}} raw={msg.data.hex()}"
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


def test_apply_patches_sends_and_logs(caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    patches = {
        "demo": {
            "can_id": 0x123,
            "payload": "01",
            "response_id": 0x456,
            "timeout_ms": 100,
        }
    }
    rsp = can.Message(arbitration_id=0x456, data=b"\x67", is_extended_id=False)

    def fake_recv(timeout=0.1):
        return rsp

    with caplog.at_level(logging.INFO):
        with patch.object(bus, "recv", side_effect=fake_recv):
            apply_patches(bus, patches)
    assert "Patch 'demo' applied" in caplog.text


def test_load_dbc_missing_file(caplog):
    with caplog.at_level(logging.WARNING):
        db = load_dbc("does_not_exist.dbc")
    assert db is None
    assert "DBC file not found" in caplog.text


def test_opendbc_fallback_selects_best_dbc(tmp_path):
    """Ensure opendbc fallback loads the best matching DBC."""
    header = (
        'VERSION ""\n'
        "\n"
        "NS_ :\n"
        "    NS_DESC_\n"
        "    CM_\n"
        "    BA_DEF_\n"
        "    BA_\n"
        "    VAL_\n"
        "    CAT_DEF_\n"
        "    CAT_\n"
        "    FILTER\n"
        "    BA_DEF_DEF_\n"
        "    EV_DATA_\n"
        "    ENVVAR_DATA_\n"
        "    SGTYPE_\n"
        "    SGTYPE_VAL_\n"
        "    BA_DEF_SGTYPE_\n"
        "    BA_SGTYPE_\n"
        "    SIG_TYPE_REF_\n"
        "    VAL_TABLE_\n"
        "    SIG_GROUP_\n"
        "    SIG_VALTYPE_\n"
        "    SIGTYPE_VALTYPE_\n"
        "    BO_TX_BU_\n"
        "    BA_DEF_REL_\n"
        "    BA_REL_\n"
        "    BA_DEF_DEF_REL_\n"
        "    BU_SG_REL_\n"
        "    BU_EV_REL_\n"
        "    BU_BO_REL_\n"
        "    SG_MUL_VAL_\n"
        "\n"
        "BS_:\n"
        "\n"
        "BU_:\n"
        "\n"
    )

    def write_dbc(path, msg_id):
        body = (
            f"BO_ {msg_id} MSG: 8 Vector__XXX\n"
            ' SG_ SIG : 0|8@1+ (1,0) [0|255] "" Vector__XXX\n'
        )
        path.write_text(header + body)

    dbc1 = tmp_path / "car1.dbc"
    dbc2 = tmp_path / "car2.dbc"
    write_dbc(dbc1, 0x100)
    write_dbc(dbc2, 0x200)

    dummy = type("Opendbc", (), {"DBC_PATH": str(tmp_path)})

    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    msg = can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes(8))
    bus.send(msg)

    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=0.1):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        calls += 1
        return None

    with patch.object(bus, "recv", side_effect=fake_recv):
        with patch.dict(sys.modules, {"opendbc": dummy}):
            db, fallbacks = load_opendbc_dbs(bus)

    assert fallbacks == []
    assert db is not None
    assert db.get_message_by_frame_id(0x100)


def test_uds_dtc_alert(log_setup):
    logger, log_file = log_setup
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )

    with open(Path(__file__).resolve().parents[1] / "uds_config.json") as f:
        uds_cfg = json.load(f)["uds"]

    first = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0B, 0x59, 0x02, 0x02, 0x20, 0xF9, 0x00]),
        is_extended_id=False,
    )
    second = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x21, 0x40, 0x05, 0x8D, 0x00, 0x40, 0x00, 0x00]),
        is_extended_id=False,
    )
    frames = [first, second]

    def fake_recv(timeout=1.0):
        if frames:
            return frames.pop(0)
        raise can.CanError("stop")

    sent: list[can.Message] = []

    def fake_send(msg, timeout=None):  # pragma: no cover - simple capture
        sent.append(msg)

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv), patch.object(
            bus, "send", side_effect=fake_send
        ):
            monitor(bus, None, logger, uds_config=uds_cfg)

    contents = log_file.read_text()
    assert "DTC P20F9" in contents
    assert "*** ALERT: Critical DTC P20F9 detected" in contents
    assert "DTC P058D" in contents
    assert "*** ALERT: Critical DTC P058D" not in contents
    assert any(m.arbitration_id == 0x7E0 and m.data[0] == 0x30 for m in sent)
