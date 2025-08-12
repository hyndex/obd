import logging
import threading
import time

import can
import pytest

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from can_monitor import _sequence_loop  # noqa: E402
import can_monitor  # noqa: E402


def test_sequence_interval(monkeypatch, bus_factory):
    bus = bus_factory(bitrate=500000)
    send_times = []
    monkeypatch.setattr(
        bus, "send", lambda msg, timeout=None: send_times.append(time.monotonic())
    )
    stop = threading.Event()
    seq = [{"can_id": 0x123, "payload": "00"}]
    t = threading.Thread(
        target=_sequence_loop,
        args=(bus, seq, 20, None, logging.getLogger("test"), stop),
    )
    t.start()
    while len(send_times) < 3:
        time.sleep(0.01)
    stop.set()
    t.join()
    intervals = [send_times[i + 1] - send_times[i] for i in range(len(send_times) - 1)]
    for iv in intervals:
        assert pytest.approx(iv, rel=0.2) == 0.02


def test_sequence_multiframe(monkeypatch, bus_factory):
    bus = bus_factory(bitrate=500000)
    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))
    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x05, 0x59, 0x02, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    cf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x21, 1, 2, 3, 4, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [ff, cf]

    def fake_recv(timeout):
        if responses:
            return responses.pop(0)
        time.sleep(timeout)
        return None

    monkeypatch.setattr(bus, "recv", fake_recv)

    stop = threading.Event()
    seq = [
        {
            "can_id": 0x7E0,
            "payload": "02010D0000000000",
            "response_id": 0x7E8,
            "timeout_ms": 100,
        }
    ]
    t = threading.Thread(
        target=_sequence_loop,
        args=(
            bus,
            seq,
            1000,
            {"flow_control": {"block_size": 0, "st_min_ms": 0}},
            logging.getLogger("test"),
            stop,
        ),
    )
    t.start()
    while responses:
        time.sleep(0.01)
    stop.set()
    t.join()
    assert any(m.data[0] == 0x30 for m in sent)


def test_sequence_skips_security_access_when_locked_out(monkeypatch, bus_factory):
    bus = bus_factory(bitrate=500000)
    sent: list[can.Message] = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))
    can_monitor.uds_locked_out = True
    stop = threading.Event()
    seq = [{"can_id": 0x123, "payload": "2701"}]
    t = threading.Thread(
        target=_sequence_loop,
        args=(bus, seq, 20, None, logging.getLogger("test"), stop),
    )
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join()
    assert not sent


def test_sequence_loop_exception_stops_thread(monkeypatch, bus_factory, caplog):
    bus = bus_factory(bitrate=500000)

    def fail_send(msg, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(bus, "send", fail_send)
    stop = threading.Event()
    seq = [{"can_id": 0x123, "payload": "00"}]
    logger = logging.getLogger("seq")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        t = threading.Thread(
            target=_sequence_loop, args=(bus, seq, 20, None, logger, stop)
        )
        t.start()
        t.join(timeout=1)
    assert stop.is_set()
    assert not t.is_alive()
    assert any("Sequence loop error" in r.getMessage() for r in caplog.records)


def test_sequence_loop_timeout_warns(monkeypatch, bus_factory, caplog):
    bus = bus_factory(bitrate=500000)
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    def no_resp(timeout):
        time.sleep(timeout)
        return None

    monkeypatch.setattr(bus, "recv", no_resp)
    stop = threading.Event()
    seq = [
        {
            "name": "ping",
            "can_id": 0x123,
            "payload": "00",
            "response_id": 0x321,
            "timeout_ms": 10,
        }
    ]
    logger = logging.getLogger("seq")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        t = threading.Thread(
            target=_sequence_loop, args=(bus, seq, 100, None, logger, stop)
        )
        t.start()
        time.sleep(0.05)
        stop.set()
        t.join()
    assert any(
        "No response to 'ping' request" in r.getMessage() for r in caplog.records
    )


def test_sequence_loop_logs_on_long_payload(monkeypatch, bus_factory, caplog):
    bus = bus_factory(bitrate=500000)
    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))
    stop = threading.Event()
    seq = [
        {
            "name": "toolong",
            "can_id": 0x123,
            "payload": "001122334455667788",
        }
    ]
    logger = logging.getLogger("seq")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        t = threading.Thread(
            target=_sequence_loop, args=(bus, seq, 20, None, logger, stop)
        )
        t.start()
        time.sleep(0.05)
        stop.set()
        t.join()
    assert not sent
    assert any("exceeds 8 bytes" in r.getMessage() for r in caplog.records)
