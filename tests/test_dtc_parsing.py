import logging

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
