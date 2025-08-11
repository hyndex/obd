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


def test_ignore_p0000(caplog):
    payload = bytes.fromhex("59 02 FF 00 00 00 10")
    state = {}
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, {}, logger)
    assert caplog.text == ""


def test_unknown_dtc_logged_once(caplog):
    payload = bytes.fromhex("59 02 FF 12 34 00 10")
    state = {}
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, {}, logger)
    assert "P1234" in caplog.text

    caplog.clear()
    # Clear DTCs to allow re-logging at debug on reappearance
    empty = bytes.fromhex("59 02 FF")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(empty, state, {}, logger)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        _process_uds_payload(payload, state, {}, logger)
    assert "P1234" in caplog.text
    assert "DTC set unchanged" not in caplog.text


def test_alert_only_on_change(caplog):
    payload = bytes.fromhex("59 02 FF 05 8D 00 10")
    state = {}
    uds_config = {
        "dtcs": {
            "P058D": {
                "description": "Dual Aux communication failure",
                "severity": "CRITICAL",
                "alert": True,
                "component": "AUX",
            }
        }
    }
    logger = logging.getLogger("test")
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(payload, state, uds_config, logger)
    assert caplog.text.count("ALERT") == 1

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(payload, state, uds_config, logger)
    assert "ALERT" not in caplog.text

    # Clear and trigger again
    clear = bytes.fromhex("59 02 FF")
    _process_uds_payload(clear, state, uds_config, logger)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(payload, state, uds_config, logger)
    assert caplog.text.count("ALERT") == 1


def test_dtc_set_logged_only_on_change(caplog):
    payload = bytes.fromhex("59 02 FF 05 8D 00 10")
    state = {}
    uds_config = {
        "dtcs": {
            "P058D": {
                "description": "Dual Aux communication failure",
                "severity": "MAJOR",
                "component": "AUX",
            }
        }
    }
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, uds_config, logger)
    assert "P058D" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, uds_config, logger)
    assert caplog.text == ""
    with caplog.at_level(logging.DEBUG):
        _process_uds_payload(payload, state, uds_config, logger)
    assert "DTC set unchanged" in caplog.text
