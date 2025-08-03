"""Utilities for serializing CAN frames."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Optional


def serialize_frame(arbitration_id: int, data: bytes, decoded: Optional[Dict[str, Any]], fmt: str) -> str:
    """Serialize a decoded CAN frame.

    Parameters
    ----------
    arbitration_id:
        Frame identifier.
    data:
        Raw data bytes.
    decoded:
        Decoded payload dictionary or ``None``.
    fmt:
        Serialization format: ``"json"`` or ``"csv"``.
    """
    frame = {"id": arbitration_id, "raw": data.hex(), "decoded": decoded}
    if fmt == "json":
        return json.dumps(frame)
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([frame["id"], frame["raw"], json.dumps(frame["decoded"])] )
        return output.getvalue().strip()
    raise ValueError(f"Unknown format: {fmt}")
