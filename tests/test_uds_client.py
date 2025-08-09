import time
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
