import time
import logging
import threading
import queue
import can
import pytest

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from uds import UDSClient, ISOTransportError  # noqa: E402
from isotp_primitives import TDataPrimitive  # noqa: E402


def test_send_segments_respects_flow_control(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    sent = []

    def fake_send(msg, timeout=None):
        sent.append(msg)

    monkeypatch.setattr(bus, "send", fake_send)

    fc = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x30, 1, 1, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    fcs = [fc, fc]

    def fake_recv(timeout):
        return fcs.pop(0)

    monkeypatch.setattr(bus, "recv", fake_recv)

    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda t: sleeps.append(t))

    data = bytes(range(14))  # 14 bytes -> payload 15 -> FF + 2 CFs
    client.send(0x22, data)

    assert len(sent) == 3
    assert sent[0].data[0] >> 4 == 0x1
    assert sent[1].data[0] == 0x21
    assert sent[2].data[0] == 0x22
    assert len(fcs) == 0
    assert sleeps and pytest.approx(sleeps[0], rel=0.1) == 0.001


def test_send_cumulative_fc_timeout(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    fc = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x30, 1, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    events = [(0.6, fc), (0.6, fc)]
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_recv(timeout: float):
        if not events:
            now[0] += timeout
            return None
        delay, msg = events[0]
        if delay > timeout:
            now[0] += timeout
            events[0] = (delay - timeout, msg)
            return None
        now[0] += delay
        events.pop(0)
        return msg

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bus, "recv", fake_recv)

    data = bytes(range(14))
    with pytest.raises(ISOTransportError, match="Flow control timeout"):
        client.send(0x22, data, timeout=1.0)


def test_send_rejects_overly_large_payload(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    with pytest.raises(ISOTransportError, match="Payload too large"):
        client.send(0x22, bytes(0x1000))

    assert not sent


def test_session_and_security(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    sent: list[can.Message] = []

    def fake_send(msg, timeout=None):
        sent.append(msg)

    monkeypatch.setattr(bus, "send", fake_send)

    resp_session = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x50, 0x03, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_seed = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x04, 0x67, 0x01, 0xAA, 0xBB, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_key = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x67, 0x02, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [resp_session, resp_seed, resp_key]

    def fake_recv(timeout):
        return responses.pop(0)

    monkeypatch.setattr(bus, "recv", fake_recv)

    assert client.change_session(3)
    assert client.security_access(1)
    assert len(sent) == 3
    # verify key derived from seed AA BB -> 55 44 (bitwise inversion)
    assert sent[2].data[:5] == bytes([0x04, 0x27, 0x02, 0x55, 0x44])


def test_extended_addressing(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8, address_extension=0x99)

    sent: list[can.Message] = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    resp = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x99, 0x02, 0x50, 0x03, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    monkeypatch.setattr(bus, "recv", lambda timeout: resp)

    client.send(0x10, b"\x03")
    payload = client.receive()

    assert sent[0].data[:4] == bytes([0x99, 0x02, 0x10, 0x03])
    assert payload == bytes([0x50, 0x03])


def test_normal_fixed_addressing(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0, 0, source_address=0xF1, target_address=0x10)

    sent: list[can.Message] = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    resp = can.Message(
        arbitration_id=0x18DAF110,
        data=bytes([0x02, 0x7F, 0x31, 0, 0, 0, 0, 0]),
        is_extended_id=True,
    )
    monkeypatch.setattr(bus, "recv", lambda timeout: resp)

    client.send(0x31, b"\x01")
    payload = client.receive()

    assert sent[0].arbitration_id == 0x18DA10F1
    assert sent[0].is_extended_id
    assert payload[:2] == bytes([0x7F, 0x31])


def test_tdata_primitives(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    calls = []
    t_data = TDataPrimitive(
        req=lambda s, d: calls.append(("req", s, d)),
        ind=lambda p: calls.append(("ind", p)),
        con=lambda ok, err: calls.append(("con", ok)),
        som_ind=lambda: calls.append(("som_ind",)),
    )
    client = UDSClient(bus, 0x7E0, 0x7E8, t_data=t_data)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    cf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x21, 6, 7, 8, 9, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [ff, cf]
    monkeypatch.setattr(bus, "recv", lambda timeout: responses.pop(0))

    payload = client.request(0x22, b"\x01")

    assert payload == bytes(range(10))
    assert calls == [
        ("req", 0x22, b"\x01"),
        ("con", True),
        ("som_ind",),
        ("ind", bytes(range(10))),
    ]


def test_receive_sequence_number_mismatch(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    cf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x22, 6, 7, 8, 9, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [ff, cf]
    monkeypatch.setattr(bus, "recv", lambda timeout: responses.pop(0))

    with pytest.raises(ISOTransportError, match="Sequence number mismatch"):
        client.receive()


def test_receive_wait_and_resume(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    cf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x21, 6, 7, 8, 9, 0, 0, 0]),
        is_extended_id=False,
    )

    responses = [ff]

    def fake_recv(timeout):
        if responses:
            return responses.pop(0)
        return None

    sent: list[can.Message] = []

    def fake_send(msg, timeout=None):
        sent.append(msg)
        if msg.arbitration_id == 0x7E0 and (msg.data[0] >> 4) == 0x3:
            fs = msg.data[0] & 0x0F
            if fs == 1:
                client.resume_rx()
            elif fs == 0:
                responses.append(cf)

    monkeypatch.setattr(bus, "send", fake_send)
    monkeypatch.setattr(bus, "recv", fake_recv)

    client.pause_rx()
    payload = client.receive(timeout=1.0)

    assert payload == bytes(range(10))
    fc_frames = [m for m in sent if (m.data[0] >> 4) == 0x3]
    assert [f.data[0] for f in fc_frames] == [0x31, 0x30]


def test_receive_overflow(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8, max_rx_size=4)

    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x05, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    sent: list[can.Message] = []

    monkeypatch.setattr(bus, "recv", lambda timeout: ff)
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    with pytest.raises(ISOTransportError, match="max_rx_size"):
        client.receive()

    fc_frames = [m for m in sent if (m.data[0] >> 4) == 0x3]
    assert fc_frames and fc_frames[0].data[0] == 0x32


def test_receive_reset_warns_and_calls_hook(monkeypatch, caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    hooks: list[str] = []
    client = UDSClient(bus, 0x7E0, 0x7E8, on_reset=lambda: hooks.append("reset"))
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    ff1 = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    sf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x62, 0x00, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [ff1, sf]
    monkeypatch.setattr(
        bus, "recv", lambda timeout: responses.pop(0) if responses else None
    )

    with caplog.at_level(logging.WARNING):
        payload = client.receive()

    assert payload[:2] == bytes([0x62, 0x00])
    assert hooks == ["reset"]
    assert any("unexpected start-of-frame" in r.message for r in caplog.records)


def test_receive_reset_raises_with_error_on_reset(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    hooks: list[str] = []
    client = UDSClient(
        bus,
        0x7E0,
        0x7E8,
        on_reset=lambda: hooks.append("reset"),
        error_on_reset=True,
    )
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    ff1 = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0, 1, 2, 3, 4, 5]),
        is_extended_id=False,
    )
    ff2 = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0xAA, 0xBB, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [ff1, ff2]
    monkeypatch.setattr(
        bus, "recv", lambda timeout: responses.pop(0) if responses else None
    )

    with pytest.raises(ISOTransportError, match="Unexpected start-of-frame"):
        client.receive()

    assert hooks == ["reset"]


def test_request_tuple_timeouts(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0x59, 0x02, 0x00, 0, 0, 0]),
        is_extended_id=False,
    )
    cf = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x21, 0, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    events = [(0.0, ff), (0.2, cf)]
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_recv(timeout: float):
        if not events:
            now[0] += timeout
            return None
        delay, msg = events[0]
        if delay > timeout:
            now[0] += timeout
            events[0] = (delay - timeout, msg)
            return None
        now[0] += delay
        events.pop(0)
        return msg

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bus, "recv", fake_recv)

    with pytest.raises(ISOTransportError, match="UDS response timeout"):
        client.request(0x22, b"\x01", timeout=(1.0, 0.1))


def test_logging_debug(monkeypatch, caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    test_logger = logging.getLogger("uds_test")
    test_logger.setLevel(logging.DEBUG)
    client = UDSClient(bus, 0x7E0, 0x7E8, logger=test_logger)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)
    resp = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x50, 0x03, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    monkeypatch.setattr(bus, "recv", lambda timeout: resp)

    with caplog.at_level(logging.DEBUG, logger="uds_test"):
        client.send(0x10, b"\x03")
        client.receive()

    assert any("Sent Single Frame" in r.message for r in caplog.records)
    assert any("Received Single Frame" in r.message for r in caplog.records)


def test_request_thread_locking(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    client = UDSClient(bus, 0x7E0, 0x7E8)

    resp = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x50, 0x03, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )

    responses: queue.Queue = queue.Queue()
    responses.put(resp)
    responses.put(resp)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)
    monkeypatch.setattr(bus, "recv", lambda timeout: responses.get_nowait())

    results: list[bytes] = []

    def worker() -> None:
        results.append(client.request(0x10, b"\x03"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t1.join()
    t2.start()
    t2.join()

    assert results == [bytes([0x50, 0x03]), bytes([0x50, 0x03])]

    block = threading.Event()
    release = threading.Event()

    def blocking_send(msg, timeout=None):
        block.set()
        release.wait()

    monkeypatch.setattr(bus, "send", blocking_send)
    monkeypatch.setattr(bus, "recv", lambda timeout: resp)

    t = threading.Thread(target=lambda: client.request(0x10, b"\x03"))
    t.start()
    block.wait()
    with pytest.raises(RuntimeError):
        client.request(0x10, b"\x03")
    release.set()
    t.join()


def test_send_wait_then_cts(monkeypatch, caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    test_logger = logging.getLogger("uds_wait")
    test_logger.setLevel(logging.DEBUG)
    client = UDSClient(bus, 0x7E0, 0x7E8, logger=test_logger)

    sent: list[can.Message] = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    fc_wait = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x31, 0, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    fc_cts = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x30, 0, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )

    calls = {"count": 0}

    def fake_recv(timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            assert len(sent) == 1
            return fc_wait
        if calls["count"] == 2:
            assert len(sent) == 1
            return fc_cts
        return None

    monkeypatch.setattr(bus, "recv", fake_recv)

    with caplog.at_level(logging.DEBUG, logger="uds_wait"):
        client.send(0x22, bytes(range(10)))

    assert len(sent) == 2
    assert sent[1].data[0] == 0x21
    assert any("Flow Control WAIT received" in r.message for r in caplog.records)


def test_send_wait_timeout(monkeypatch, caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    test_logger = logging.getLogger("uds_wait_timeout")
    test_logger.setLevel(logging.DEBUG)
    client = UDSClient(bus, 0x7E0, 0x7E8, logger=test_logger)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    fc_wait = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x31, 0, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )

    events = [(0.6, fc_wait)]
    now = [0.0]

    def fake_monotonic():
        return now[0]

    def fake_recv(timeout):
        if events:
            delay, msg = events[0]
            if delay > timeout:
                now[0] += timeout
                events[0] = (delay - timeout, msg)
                return None
            now[0] += delay
            events.pop(0)
            return msg
        now[0] += timeout
        return None

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bus, "recv", fake_recv)

    with caplog.at_level(logging.DEBUG, logger="uds_wait_timeout"):
        with pytest.raises(ISOTransportError, match="No Flow Control frame received"):
            client.send(0x22, bytes(range(10)), timeout=1.0)

    assert any("Flow Control WAIT received" in r.message for r in caplog.records)
    assert any("No Flow Control frame received" in r.message for r in caplog.records)


def test_send_flow_control_overflow(monkeypatch, caplog):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    test_logger = logging.getLogger("uds_overflow")
    test_logger.setLevel(logging.DEBUG)
    client = UDSClient(bus, 0x7E0, 0x7E8, logger=test_logger)

    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: None)

    fc_overflow = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x32, 0, 0, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    monkeypatch.setattr(bus, "recv", lambda timeout: fc_overflow)

    with caplog.at_level(logging.DEBUG, logger="uds_overflow"):
        with pytest.raises(ISOTransportError, match="Flow control overflow"):
            client.send(0x22, bytes(range(10)))

    assert any("Flow control overflow" in r.message for r in caplog.records)

