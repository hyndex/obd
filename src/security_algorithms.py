"""Collection of simple seed-key derivation algorithms for UDS security access.

The functions here accept the seed bytes provided by the ECU and an optional
``data_record`` which may contain supplier specific selectors.  Each algorithm
returns the key bytes to be echoed in the SecurityAccess request.
"""
from __future__ import annotations

from typing import Iterable


def xor_invert(seed: bytes, data_record: bytes = b"") -> bytes:
    """Return the bitwise inversion of ``seed``.

    ``data_record`` is ignored and only present for API compatibility.
    """

    return bytes((b ^ 0xFF) & 0xFF for b in seed)


def add_rotate(seed: bytes, data_record: bytes = b"") -> bytes:
    """Example 16-bit additive/rotate transform.

    The seed is interpreted as a big endian 16-bit value.  A constant is added
    then the value is rotated left by three bits.
    """

    if len(seed) < 2:
        raise ValueError("seed must be at least two bytes")
    val = int.from_bytes(seed[:2], "big")
    val = (val + 0x4D2B) & 0xFFFF
    val = ((val << 3) | (val >> 13)) & 0xFFFF
    return val.to_bytes(2, "big")


def crc16_ccitt(seed: bytes, data_record: bytes = b"") -> bytes:
    """Compute a CRC16-CCITT over ``seed`` + ``data_record``."""

    data = seed + data_record
    poly = 0x1021
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")


# List of algorithms in recommended trial order
DEFAULT_ALGOS: Iterable = (crc16_ccitt, xor_invert, add_rotate)
