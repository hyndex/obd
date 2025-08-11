import sys
from pathlib import Path

import can

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from sequence_runner import SequenceRunner


def test_sequence_runner_sends_flow_control(monkeypatch):
    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    sequence = [
        {
            "name": "read_dtc",
            "can_id": 0x7E0,
            "payload": "03 19 02 FF 00 00 00 00",
            "response_id": 0x7E8,
        }
    ]
    sent = []
    monkeypatch.setattr(bus, "send", lambda msg, timeout=None: sent.append(msg))
    ff = can.Message(
        arbitration_id=0x7E8,
        data=bytes([0x10, 0x0A, 0x62, 0xF1, 0x90, 0x01, 0x02, 0x03]),
    )
    cf = can.Message(
        arbitration_id=0x7E8, data=bytes([0x21, 0x04, 0x05, 0x06, 0x07, 0, 0, 0])
    )
    recvs = [ff, cf]
    monkeypatch.setattr(bus, "recv", lambda timeout: recvs.pop(0) if recvs else None)
    runner = SequenceRunner(
        bus, sequence, flow_control={"block_size": 1, "st_min_ms": 1}
    )
    runner.run_once()
    assert len(sent) == 2
    assert sent[0].arbitration_id == 0x7E0
    assert sent[0].data == bytes.fromhex("03 19 02 FF 00 00 00 00")
    assert sent[1].arbitration_id == 0x7E0
    assert sent[1].data[0] == 0x30
    assert not recvs
