import can
import pytest

from uds import UDSClient, ISOTransportError
from security_algorithms import crc16_ccitt, xor_invert
from security_pretest import auto_security_access


def test_security_access_with_data_record(monkeypatch, bus_factory):
    bus = bus_factory(bitrate=500000)
    client = UDSClient(bus, 0x7E0, 0x7E8)

    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    resp_seed = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x04, 0x67, 0x01, 0x12, 0x34, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_key = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x67, 0x02, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [resp_seed, resp_key]
    monkeypatch.setattr(bus, "recv", lambda timeout: responses.pop(0))

    record = b"\x01\x01"
    key_expected = crc16_ccitt(b"\x12\x34", record)

    assert client.security_access(1, data_record=record, key_algo=crc16_ccitt)
    assert len(sent) == 2
    assert bytes(sent[0].data)[:5] == bytes([0x04, 0x27, 0x01, 0x01, 0x01])
    assert bytes(sent[1].data)[:7] == bytes([0x06, 0x27, 0x02, 0x01, 0x01]) + key_expected


def test_auto_security_access(monkeypatch, bus_factory):
    bus = bus_factory(bitrate=500000)
    client = UDSClient(bus, 0x7E0, 0x7E8)

    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))

    resp_seed1 = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x04, 0x67, 0x01, 0x12, 0x34, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_neg = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x03, 0x7F, 0x27, 0x35, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_seed2 = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x04, 0x67, 0x01, 0x56, 0x78, 0, 0, 0]),
        is_extended_id=False,
    )
    resp_key = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x02, 0x67, 0x02, 0, 0, 0, 0, 0]),
        is_extended_id=False,
    )
    responses = [resp_seed1, resp_neg, resp_seed2, resp_key]

    def fake_recv(timeout):
        return responses.pop(0)

    monkeypatch.setattr(bus, "recv", fake_recv)

    record = b"\x01\x01"
    key_ok = crc16_ccitt(b"\x56\x78", record)

    used_record, algo = auto_security_access(
        client, 1, [record], algorithms=[xor_invert, crc16_ccitt]
    )
    assert used_record == record
    assert algo is crc16_ccitt
    assert len(sent) == 4
    assert bytes(sent[-1].data)[:7] == bytes([0x06, 0x27, 0x02, 0x01, 0x01]) + key_ok
