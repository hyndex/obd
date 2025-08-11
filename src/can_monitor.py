#!/usr/bin/env python3
"""CAN bus monitor for SocketCAN interfaces.

This module sets up a SocketCAN interface, loads a DBC file, and
continuously logs raw and decoded CAN messages.  Configuring the
interface requires root privileges or the ``CAP_NET_ADMIN`` capability.
It includes support for listen-only mode on modern python-can versions
and tolerates removal of the BUS_OFF enum, preventing controller lockouts.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import os
import time
import threading
import queue
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from serialization import serialize_frame
from transport import Transport
from canbus import setup_interface
from metrics import (
    record_bus_error,
    record_decoding_failure,
    record_restart,
    reset_metrics,
)
from uds import ISOTransportError, UDSClient

try:
    import can
except ImportError:
    can = None  # type: ignore

try:
    import cantools
    from cantools.database import Database
except ImportError:
    cantools = None  # type: ignore
    Database = None  # type: ignore


uds_locked_out = False


def apply_patches(bus: "can.BusABC", patches: dict[str, Any]) -> None:
    """Send one-shot frames defined in configuration."""
    for name, p in patches.items():
        msg = can.Message(
            arbitration_id=p["can_id"],
            data=bytes.fromhex(p["payload"]),
            is_extended_id=False,
        )
        for _ in range(p.get("retries", 1)):
            bus.send(msg, timeout=0.2)
            rsp = bus.recv(timeout=p.get("timeout_ms", 300) / 1000)
            if rsp and rsp.arbitration_id == p["response_id"]:
                logging.info("Patch '%s' applied (got 0x%02X)", name, rsp.data[0])
                break
        else:
            logging.warning("Patch '%s' failed – no response", name)


def load_dbc(dbc_path: str) -> Optional[Database]:
    if not cantools:
        logging.warning("cantools library not installed; decoding disabled")
        return None

    try:
        return cantools.database.load_file(dbc_path)
    except FileNotFoundError:
        logging.warning("DBC file not found: %s", dbc_path)
    except Exception as exc:
        logging.warning("Failed to load DBC: %s", exc)
    return None


def select_best_dbc(dbc_paths: list[str], bus: "can.BusABC") -> Optional[str]:
    """Pick the DBC file that best matches observed CAN traffic."""
    if not cantools:
        return None

    seen_ids: dict[int, int] = {}
    for _ in range(100):
        msg = bus.recv(timeout=0.1)
        if not msg:
            continue
        seen_ids[msg.arbitration_id] = len(msg.data)

    best_score = 0
    best_path: Optional[str] = None
    for path in dbc_paths:
        try:
            candb = cantools.database.load_file(path)
        except Exception:
            continue
        score = 0
        for mid, dlc in seen_ids.items():
            try:
                msg_def = candb.get_message_by_frame_id(mid)
            except KeyError:
                msg_def = None
            if msg_def and msg_def.length == dlc:
                score += 1
        if score > best_score:
            best_score = score
            best_path = path
    return best_path


def load_opendbc_dbs(bus: "can.BusABC") -> tuple[Optional[Database], list[Database]]:
    """Load opendbc databases either as a single best match or a list."""
    if not cantools:
        logging.warning("cantools library not installed; decoding disabled")
        return None, []

    try:
        import opendbc  # type: ignore
    except Exception:
        logging.error("Install commaai/opendbc to enable DBC fallback decoding")
        return None, []

    dbc_paths = [
        os.path.join(root, f)
        for root, _, files in os.walk(opendbc.DBC_PATH)
        for f in files
        if f.endswith(".dbc")
    ]
    logging.info("Found %d DBC files in opendbc", len(dbc_paths))

    selected = select_best_dbc(dbc_paths, bus)
    if selected:
        db = cantools.database.load_file(selected)
        logging.info("Loaded fallback DBC: %s", os.path.basename(selected))
        return db, []

    fallback_dbs = [cantools.database.load_file(p) for p in dbc_paths]
    logging.info("Loaded all opendbc DBC files for decoding fallback")
    return None, fallback_dbs


def _convert_to_pcode(code_bytes: bytes) -> str:
    """Convert three raw DTC bytes to standard Pxxxx style code."""
    if len(code_bytes) < 2:
        return "P0000"
    value = (code_bytes[0] << 8) | code_bytes[1]
    letter_map = {0: "P", 1: "C", 2: "B", 3: "U"}
    letter = letter_map.get((value >> 14) & 0x3, "P")
    digits = value & 0x3FFF
    return f"{letter}{digits:04X}"


def _process_uds_payload(
    payload: bytes,
    state: dict[str, Any],
    uds_config: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Parse a complete UDS payload containing DTC information."""
    if not payload:
        return
    # negative response handling
    if payload[0] == 0x7F and len(payload) >= 3:
        orig_sid = payload[1]
        nrc = payload[2]
        nrc_map = {
            0x10: "General Reject",
            0x11: "Service Not Supported",
            0x12: "Sub-function Not Supported",
            0x13: "Incorrect Length or Format",
            0x22: "Conditions Not Correct",
            0x31: "Request Out Of Range",
            0x33: "Security Access Denied",
            0x35: "Invalid Key",
            0x36: "Exceeded Number of Attempts",
            0x37: "Time Delay Not Expired",
            0x7E: "Sub-function Not Supported In Active Session",
            0x78: "Response Pending",
        }
        desc = nrc_map.get(nrc, "Unknown NRC")
        logger.warning(
            "UDS Negative Response: Service 0x%02X, NRC 0x%02X (%s)",
            orig_sid,
            nrc,
            desc,
        )
        if nrc in (0x36, 0x37):
            state["uds_locked_out"] = True
            global uds_locked_out
            uds_locked_out = True
        if nrc == 0x78:
            # signal to the caller that a final response is pending
            state["pending"] = True
        return

    if len(payload) < 3:
        return
    if payload[0] == 0x59 and payload[1] == 0x02:
        # Response to reportDTCByStatusMask. Byte 2 is the status
        # availability mask, followed by repeated <DTC+status> records.
        entries = payload[3:]
        dtc_count = len(entries) // 4

        seen_unknown = state.setdefault("seen_unknown_dtcs", set())
        active_alerts = state.setdefault("active_alerts", set())
        parsed: list[dict[str, Any]] = []
        current_codes: set[str] = set()
        current_alerts: set[str] = set()

        for i in range(dtc_count):
            start = i * 4
            code = _convert_to_pcode(entries[start : start + 3])  # noqa: E203
            if code == "P0000":
                continue
            info = uds_config.get("dtcs", {}).get(code)
            if info:
                desc = info.get("description", "")
                severity = info.get("severity", "INFO")
                component = info.get("component", "Unknown")
                alert = info.get("alert", False) or severity.upper() == "CRITICAL"
                known = True
            else:
                desc = "Unknown DTC"
                severity = "UNKNOWN"
                component = "Unknown"
                alert = False
                known = False
            parsed.append(
                {
                    "code": code,
                    "desc": desc,
                    "severity": severity,
                    "component": component,
                    "alert": alert,
                    "known": known,
                }
            )
            current_codes.add(code)
            if alert:
                current_alerts.add(code)

        last_codes = state.get("last_dtcs")
        if last_codes == current_codes:
            logger.debug("DTC set unchanged (%d codes)", len(current_codes))
            return
        state["last_dtcs"] = current_codes

        for cleared in active_alerts - current_alerts:
            active_alerts.remove(cleared)

        for entry in parsed:
            code = entry["code"]
            desc = entry["desc"]
            severity = entry["severity"]
            component = entry["component"]
            alert = entry["alert"]
            known = entry["known"]
            if not known:
                if code in seen_unknown:
                    logger.debug(
                        "DTC %s (%s): %s [Severity: %s]",
                        code,
                        component,
                        desc,
                        severity,
                    )
                else:
                    seen_unknown.add(code)
                    logger.info(
                        "DTC %s (%s): %s [Severity: %s]",
                        code,
                        component,
                        desc,
                        severity,
                    )
            else:
                logger.info(
                    "DTC %s (%s): %s [Severity: %s]",
                    code,
                    component,
                    desc,
                    severity,
                )
            if alert and code not in active_alerts:
                logger.error("*** ALERT: Critical DTC %s detected - %s ***", code, desc)
                active_alerts.add(code)


def _handle_uds_frame(
    bus: "can.BusABC",
    msg: "can.Message",
    state: dict[str, Any],
    ecu_req_id: Optional[int],
    block_size: int,
    st_min: int,
    uds_config: dict[str, Any],
    logger: logging.Logger,
) -> bool:
    """Handle ISO-TP reassembly and DTC processing.

    Returns True if the frame was consumed as part of UDS handling.
    """

    data = bytes(msg.data)
    if not data:
        return True
    addr_ext = None
    if uds_config and uds_config.get("address_extension") is not None:
        addr_ext = uds_config.get("address_extension")
        data = data[1:]
        if not data:
            return True
    pci = data[0]
    frame_type = pci >> 4
    # interleaved new message
    if frame_type in (0x0, 0x1) and state.get("expected", 0) > 0:
        state["payload"] = bytearray()
        state["expected"] = 0
        state.pop("next_seq", None)
        state.pop("bs_count", None)
    if frame_type == 0x0:  # single frame
        length = pci & 0xF
        payload = data[1 : 1 + length]  # noqa: E203
        _process_uds_payload(payload, state, uds_config, logger)
        return True
    if frame_type == 0x1:  # first frame
        length = ((pci & 0xF) << 8) | data[1]
        state["payload"] = bytearray(data[2:])
        state["expected"] = length - len(state["payload"])
        state["next_seq"] = 1
        state["bs_count"] = 0
        if ecu_req_id is not None:
            fc_data = bytes([0x30, block_size & 0xFF, st_min & 0xFF, 0, 0, 0, 0, 0])
            if addr_ext is not None:
                fc_data = bytes(
                    [addr_ext, 0x30, block_size & 0xFF, st_min & 0xFF, 0, 0, 0, 0]
                )
            fc = can.Message(
                arbitration_id=ecu_req_id,
                data=fc_data,
                is_extended_id=bool(ecu_req_id and ecu_req_id > 0x7FF),
            )
            bus.send(fc)
        return True
    if frame_type == 0x2 and state.get("expected", 0) > 0:  # consecutive frame
        seq = pci & 0x0F
        if seq != state.get("next_seq"):
            logger.warning(
                "Unexpected CF sequence: got %d expected %d", seq, state.get("next_seq")
            )
            state["payload"] = bytearray()
            state["expected"] = 0
            return True
        chunk_len = 7 if addr_ext is None else 6
        take = min(state["expected"], chunk_len)
        state["payload"].extend(data[1 : 1 + take])  # noqa: E203
        state["expected"] -= take
        state["next_seq"] = (state["next_seq"] + 1) & 0x0F
        state["bs_count"] = state.get("bs_count", 0) + 1
        if state["expected"] <= 0:
            _process_uds_payload(bytes(state["payload"]), state, uds_config, logger)
            state["payload"] = bytearray()
            state["expected"] = 0
            state.pop("next_seq", None)
            state.pop("bs_count", None)
        elif block_size > 0 and state["bs_count"] >= block_size:
            if ecu_req_id is not None:
                fc_data = bytes([0x30, block_size & 0xFF, st_min & 0xFF, 0, 0, 0, 0, 0])
                if addr_ext is not None:
                    fc_data = bytes(
                        [addr_ext, 0x30, block_size & 0xFF, st_min & 0xFF, 0, 0, 0, 0]
                    )
                fc = can.Message(
                    arbitration_id=ecu_req_id,
                    data=fc_data,
                    is_extended_id=bool(ecu_req_id and ecu_req_id > 0x7FF),
                )
                bus.send(fc)
            state["bs_count"] = 0
        return True
    return False


def _sequence_loop(
    bus: "can.BusABC",
    sequence: list[dict[str, Any]],
    interval_ms: int,
    uds_config: Optional[dict[str, Any]],
    logger: logging.Logger,
    stop_event: threading.Event,
) -> None:
    """Periodically send configured frames and handle responses."""
    block = uds_config.get("flow_control", {}).get("block_size", 0) if uds_config else 0
    st_min = uds_config.get("flow_control", {}).get("st_min_ms", 0) if uds_config else 0
    state = {"expected": 0, "payload": bytearray()}
    while not stop_event.is_set():
        start = time.time()
        for step in sequence:
            msg = can.Message(
                arbitration_id=step["can_id"],
                data=bytes.fromhex(step["payload"]),
                is_extended_id=bool(step.get("is_extended_id", step["can_id"] > 0x7FF)),
            )
            bus.send(msg)
            resp_id = step.get("response_id")
            if resp_id is not None:
                timeout = step.get("timeout_ms", 100) / 1000
                end_time = time.time() + timeout
                state["expected"] = 0
                state["payload"] = bytearray()
                while time.time() < end_time and not stop_event.is_set():
                    remaining = end_time - time.time()
                    rsp = bus.recv(timeout=remaining)
                    if not rsp or rsp.arbitration_id != resp_id:
                        continue
                    if (
                        _handle_uds_frame(
                            bus,
                            rsp,
                            state,
                            step["can_id"],
                            block,
                            st_min,
                            uds_config or {},
                            logger,
                        )
                        and state.get("expected", 0) <= 0
                        and not state.pop("pending", False)
                    ):
                        break
        elapsed = (time.time() - start) * 1000
        wait_ms = interval_ms - elapsed
        if wait_ms > 0:
            stop_event.wait(wait_ms / 1000)


def monitor(
    bus: "can.BusABC",
    db: Optional[Database],
    logger: logging.Logger,
    *,
    serializer: Optional[str] = None,
    transport: Optional[Transport] = None,
    print_raw: bool = False,
    fallback_dbs: Optional[list[Database]] = None,
    uds_config: Optional[dict[str, Any]] = None,
    sequence: Optional[list[dict[str, Any]]] = None,
    interval_ms: int = 500,
    log_obd: bool = True,
) -> None:
    send_queue: queue.Queue[str] | None = None
    if serializer and transport:
        send_queue = queue.Queue(maxsize=1000)

        def _worker() -> None:
            while True:
                payload = send_queue.get()
                try:
                    transport.send(payload)
                except Exception:
                    logger.error("Transport error", exc_info=True)
                finally:
                    send_queue.task_done()

        threading.Thread(target=_worker, daemon=True).start()

    # helper to compare bus-off state without enum error
    def is_bus_off(b: "can.BusABC") -> bool:
        try:
            return getattr(b, "state", None) == can.bus.BusState.BUS_OFF
        except Exception:
            return False

    seq_thread: threading.Thread | None = None
    seq_stop = threading.Event()
    if sequence:
        seq_thread = threading.Thread(
            target=_sequence_loop,
            args=(bus, sequence, interval_ms, uds_config, logger, seq_stop),
            daemon=True,
        )
        seq_thread.start()

    missing_ids: set[int] = set()

    ecu_resp_id = None
    ecu_req_id = None
    flow_block = 0
    flow_st = 0
    uds_state = {"expected": 0, "payload": bytearray()}
    if uds_config:
        ecu_resp_id = uds_config.get("ecu_response_id")
        ecu_req_id = uds_config.get("ecu_request_id")
        flow_block = uds_config.get("flow_control", {}).get("block_size", 0)
        flow_st = uds_config.get("flow_control", {}).get("st_min_ms", 0)
    try:
        while True:
            msg = bus.recv(timeout=1.0)
            if msg is None:
                if is_bus_off(bus):
                    record_bus_error()
                    raise can.CanError("Bus-off state detected")
                time.sleep(0.1)
                continue

            if uds_config and msg.arbitration_id == ecu_resp_id:
                if _handle_uds_frame(
                    bus,
                    msg,
                    uds_state,
                    ecu_req_id,
                    flow_block,
                    flow_st,
                    uds_config,
                    logger,
                ):
                    continue

            fmt = "%08X" if getattr(msg, "is_extended_id", False) else "%03X"
            raw = msg.data.hex()
            decoded = None

            if db:
                try:
                    decoded = db.decode_message(
                        msg.arbitration_id, msg.data, decode_choices=True
                    )
                except KeyError:
                    record_decoding_failure()
                    if log_obd:
                        if msg.arbitration_id not in missing_ids:
                            missing_ids.add(msg.arbitration_id)
                            logger.info(
                                "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                            )
                        else:
                            logger.debug(
                                "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                            )
                except Exception as exc:
                    record_decoding_failure()
                    if log_obd:
                        logger.warning(
                            "Decoding error for id=0x%s: %s",
                            fmt % msg.arbitration_id,
                            exc,
                        )
            elif fallback_dbs:
                for candb in fallback_dbs:
                    try:
                        decoded = candb.decode_message(
                            msg.arbitration_id, msg.data, decode_choices=True
                        )
                        break
                    except KeyError:
                        continue
                    except Exception as exc:
                        record_decoding_failure()
                        if log_obd:
                            logger.warning(
                                "Decoding error for id=0x%s: %s",
                                fmt % msg.arbitration_id,
                                exc,
                            )
                if decoded is None:
                    record_decoding_failure()
                    if log_obd:
                        if msg.arbitration_id not in missing_ids:
                            missing_ids.add(msg.arbitration_id)
                            logger.info(
                                "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                            )
                        else:
                            logger.debug(
                                "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                            )

            if log_obd:
                if print_raw:
                    line = f"id=0x{fmt % msg.arbitration_id} raw={raw}"
                    if decoded is not None:
                        line += f" decoded={decoded}"
                    logger.info(line)
                elif decoded is not None:
                    logger.info("id=0x%s decoded=%s", fmt % msg.arbitration_id, decoded)

            if send_queue is not None:
                payload = serialize_frame(
                    msg.arbitration_id,
                    msg.data,
                    decoded,
                    serializer,  # type: ignore[arg-type]
                )
                try:
                    send_queue.put_nowait(payload)
                except queue.Full:
                    logger.warning("Transport queue full; dropping frame")

            if is_bus_off(bus):
                record_bus_error()
                raise can.CanError("Bus-off state detected")

    finally:
        if send_queue is not None:
            send_queue.join()
        if seq_thread is not None:
            seq_stop.set()
            seq_thread.join()


def main(argv: Optional[list[str]] = None) -> int:
    global uds_locked_out
    parser = argparse.ArgumentParser(
        description="Monitor a SocketCAN bus and decode messages"
    )
    parser.add_argument(
        "--bitrate", type=int, default=250000, help="CAN bitrate in bits per second"
    )
    parser.add_argument(
        "--interface", default="can0", help="SocketCAN interface to use"
    )
    parser.add_argument(
        "--log", dest="log_path", default="can.log", help="Path to log file"
    )
    parser.add_argument(
        "--listen-only", action="store_true", help="Enable listen-only mode"
    )
    parser.add_argument(
        "--print-raw",
        action="store_true",
        help="Print raw CAN frames alongside decoded data",
    )
    parser.add_argument(
        "--uds-only",
        action="store_true",
        help="Only log UDS operations, suppress normal CAN frame logs",
    )
    parser.add_argument("--config", help="Path to JSON configuration file")
    parser.add_argument("--log-level", help="Logging level (e.g. INFO, DEBUG)")
    args = parser.parse_args(argv)

    config: dict[str, Any] = {}
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            print(f"Failed to load config file: {args.config}")

    level_name = args.log_level or config.get("log_level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    reset_metrics()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            RotatingFileHandler(args.log_path, maxBytes=1_000_000, backupCount=5),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger(__name__)

    uds_cfg = config.get("uds")
    sequence_cfg = config.get("sequence")
    interval_ms = config.get("interval_ms", 500)

    dbc_path = Path(__file__).with_name("OBD.dbc")
    db = load_dbc(str(dbc_path))
    fallback_dbs: list[Database] = []
    if db is None:
        logger.warning("Custom DBC failed to load; attempting opendbc fallback")
    else:
        logger.info("DBC loaded with %d messages", len(db.messages))

    max_failures = 3
    failure_count = 0

    def setup_with_counter() -> bool:
        nonlocal failure_count
        try:
            setup_interface(args.interface, args.bitrate, args.listen_only)
        except Exception as exc:  # pragma: no cover - log and count
            failure_count += 1
            logger.error("Failed to set up CAN interface: %s", exc)
            return False
        else:
            if failure_count > 0:
                logger.info("CAN interface reset and up")
            failure_count = 0
            return True

    delay = 1.0
    while not setup_with_counter():
        if failure_count >= max_failures:
            logger.fatal(
                "Exceeded maximum consecutive CAN errors (%d); exiting", max_failures
            )
            return 1
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
        record_restart()
    delay = 1.0

    if can is None:
        logger.error("python-can is required but not installed")
        return 1

    while True:
        try:
            with can.interface.Bus(
                interface="socketcan",
                channel=args.interface,
                bitrate=args.bitrate,
                receive_own_messages=False,
            ) as bus:
                logger.info("Connected to %s", args.interface)
                if "patches" in config:
                    apply_patches(bus, config["patches"])
                if uds_cfg:
                    try:
                        key_algo_spec = (
                            uds_cfg.get("security", {}).get("algorithm")
                            if uds_cfg
                            else None
                        )
                        client = UDSClient(
                            bus,
                            uds_cfg["ecu_request_id"],
                            uds_cfg["ecu_response_id"],
                            is_extended_id=uds_cfg.get("is_extended_id", False),
                            rx_block_size=uds_cfg.get("flow_control", {}).get(
                                "block_size", 0
                            ),
                            rx_st_min=uds_cfg.get("flow_control", {}).get(
                                "st_min_ms", 0
                            ),
                            key_algo=key_algo_spec,
                            source_address=uds_cfg.get("source_address"),
                            target_address=uds_cfg.get("target_address"),
                            address_extension=uds_cfg.get("address_extension"),
                        )
                        sess = uds_cfg.get("session")
                        if sess is not None:
                            if client.change_session(sess):
                                logger.info("UDS session %s established", sess)
                            else:
                                logger.warning("UDS session %s request failed", sess)
                        sec_cfg = uds_cfg.get("security") or {}
                        level = sec_cfg.get("level")
                        if level is not None and not uds_locked_out:
                            key_cfg = sec_cfg.get("key")
                            key = None
                            if isinstance(key_cfg, str):
                                key = bytes.fromhex(key_cfg)
                            elif isinstance(key_cfg, list):
                                key = bytes(int(b) & 0xFF for b in key_cfg)
                            try:
                                client.security_access(level, key)
                                logger.info("UDS security level %s unlocked", level)
                            except ISOTransportError as exc:
                                logger.warning(
                                    "UDS security level %s denied: %s", level, exc
                                )
                                if any(code in str(exc) for code in ("0x36", "0x37")):
                                    uds_locked_out = True
                    except Exception as exc:  # pragma: no cover - best effort
                        logger.warning("UDS initialisation failed: %s", exc)
                if db is None and not fallback_dbs:
                    db, fallback_dbs = load_opendbc_dbs(bus)
                monitor(
                    bus,
                    db,
                    logger,
                    print_raw=args.print_raw,
                    fallback_dbs=fallback_dbs,
                    uds_config=uds_cfg,
                    sequence=sequence_cfg,
                    interval_ms=interval_ms,
                    log_obd=not args.uds_only,
                )
                delay = 1.0
        except can.CanError as exc:
            record_bus_error()
            logger.error("CAN error: %s. Restarting interface...", exc)
            failure_count += 1
            if failure_count >= max_failures:
                logger.fatal(
                    "Exceeded maximum consecutive CAN errors (%d); exiting",
                    max_failures,
                )
                return 1
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            while not setup_with_counter():
                if failure_count >= max_failures:
                    logger.fatal(
                        "Exceeded maximum consecutive CAN errors (%d); exiting",
                        max_failures,
                    )
                    return 1
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                record_restart()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as exc:
            record_bus_error()
            logger.exception("Unexpected error: %s", exc)
            failure_count += 1
            if failure_count >= max_failures:
                logger.fatal(
                    "Exceeded maximum consecutive CAN errors (%d); exiting",
                    max_failures,
                )
                return 1
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            while not setup_with_counter():
                if failure_count >= max_failures:
                    logger.fatal(
                        "Exceeded maximum consecutive CAN errors (%d); exiting",
                        max_failures,
                    )
                    return 1
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                record_restart()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
