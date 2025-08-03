"""CAN bus monitor for SocketCAN interfaces.

This module sets up a SocketCAN interface, loads a DBC file, and
continuously logs raw and decoded CAN messages.  It is intended as a
maintenance tool and example implementation for developers working with
CAN buses on Linux.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
except ImportError:  # pragma: no cover - dependency is optional at import time
    can = None  # type: ignore

try:
    import cantools
    from cantools.database import Database
except ImportError:  # pragma: no cover
    cantools = None  # type: ignore
    Database = None  # type: ignore


def load_dbc(dbc_path: str) -> Optional[Database]:
    """Load a DBC file if available.

    Parameters
    ----------
    dbc_path:
        File system path to the DBC file.

    Returns
    -------
    ``cantools.database.Database`` or ``None`` if loading fails.
    """

    if not cantools:
        logging.warning("cantools library not installed; decoding disabled")
        return None

    try:
        return cantools.database.load_file(dbc_path)
    except FileNotFoundError:
        logging.warning("DBC file not found: %s", dbc_path)
    except Exception as exc:  # pragma: no cover - cantools errors
        logging.warning("Failed to load DBC: %s", exc)
    return None


def monitor(
    bus: "can.BusABC",
    db: Optional[Database],
    logger: logging.Logger,
    *,
    serializer: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> None:
    """Continuously read from the bus and log frames.

    Parameters
    ----------
    bus:
        An open ``can.Bus`` instance.
    db:
        Parsed DBC database or ``None`` if decoding is unavailable.
    logger:
        Logger used for output.
    """

    send_queue: queue.Queue[str] | None = None
    if serializer and transport:
        send_queue = queue.Queue(maxsize=1000)

        def _worker() -> None:
            while True:
                payload = send_queue.get()
                try:
                    transport.send(payload)
                except Exception:  # pragma: no cover - network errors
                    logger.error("Transport error", exc_info=True)
                finally:
                    send_queue.task_done()

        threading.Thread(target=_worker, daemon=True).start()

    try:
        while True:
            msg = bus.recv(timeout=1.0)
            if msg is None:
                # Avoid busy-looping when no frames are available
                if getattr(bus, "state", None) == can.bus.BusState.BUS_OFF:
                    record_bus_error()
                    raise can.CanError("Bus-off state detected")
                time.sleep(0.1)
                continue

            decoded = None
            if db:
                try:
                    decoded = db.decode_message(
                        msg.arbitration_id,
                        msg.data,
                        decode_choices=True,
                    )
                except KeyError:
                    record_decoding_failure()
                    logger.debug("No DBC entry for id=0x%03X", msg.arbitration_id)
                except Exception as exc:  # pragma: no cover - depends on DBC
                    record_decoding_failure()
                    logger.warning(
                        "Decoding error for id=0x%03X: %s", msg.arbitration_id, exc
                    )

            id_fmt = "%08X" if getattr(msg, "is_extended_id", False) else "%03X"
            logger.info(
                "id=0x%s raw=%s decoded=%s",
                id_fmt % msg.arbitration_id,
                msg.data.hex(),
                decoded,
            )

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

            if getattr(bus, "state", None) == can.bus.BusState.BUS_OFF:
                record_bus_error()
                raise can.CanError("Bus-off state detected")
    finally:
        if send_queue is not None:
            send_queue.join()


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for command-line execution."""

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
    level = getattr(logging, str(level_name).upper(), logging.INFO)

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

    setup_interface(args.interface, args.bitrate, args.listen_only)

    dbc_path = os.path.join(os.path.dirname(__file__), "OBD.dbc")
    db = load_dbc(dbc_path)

    if can is None:
        logger.error("python-can is required but not installed")
        return 1

    delay = 1.0
    while True:
        try:
            with can.interface.Bus(
                bustype="socketcan", channel=args.interface, receive_own_messages=False
            ) as bus:
                logger.info("Connected to %s", args.interface)
                monitor(bus, db, logger)
                delay = 1.0
        except can.CanError as exc:  # pragma: no cover - runtime CAN errors
            record_bus_error()
            logger.error("CAN error: %s. Restarting interface...", exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            setup_interface(args.interface, args.bitrate, args.listen_only)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as exc:  # pragma: no cover - unexpected
            record_bus_error()
            logger.exception("Unexpected error: %s", exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            record_restart()
            setup_interface(args.interface, args.bitrate, args.listen_only)

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
