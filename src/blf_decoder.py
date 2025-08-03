"""Decode BLF log files using a DBC database.

This helper module provides a small API and command-line interface for
converting Vector BLF log files into decoded CAN frames using the project's
``OBD.dbc`` database.  It is intentionally lightweight so it can be used
in scripts or during testing.
"""

from __future__ import annotations

import argparse
from typing import Iterator, Tuple, Optional, Any

try:  # pragma: no cover - import error handled in runtime environments
    import can
    import cantools
except Exception as exc:  # pragma: no cover - dependency resolution
    raise RuntimeError("cantools and python-can are required for BLF decoding") from exc


def decode_blf(
    blf_path: str, dbc_path: str
) -> Iterator[Tuple["can.Message", Optional[Any]]]:
    """Iterate over messages in ``blf_path`` decoding with ``dbc_path``.

    Parameters
    ----------
    blf_path:
        Path to the BLF log file.
    dbc_path:
        Path to the DBC database used for decoding.
    """
    db = cantools.database.load_file(dbc_path)
    reader = can.BLFReader(blf_path)
    for msg in reader:
        try:
            decoded = db.decode_message(msg.arbitration_id, msg.data)
        except Exception:
            decoded = None
        yield msg, decoded


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - CLI glue
    parser = argparse.ArgumentParser(description="Decode BLF log using OBD DBC")
    parser.add_argument("blf", help="Path to BLF log file")
    parser.add_argument(
        "--dbc",
        default=str(__file__).replace("blf_decoder.py", "OBD.dbc"),
        help="Path to DBC file (default: OBD.dbc in source directory)",
    )
    args = parser.parse_args(argv)

    for msg, decoded in decode_blf(args.blf, args.dbc):
        fmt = "08X" if getattr(msg, "is_extended_id", False) else "03X"
        print(
            f"id=0x{msg.arbitration_id:{fmt}} data={msg.data.hex()} decoded={decoded}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
