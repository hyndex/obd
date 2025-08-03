import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from blf_decoder import decode_blf  # noqa: E402


def test_decode_blf_first_frame():
    blf_path = os.path.join(
        os.path.dirname(__file__), "..", "PV11-yadwad_0004465_20250102_012231.blf"
    )
    dbc_path = os.path.join(os.path.dirname(__file__), "..", "src", "OBD.dbc")
    iterator = decode_blf(blf_path, dbc_path)
    msg, decoded = next(iterator)
    assert msg.arbitration_id == 0x0C08A7F0
    assert isinstance(decoded, dict)
    assert decoded.get("MCU_TrqEst") == 0.0
