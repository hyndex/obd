import logging

import pytest

import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

import can_monitor  # noqa: E402
from can_monitor import _process_uds_payload, _convert_to_pcode  # noqa: E402


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
def test_process_uds_payload_sets_locked_out(nrc, caplog):
    can_monitor.uds_locked_out = False
    state = {}
    payload = bytes([0x7F, 0x27, nrc])
    logger = logging.getLogger("test")
    with caplog.at_level(logging.WARNING):
        _process_uds_payload(payload, state, {}, logger)
    assert state.get("uds_locked_out") is True
    assert can_monitor.uds_locked_out is True
    assert "locked out" in caplog.text.lower()


def test_ignore_p0000(caplog):
    payload = bytes.fromhex("59 02 FF 00 00 00 10")
    state = {}
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, {}, logger)
    assert caplog.text == ""


def test_empty_payload(caplog):
    state = {}
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(b"", state, {}, logger)
    assert caplog.text == ""


def test_convert_to_pcode_insufficient_bytes():
    assert _convert_to_pcode(b"\x12") is None


def test_unknown_dtc_logged_once(caplog):
    payload = bytes.fromhex("59 02 FF 12 34 00 10")
    state = {}
    logger = logging.getLogger("test")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(payload, state, {}, logger)
    assert "P1234" in caplog.text
    assert caplog.records[0].levelname == "INFO"

    caplog.clear()
    # Clear DTCs to allow re-logging at debug on reappearance
    empty = bytes.fromhex("59 02 FF")
    with caplog.at_level(logging.INFO):
        _process_uds_payload(empty, state, {}, logger)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        _process_uds_payload(payload, state, {}, logger)
    assert any(r.levelname == "DEBUG" and "P1234" in r.message for r in caplog.records)
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


def test_persistent_alert_ignores_other_dtc_changes(caplog):
    critical_only = bytes.fromhex("59 02 FF 05 8D 00 10")
    with_extra = bytes.fromhex("59 02 FF 05 8D 00 10 12 34 00 10")
    clear = bytes.fromhex("59 02 FF")
    state: dict[str, Any] = {}
    uds_config = {
        "dtcs": {
            "P058D": {
                "description": "Dual Aux communication failure",
                "severity": "CRITICAL",
                "alert": True,
                "component": "AUX",
            },
            "P1234": {
                "description": "Example DTC",
                "severity": "MAJOR",
                "component": "MISC",
            },
        }
    }
    logger = logging.getLogger("test")

    with caplog.at_level(logging.ERROR):
        _process_uds_payload(critical_only, state, uds_config, logger)
    assert caplog.text.count("ALERT") == 1

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(with_extra, state, uds_config, logger)
    assert "ALERT" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(critical_only, state, uds_config, logger)
    assert "ALERT" not in caplog.text

    _process_uds_payload(clear, state, uds_config, logger)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _process_uds_payload(critical_only, state, uds_config, logger)
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
