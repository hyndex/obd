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
import subprocess
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

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


def setup_interface(interface: str, bitrate: int, listen_only: bool) -> None:
    """Configure the SocketCAN interface using ``modprobe`` and ``ip link``.

    Parameters
    ----------
    interface:
        Name of the interface (e.g. ``"can0"``).
    bitrate:
        Bus bitrate in bits per second.
    listen_only:
        If ``True``, the interface is placed in listen-only mode.

    Notes
    -----
    ``subprocess`` is used so that the module can auto-recover by rerunning
    setup commands.  Each command failure is logged but does not raise, so that
    the caller can decide how to proceed.
    """

    commands = [
        ["modprobe", "can"],
        ["modprobe", "can_raw"],
        ["ip", "link", "set", interface, "down"],
        [
            "ip",
            "link",
            "set",
            interface,
            "up",
            "type",
            "can",
            "bitrate",
            str(bitrate),
        ],
    ]

    if listen_only:
        commands[-1].extend(["listen-only", "on"])

    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:  # pragma: no cover - system dependent
            logging.warning("Command failed (%s): %s", " ".join(cmd), exc)


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
        logging.warning("DBC file not found: %%s", dbc_path)
    except Exception as exc:  # pragma: no cover - cantools errors
        logging.warning("Failed to load DBC: %%s", exc)
    return None


def monitor(bus: "can.BusABC", db: Optional[Database], logger: logging.Logger) -> None:
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

    while True:
        msg = bus.recv(timeout=1.0)
        if msg is None:
            continue

        decoded = None
        if db:
            try:
                decoded = db.decode_message(msg.arbitration_id, msg.data)
            except Exception:  # pragma: no cover - depends on DBC
                logger.debug("No DBC entry for id=0x%%03X", msg.arbitration_id)

        logger.info(
            "id=0x%%03X raw=%%s decoded=%%s",
            msg.arbitration_id,
            msg.data.hex(),
            decoded,
        )

        if getattr(bus, "state", None) == can.bus.BusState.BUS_OFF:
            raise can.CanError("Bus-off state detected")


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for command-line execution."""

    parser = argparse.ArgumentParser(description="Monitor a SocketCAN bus and decode messages")
    parser.add_argument("--bitrate", type=int, default=500000, help="CAN bitrate in bits per second")
    parser.add_argument("--interface", default="can0", help="SocketCAN interface to use")
    parser.add_argument("--log", dest="log_path", default="can.log", help="Path to log file")
    parser.add_argument("--listen-only", action="store_true", help="Enable listen-only mode")
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

    while True:
        try:
            with can.interface.Bus(bustype="socketcan", channel=args.interface, receive_own_messages=False) as bus:
                logger.info("Connected to %%s", args.interface)
                monitor(bus, db, logger)
        except can.CanError as exc:  # pragma: no cover - runtime CAN errors
            logger.error("CAN error: %%s. Restarting interface...", exc)
            time.sleep(1)
            setup_interface(args.interface, args.bitrate, args.listen_only)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as exc:  # pragma: no cover - unexpected
            logger.exception("Unexpected error: %%s", exc)
            time.sleep(1)
            setup_interface(args.interface, args.bitrate, args.listen_only)

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
