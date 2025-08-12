"""Helpers to automatically determine security access settings for a VCU.

The routines here orchestrate the ISO 15765-2/UDS handshake used by the
repository's diagnostics.  They try multiple data-record and key derivation
algorithms until the ECU grants security access.  This mirrors the manual
sequence used during reverse engineering:

1. enter extended diagnostic session
2. request seed and compute key
3. read diagnostic trouble codes
"""
from __future__ import annotations

from typing import Iterable, Callable, Tuple

from uds import UDSClient, ISOTransportError
from security_algorithms import DEFAULT_ALGOS


def auto_security_access(
    client: UDSClient,
    level: int,
    data_records: Iterable[bytes],
    algorithms: Iterable[Callable[[bytes, bytes], bytes]] = DEFAULT_ALGOS,
    timeout: float = 1.0,
) -> Tuple[bytes, Callable[[bytes, bytes], bytes]]:
    """Attempt security access using all combinations of records and algorithms.

    Parameters
    ----------
    client:
        Connected :class:`UDSClient` instance.
    level:
        Security access level to request.
    data_records:
        Iterable of supplier-specific data-record byte sequences to try.
    algorithms:
        Iterable of key-derivation callables.  Each receives ``(seed,
        data_record)`` and returns the key bytes.
    timeout:
        Per-request timeout in seconds.

    Returns
    -------
    Tuple of the successful ``data_record`` and algorithm.

    Raises
    ------
    ISOTransportError
        If none of the combinations grant security access.
    """

    for record in data_records:
        for algo in algorithms:
            try:
                client.security_access(
                    level,
                    data_record=record,
                    key_algo=algo,
                    timeout=timeout,
                )
                return record, algo
            except ISOTransportError as exc:  # pragma: no cover - exercised via tests
                msg = str(exc)
                if any(code in msg for code in ("0x35", "0x33", "0x24")):
                    continue
                raise
    raise ISOTransportError("Unable to determine security settings")


def run_basic_sequence(
    client: UDSClient,
    *,
    session: int = 3,
    level: int = 1,
    data_records: Iterable[bytes] = (b"\x01\x01",),
    algorithms: Iterable[Callable[[bytes, bytes], bytes]] = DEFAULT_ALGOS,
    timeout: float = 1.0,
) -> None:
    """Execute the standard VCU handshake and read DTCs.

    This function is intentionally minimal and suitable as a starting point for
    integration tests.  It performs the sequence:

    1. change diagnostic session
    2. automatically unlock security access
    3. read DTCs twice (emulating the observed VCU behaviour)
    """

    client.change_session(session, timeout)
    auto_security_access(client, level, data_records, algorithms, timeout)
    client.request(0x19, b"\x01\x01", timeout)
    client.request(0x19, b"\x01\x01", timeout)
