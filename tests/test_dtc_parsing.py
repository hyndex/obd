import logging

import pytest

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

import can_monitor
from can_monitor import _process_uds_payload


def test_process_uds_payload_parses_dtc(caplog):
    payload = bytes.fromhex("59 02 FF 05 8D 00 10")
    state = {}
    uds_config = {
        "dtcs": {
            "P058D": {
                "description": "Dual Aux communication failure",
                "severity": "MAJOR",
                "alert": False,
                "component": "AUX",
            }
        }
    }
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, uds_config, logger)
    assert "DTC P058D" in caplog.text


@pytest.mark.parametrize("nrc", [0x36, 0x37])
def test_process_uds_payload_sets_locked_out(nrc):
    can_monitor.uds_locked_out = False
    state = {}
    payload = bytes([0x7F, 0x27, nrc])
    logger = logging.getLogger("test")
    _process_uds_payload(payload, state, {}, logger)
    assert state.get("uds_locked_out") is True
    assert can_monitor.uds_locked_out is True
