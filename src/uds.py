"""UDS client with ISO-TP transport support.

Note
----
The :class:`UDSClient` is **not** thread-safe. Concurrent calls to its
messaging methods must be serialized externally or they will raise a
``RuntimeError`` when a lock is in use.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Callable

try:
    import can
except ImportError:  # pragma: no cover - optional dependency
    can = None  # type: ignore

from isotp_primitives import TDataPrimitive

LOGGER = logging.getLogger(__name__)


def default_key_algo(seed: bytes) -> bytes:
    """Fallback key algorithm performing a bitwise inversion."""

    return bytes((b ^ 0xFF) & 0xFF for b in seed)


def _load_key_algo(
    spec: "Callable[[bytes], bytes] | str | None",
) -> "Callable[[bytes], bytes]":
    """Resolve a key algorithm from a callable or ``module:attr`` string."""

    if spec is None:
        return default_key_algo
    if callable(spec):
        return spec
    if isinstance(spec, str):
        module_name, _, attr = spec.partition(":")
        module = importlib.import_module(module_name)
        algo = getattr(module, attr) if attr else module
        if callable(algo):
            return algo  # type: ignore[arg-type]
        raise TypeError("Security key algorithm is not callable")
    raise TypeError("Invalid key algorithm specification")


class ISOTransportError(RuntimeError):
    """Raised when ISO-TP segmentation or flow control fails."""


def _calc_st_delay(byte: int) -> float:
    """Convert STmin byte to seconds."""
    if byte <= 0x7F:
        return byte / 1000.0
    if 0xF1 <= byte <= 0xF9:
        return (byte - 0xF0) / 10000.0
    return 0.0


class UDSClient:
    """Minimal UDS client implementing ISO-TP segmentation.

    The client is not thread-safe; calls to :meth:`send`, :meth:`receive`, and
    :meth:`request` are serialized via an internal :class:`threading.Lock` and
    will raise :class:`RuntimeError` if invoked concurrently.

    Parameters
    ----------
    bus: can.BusABC
        Underlying CAN bus implementation.
    req_id: int
        Arbitration ID used for requests sent to the ECU.
    resp_id: int
        Expected arbitration ID of ECU responses.
    is_extended_id: bool, optional
        Use 29-bit identifiers instead of 11-bit.  Default ``False``.
    rx_block_size: int, optional
        Block size to advertise when reassembling multi-frame responses.
    rx_st_min: int, optional
        Minimum separation time in milliseconds to advertise in flow
        control frames.
    wft_max: int, optional
        Maximum number of consecutive Flow Control WAIT frames permitted
        before aborting.  Default ``0`` per ISO-15765-2.
        key_algo: callable or ``module:attr`` string, optional
        Function applied to the received seed to generate the security
        access key. When ``None`` a default inversion-based algorithm is
        used.  If a string is supplied it is treated as ``module:attr`` and
        imported dynamically, allowing pluggable algorithms.
    source_address: int, optional
        8-bit source address used for normal-fixed addressing.  When both
        ``source_address`` and ``target_address`` are provided the
        arbitration identifiers are automatically derived using the
        29-bit normal-fixed scheme.
    target_address: int, optional
        8-bit target address for normal-fixed addressing.
    address_extension: int, optional
        Additional address byte prepended to each frame when operating in
        extended or mixed addressing modes.
    max_rx_size: int, optional
        Maximum number of bytes allowed when reassembling multi-frame
        responses.  When ``None`` no limit is enforced.
    on_reset: callable, optional
        Function invoked when reception is reset due to an unexpected
        start-of-frame.
    error_on_reset: bool, optional
        When ``True``, an :class:`ISOTransportError` is raised instead of
        logging a warning when a reset occurs.
    logger: logging.Logger, optional
        Logger used for debug output.  Defaults to a module-level logger.
    """

    def __init__(
        self,
        bus: "can.BusABC",
        req_id: int,
        resp_id: int,
        *,
        is_extended_id: bool = False,
        rx_block_size: int = 0,
        rx_st_min: int = 0,
        wft_max: int = 0,
        key_algo: "Callable[[bytes], bytes] | str | None" = None,
        source_address: "int | None" = None,
        target_address: "int | None" = None,
        address_extension: "int | None" = None,
        t_data: "TDataPrimitive | None" = None,
        max_rx_size: "int | None" = None,
        on_reset: "Callable[[], None] | None" = None,
        error_on_reset: bool = False,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.is_extended_id = is_extended_id
        self.rx_block_size = rx_block_size
        self.rx_st_min = rx_st_min
        self.wft_max = wft_max
        self._key_algo = _load_key_algo(key_algo)
        self.source_address = source_address
        self.target_address = target_address
        self.address_extension = address_extension
        self.t_data = t_data
        self.max_rx_size = max_rx_size
        self.on_reset = on_reset
        self.error_on_reset = error_on_reset
        self.logger = logger
        self._rx_fc_status = 0
        self._lock = threading.Lock()

        if self.source_address is not None and self.target_address is not None:
            base = 0x18DA
            self.req_id = (
                (base << 16) | (self.target_address << 8) | self.source_address
            )
            self.resp_id = (
                (base << 16) | (self.source_address << 8) | self.target_address
            )
            self.is_extended_id = True

    # ------------------------------------------------------------------
    # sending
    def _send(
        self, service: int, data: bytes, timeout: float | tuple[float, float] = 1.0
    ) -> bool:
        payload = bytes([service]) + data
        if len(payload) > 0xFFF:
            self.logger.debug("Payload too large: %d bytes", len(payload))
            raise ISOTransportError("Payload too large")
        if isinstance(timeout, tuple):
            fc_timeout, send_timeout = timeout
        else:
            fc_timeout = send_timeout = timeout
        single_limit = 7 if self.address_extension is None else 6
        try:
            if len(payload) <= single_limit:
                pci = len(payload) & 0x0F
                if self.address_extension is not None:
                    frame_data = (
                        bytes([self.address_extension, pci])
                        + payload
                        + bytes(single_limit - len(payload))
                    )
                else:
                    frame_data = (
                        bytes([pci]) + payload + bytes(single_limit - len(payload))
                    )
                frame = can.Message(
                    arbitration_id=self.req_id,
                    is_extended_id=self.is_extended_id,
                    data=frame_data,
                )
                self.bus.send(frame, timeout=send_timeout)
                self.logger.debug("Sent Single Frame: %s", frame.data.hex())
                if self.t_data and self.t_data.con:
                    self.t_data.con(True, None)
                return True

            total_len = len(payload)
            pci_high = 0x10 | ((total_len >> 8) & 0x0F)
            pci_low = total_len & 0xFF
            first_len = 6 if self.address_extension is None else 5
            first_payload = payload[:first_len]
            if self.address_extension is not None:
                ff_data = (
                    bytes([self.address_extension, pci_high, pci_low])
                    + first_payload
                    + bytes(8 - 3 - len(first_payload))
                )
            else:
                ff_data = (
                    bytes([pci_high, pci_low])
                    + first_payload
                    + bytes(8 - 2 - len(first_payload))
                )
            ff = can.Message(
                arbitration_id=self.req_id,
                is_extended_id=self.is_extended_id,
                data=ff_data,
            )
            self.bus.send(ff, timeout=send_timeout)
            self.logger.debug("Sent First Frame: total_len=%d", total_len)

            # wait for flow control
            self.logger.debug("Waiting for Flow Control frame")
            elapsed = 0.0
            wait_count = 0
            while True:
                remaining = fc_timeout - elapsed
                if remaining <= 0:
                    self.logger.debug("No Flow Control frame received")
                    raise ISOTransportError("No Flow Control frame received")
                wait_start = time.monotonic()
                fc = self.bus.recv(remaining)
                elapsed += time.monotonic() - wait_start
                if not fc or fc.arbitration_id != self.resp_id:
                    continue
                data_fc = bytes(fc.data)
                if self.address_extension is not None:
                    if data_fc[0] != self.address_extension:
                        continue
                    data_fc = data_fc[1:]
                if data_fc[0] >> 4 != 0x3:
                    continue
                fs = data_fc[0] & 0x0F
                if fs == 0x2:
                    self.logger.debug("Flow control overflow")
                    raise ISOTransportError("Flow control overflow")
                if fs == 0x0:
                    block_size = data_fc[1]
                    st_delay = _calc_st_delay(data_fc[2])
                    self.logger.debug(
                        "Received Flow Control: fs=%d bs=%d st=%.3f",
                        fs,
                        block_size,
                        st_delay,
                    )
                    wait_count = 0
                    break
                wait_count += 1
                self.logger.debug("Flow Control WAIT received (%d)", wait_count)
                if wait_count > self.wft_max:
                    self.logger.debug("Flow control WAIT limit exceeded")
                    raise ISOTransportError("Too many Flow Control WAIT frames")
            seq = 1
            offset = first_len
            sent_in_block = 0
            chunk_len = 7 if self.address_extension is None else 6
            while offset < len(payload):
                if block_size != 0 and sent_in_block >= block_size:
                    self.logger.debug(
                        "Block size %d reached, waiting for Flow Control",
                        block_size,
                    )
                    # need next flow control
                    while True:
                        remaining = fc_timeout - elapsed
                        if remaining <= 0:
                            self.logger.debug("Flow control timeout")
                            raise ISOTransportError("Flow control timeout")
                        wait_start = time.monotonic()
                        fc = self.bus.recv(remaining)
                        elapsed += time.monotonic() - wait_start
                        if not fc or fc.arbitration_id != self.resp_id:
                            continue
                        data_fc = bytes(fc.data)
                        if self.address_extension is not None:
                            if data_fc[0] != self.address_extension:
                                continue
                            data_fc = data_fc[1:]
                        if data_fc[0] >> 4 != 0x3:
                            continue
                        fs = data_fc[0] & 0x0F
                        if fs == 0x2:
                            self.logger.debug("Flow control overflow")
                            raise ISOTransportError("Flow control overflow")
                        if fs == 0x0:
                            block_size = data_fc[1]
                            st_delay = _calc_st_delay(data_fc[2])
                            self.logger.debug(
                                "Received Flow Control: fs=%d bs=%d st=%.3f",
                                fs,
                                block_size,
                                st_delay,
                            )
                            wait_count = 0
                            sent_in_block = 0
                            break
                        wait_count += 1
                        self.logger.debug("Flow Control WAIT received (%d)", wait_count)
                        if wait_count > self.wft_max:
                            self.logger.debug("Flow control WAIT limit exceeded")
                            raise ISOTransportError("Too many Flow Control WAIT frames")
                chunk = payload[offset : offset + chunk_len]  # noqa: E203
                if self.address_extension is not None:
                    cf_data = (
                        bytes([self.address_extension, 0x20 | (seq & 0x0F)])
                        + chunk
                        + bytes(chunk_len - len(chunk))
                    )
                else:
                    cf_data = (
                        bytes([0x20 | (seq & 0x0F)])
                        + chunk
                        + bytes(chunk_len - len(chunk))
                    )
                cf = can.Message(
                    arbitration_id=self.req_id,
                    is_extended_id=self.is_extended_id,
                    data=cf_data,
                )
                self.bus.send(cf, timeout=send_timeout)
                self.logger.debug("Sent Consecutive Frame seq=%d", seq)
                offset += len(chunk)
                seq = (seq + 1) & 0x0F
                sent_in_block += 1
                if offset < len(payload):
                    time.sleep(st_delay)
                    # loop continues
            if self.t_data and self.t_data.con:
                self.t_data.con(True, None)
            return True
        except Exception as exc:
            self.logger.debug("Send failed: %s", exc)
            if self.t_data and self.t_data.con:
                self.t_data.con(False, exc)
            raise

    # ------------------------------------------------------------------
    # receiving
    def _send_fc(self, status: int = 0) -> None:
        pci = 0x30 | (status & 0x0F)
        if self.address_extension is not None:
            data = bytes(
                [
                    self.address_extension,
                    pci,
                    self.rx_block_size & 0xFF,
                    self.rx_st_min & 0xFF,
                    0,
                    0,
                    0,
                    0,
                ]
            )
        else:
            data = bytes(
                [pci, self.rx_block_size & 0xFF, self.rx_st_min & 0xFF, 0, 0, 0, 0, 0]
            )
        fc = can.Message(
            arbitration_id=self.req_id,
            is_extended_id=self.is_extended_id,
            data=data,
        )
        self.bus.send(fc)
        self.logger.debug(
            "Sent Flow Control: status=%d bs=%d st_min=%d",
            status,
            self.rx_block_size,
            self.rx_st_min,
        )

    def pause_rx(self) -> None:
        """Request the sender to pause transmission via Flow Control WAIT."""
        self._rx_fc_status = 1

    def resume_rx(self) -> None:
        """Resume a paused transfer by sending a Flow Control CTS frame."""
        self._rx_fc_status = 0
        self._send_fc(status=0)

    def _receive(self, timeout: float = 1.0) -> bytes:
        state: dict[str, any] = {
            "expected": 0,
            "payload": bytearray(),
            "next_seq": 0,
            "bs": 0,
        }
        wait_count = 0
        start = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                self.logger.debug("UDS response timeout")
                raise ISOTransportError("UDS response timeout")
            msg = self.bus.recv(remaining)
            if not msg or msg.arbitration_id != self.resp_id:
                continue
            data = bytes(msg.data)
            if self.address_extension is not None:
                if data[0] != self.address_extension:
                    continue
                data = data[1:]
            frame_type = data[0] >> 4
            if state["expected"] > 0 and frame_type in (0x0, 0x1):
                state["expected"] = 0
                state["payload"] = bytearray()
                if self.on_reset:
                    self.on_reset()
                if self.error_on_reset:
                    self.logger.debug("Unexpected start-of-frame during reception")
                    raise ISOTransportError(
                        "Unexpected start-of-frame during reception"
                    )
                self.logger.warning(
                    "ISO-TP reception reset due to unexpected start-of-frame"
                )
            if frame_type == 0x0:  # single
                length = data[0] & 0x0F
                payload = data[1 : 1 + length]  # noqa: E203
                self.logger.debug("Received Single Frame: len=%d", length)
                if self.t_data and self.t_data.ind:
                    self.t_data.ind(payload)
                return payload
            if frame_type == 0x1:  # first frame
                total_len = ((data[0] & 0x0F) << 8) | data[1]
                if self.max_rx_size is not None and total_len > self.max_rx_size:
                    self._send_fc(status=2)
                    self.logger.debug(
                        "Response length %d exceeds max_rx_size %d",
                        total_len,
                        self.max_rx_size,
                    )
                    raise ISOTransportError("Response length exceeds max_rx_size")
                state["payload"] = bytearray(data[2:])
                state["expected"] = total_len - len(state["payload"])
                state["next_seq"] = 1
                state["bs"] = 0
                if self.t_data and self.t_data.som_ind:
                    self.t_data.som_ind()
                if self._rx_fc_status == 1:
                    wait_count += 1
                    if wait_count > self.wft_max:
                        self.logger.debug("Flow control WAIT limit exceeded")
                        raise ISOTransportError("Too many Flow Control WAIT frames")
                else:
                    wait_count = 0
                self._send_fc(status=self._rx_fc_status)
                self.logger.debug("Received First Frame: total_len=%d", total_len)
                continue
            if frame_type == 0x2 and state["expected"] > 0:
                seq = data[0] & 0x0F
                if seq != state["next_seq"]:
                    state["expected"] = 0
                    state["payload"] = bytearray()
                    self.logger.debug(
                        "Sequence number mismatch: expected %d got %d",
                        state["next_seq"],
                        seq,
                    )
                    raise ISOTransportError("Sequence number mismatch")
                take = min(
                    state["expected"], 7 if self.address_extension is None else 6
                )
                state["payload"].extend(data[1 : 1 + take])  # noqa: E203
                state["expected"] -= take
                state["next_seq"] = (state["next_seq"] + 1) & 0x0F
                state["bs"] += 1
                self.logger.debug("Received Consecutive Frame seq=%d", seq)
                if state["expected"] <= 0:
                    payload = bytes(state["payload"])
                    state["payload"] = bytearray()
                    state["expected"] = 0
                    if self.t_data and self.t_data.ind:
                        self.t_data.ind(payload)
                    return payload
                if self.rx_block_size > 0 and state["bs"] >= self.rx_block_size:
                    if self._rx_fc_status == 1:
                        wait_count += 1
                        if wait_count > self.wft_max:
                            self.logger.debug("Flow control WAIT limit exceeded")
                            raise ISOTransportError("Too many Flow Control WAIT frames")
                    else:
                        wait_count = 0
                    self._send_fc(status=self._rx_fc_status)
                    state["bs"] = 0
                continue
            if state["expected"] > 0:
                state["expected"] = 0
                state["payload"] = bytearray()
                if self.on_reset:
                    self.on_reset()
                if self.error_on_reset:
                    self.logger.debug("Unexpected start-of-frame during reception")
                    raise ISOTransportError(
                        "Unexpected start-of-frame during reception"
                    )
                self.logger.warning(
                    "ISO-TP reception reset due to unexpected start-of-frame"
                )
                continue

    # ------------------------------------------------------------------
    def _request(
        self, service: int, data: bytes, timeout: float | tuple[float, float] = 1.0
    ) -> bytes:
        if self.t_data and self.t_data.req:
            self.t_data.req(service, data)
        if isinstance(timeout, tuple):
            send_to, recv_to = timeout
        else:
            send_to = recv_to = timeout
        self._send(service, data, send_to)
        return self._receive(recv_to)

    def send(
        self, service: int, data: bytes, timeout: float | tuple[float, float] = 1.0
    ) -> bool:
        """Send a UDS request segmented over ISO-TP.

        Raises
        ------
        RuntimeError
            If another operation is already in progress.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("UDSClient operation already in progress")
        try:
            return self._send(service, data, timeout)
        finally:
            self._lock.release()

    def receive(self, timeout: float = 1.0) -> bytes:
        """Wait for a UDS response segmented over ISO-TP.

        Raises
        ------
        RuntimeError
            If another operation is already in progress.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("UDSClient operation already in progress")
        try:
            return self._receive(timeout)
        finally:
            self._lock.release()

    def request(
        self, service: int, data: bytes, timeout: float | tuple[float, float] = 1.0
    ) -> bytes:
        """Send a request and wait for the response atomically.

        Raises
        ------
        RuntimeError
            If another operation is already in progress.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("UDSClient operation already in progress")
        try:
            return self._request(service, data, timeout)
        finally:
            self._lock.release()

    # high-level services ------------------------------------------------
    def change_session(self, session: int, timeout: float = 1.0) -> bool:
        rsp = self.request(0x10, bytes([session]), timeout)
        return rsp[:2] == bytes([0x50, session])

    def security_access(
        self, level: int, key: "bytes | None" = None, timeout: float = 1.0
    ) -> bool:
        """Request security access at ``level``.

        If ``key`` is ``None`` the key is derived from the ECU-provided seed
        using ``key_algo`` specified at construction or via configuration.
        ``key_algo`` may be a callable or a ``module:attr`` string which is
        imported dynamically.

        Raises
        ------
        ISOTransportError
            If a negative response is received or the response format is
            unexpected.
        """

        rsp = self.request(0x27, bytes([level * 2 - 1]), timeout)
        if len(rsp) < 2:
            raise ISOTransportError("Invalid seed response")
        if rsp[0] == 0x7F:
            code = rsp[2] if len(rsp) > 2 else 0
            raise ISOTransportError(f"Security seed request denied (NRC 0x{code:02X})")
        if rsp[0] != 0x67 or rsp[1] != level * 2 - 1:
            raise ISOTransportError("Unexpected seed response")
        seed = rsp[2:]
        if key is None:
            key = self._key_algo(seed)
        rsp2 = self.request(0x27, bytes([level * 2]) + key, timeout)
        if len(rsp2) < 2:
            raise ISOTransportError("Invalid key response")
        if rsp2[0] == 0x7F:
            code = rsp2[2] if len(rsp2) > 2 else 0
            raise ISOTransportError(f"Security access denied (NRC 0x{code:02X})")
        if rsp2[:2] != bytes([0x67, level * 2]):
            raise ISOTransportError("Unexpected key response")
        return True

    def read_dtc_by_status_mask(self, mask: int = 0xFF, timeout: float = 1.0) -> bytes:
        return self.request(0x19, bytes([0x02, mask]), timeout)
