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
import time
import threading
import queue
from logging.handlers import RotatingFileHandler
from typing import Optional

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


def monitor(
    bus: "can.BusABC",
    db: Optional[Database],
    logger: logging.Logger,
    *,
    serializer: Optional[str] = None,
    transport: Optional[Transport] = None,
    print_raw: bool = False,
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
    try:
        while True:
            msg = bus.recv(timeout=1.0)
            if msg is None:
                if is_bus_off(bus):
                    record_bus_error()
                    raise can.CanError("Bus-off state detected")
                time.sleep(0.1)
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

            if print_raw:
                line = f"id=0x{fmt%msg.arbitration_id} raw={raw}"
                if decoded is not None:
                    line += f" decoded={decoded}"
                logger.info(line)
            elif decoded is not None:
                logger.info(
                    "id=0x%s decoded=%s", fmt % msg.arbitration_id, decoded
                )

            if send_queue is not None:
                payload = serialize_frame(
                    msg.arbitration_id, msg.data, decoded, serializer  # type: ignore[arg-type]
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
        "--bitrate", type=int, default=500000, help="CAN bitrate in bits per second"
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

    config: dict[str, str] = {}
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

    dbc_path = Path(__file__).with_name("OBD.dbc")
    db = load_dbc(str(dbc_path))
    if db is None:
        logger.warning("DBC not loaded, decoding will be skipped!")
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
                monitor(bus, db, logger, print_raw=args.print_raw)
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
