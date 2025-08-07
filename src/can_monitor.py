#!/usr/bin/env python3
"""CAN bus monitor for SocketCAN interfaces.

This module sets up a SocketCAN interface, loads a DBC file, and
continuously logs raw and decoded CAN messages.  It includes support
for listen-only mode on modern python-can versions and tolerates removal
of the BUS_OFF enum, preventing controller lockouts.
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
    payload: bytes, uds_config: dict[str, Any], logger: logging.Logger
) -> None:
    """Parse a complete UDS payload containing DTC information."""
    if len(payload) < 3:
        return
    if payload[0] == 0x59 and payload[1] == 0x02:
        dtc_count = payload[2]
        entries = payload[3:]
        for i in range(dtc_count):
            start = i * 4
            if start + 4 > len(entries):
                break
            code = _convert_to_pcode(entries[start : start + 3])  # noqa: E203
            info = uds_config.get("dtcs", {}).get(code)
            if info:
                desc = info.get("description", "")
                severity = info.get("severity", "INFO")
                component = info.get("component", "Unknown")
                alert = info.get("alert", False) or severity.upper() == "CRITICAL"
            else:
                desc = "Unknown DTC"
                severity = "UNKNOWN"
                component = "Unknown"
                alert = False
            logger.info(
                "DTC %s (%s): %s [Severity: %s]",
                code,
                component,
                desc,
                severity,
            )
            if alert:
                logger.error("*** ALERT: Critical DTC %s detected - %s ***", code, desc)


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
    pci = data[0]
    frame_type = pci >> 4
    if frame_type == 0x0:  # single frame
        length = pci & 0xF
        payload = data[1 : 1 + length]  # noqa: E203
        _process_uds_payload(payload, uds_config, logger)
        return True
    if frame_type == 0x1:  # first frame
        length = ((pci & 0xF) << 8) | data[1]
        state["payload"] = bytearray(data[2:])
        state["expected"] = length - len(state["payload"])
        if ecu_req_id is not None:
            fc = can.Message(
                arbitration_id=ecu_req_id,
                data=bytes([0x30, block_size & 0xFF, st_min & 0xFF, 0, 0, 0, 0, 0]),
                is_extended_id=False,
            )
            bus.send(fc)
        return True
    if frame_type == 0x2 and state.get("expected", 0) > 0:  # consecutive frame
        take = min(state["expected"], 7)
        state["payload"].extend(data[1 : 1 + take])  # noqa: E203
        state["expected"] -= take
        if state["expected"] <= 0:
            _process_uds_payload(bytes(state["payload"]), uds_config, logger)
            state["payload"] = bytearray()
            state["expected"] = 0
        return True
    return False


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
                    logger.warning(
                        "Decoding error for id=0x%s: %s", fmt % msg.arbitration_id, exc
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
                        logger.warning(
                            "Decoding error for id=0x%s: %s",
                            fmt % msg.arbitration_id,
                            exc,
                        )
                if decoded is None:
                    record_decoding_failure()
                    if msg.arbitration_id not in missing_ids:
                        missing_ids.add(msg.arbitration_id)
                        logger.info(
                            "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                        )
                    else:
                        logger.debug(
                            "No DBC entry for id=0x%s", fmt % msg.arbitration_id
                        )

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


def main(argv: Optional[list[str]] = None) -> int:
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

    dbc_path = Path(__file__).with_name("OBD.dbc")
    db = load_dbc(str(dbc_path))
    fallback_dbs: list[Database] = []
    if db is None:
        logger.warning("Custom DBC failed to load; attempting opendbc fallback")
    else:
        logger.info("DBC loaded with %d messages", len(db.messages))

    # bring up the CAN interface
    setup_interface(args.interface, args.bitrate, args.listen_only)

    if can is None:
        logger.error("python-can is required but not installed")
        return 1

    delay = 1.0
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
                if db is None and not fallback_dbs:
                    db, fallback_dbs = load_opendbc_dbs(bus)
                monitor(
                    bus,
                    db,
                    logger,
                    print_raw=args.print_raw,
                    fallback_dbs=fallback_dbs,
                    uds_config=uds_cfg,
                )
                delay = 1.0
        except can.CanError as exc:
            record_bus_error()
            logger.error("CAN error: %s. Restarting interface...", exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            setup_interface(args.interface, args.bitrate, args.listen_only)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as exc:
            record_bus_error()
            logger.exception("Unexpected error: %s", exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            setup_interface(args.interface, args.bitrate, args.listen_only)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
