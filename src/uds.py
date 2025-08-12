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

_NRC_DESC = {
    0x10: "General reject",
    0x11: "Service not supported",
    0x12: "Sub-function not supported",
    0x13: "Incorrect message length or invalid format",
    0x14: "Response too long",
    0x21: "Busy repeat request",
    0x22: "Conditions not correct (e.g. wrong baud rate)",
    0x24: "Request sequence error",
    0x31: "Request out of range",
    0x33: "Security access denied (possible wrong security patch)",
    0x35: "Invalid key (security challenge failed)",
    0x36: "Exceeded number of attempts",
    0x37: "Required time delay not expired",
    0x78: "Response pending",
}


def _nrc_desc(code: int) -> str:
    """Return human readable description for a negative response code."""

    return _NRC_DESC.get(code, "Unknown error")


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
        before aborting.  A value of ``0xFF`` (the default) allows unlimited
        waits as long as the overall timeout is not exceeded.
    pad_byte: int, optional
        Padding byte used to fill unused bytes in CAN frames. Defaults to
        ``0x00``.
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
        wft_max: int = 0xFF,
        pad_byte: int = 0x00,
        key_algo: "Callable[[bytes], bytes] | str | None" = None,
        source_address: "int | None" = None,
        target_address: "int | None" = None,
        address_extension: "int | None" = None,
        t_data: "TDataPrimitive | None" = None,
        max_rx_size: "int | None" = None,
        on_reset: "Callable[[], None] | None" = None,
        error_on_reset: bool = False,
        logger: logging.Logger = LOGGER,
        bus_lock: threading.Lock | None = None,
    ) -> None:
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.is_extended_id = is_extended_id
        self.rx_block_size = rx_block_size
        self.rx_st_min = rx_st_min
        self.wft_max = wft_max
        self.pad_byte = pad_byte & 0xFF
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
        self.bus_lock = bus_lock

        if self.source_address is not None and self.target_address is not None:
            base = 0x18DA
            self.req_id = (
                (base << 16) | (self.target_address << 8) | self.source_address
            )
            self.resp_id = (
                (base << 16) | (self.source_address << 8) | self.target_address
            )
            self.is_extended_id = True

    def _bus_send(self, msg: "can.Message", timeout: float | None = None) -> None:
        if self.bus_lock:
            with self.bus_lock:
                self.bus.send(msg, timeout=timeout)
        else:
            self.bus.send(msg, timeout=timeout)

    def _bus_recv(self, timeout: float | None = None) -> "can.Message | None":
        if self.bus_lock:
            with self.bus_lock:
                return self.bus.recv(timeout)
        return self.bus.recv(timeout)

    # ------------------------------------------------------------------
    # sending
    def _send(
        self, service: int, data: bytes, timeout: float | tuple[float, float] = 1.0
    ) -> bool:
        payload = bytes([service]) + data
        if len(payload) > 0xFFF:
            self.logger.error("Payload too large: %d bytes", len(payload))
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
                        + bytes([self.pad_byte] * (single_limit - len(payload)))
                    )
                else:
                    frame_data = (
                        bytes([pci])
                        + payload
                        + bytes([self.pad_byte] * (single_limit - len(payload)))
                    )
                frame = can.Message(
                    arbitration_id=self.req_id,
                    is_extended_id=self.is_extended_id,
                    data=frame_data,
                )
                self._bus_send(frame, timeout=send_timeout)
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
                    + bytes([self.pad_byte] * (8 - 3 - len(first_payload)))
                )
            else:
                ff_data = (
                    bytes([pci_high, pci_low])
                    + first_payload
                    + bytes([self.pad_byte] * (8 - 2 - len(first_payload)))
                )
            ff = can.Message(
                arbitration_id=self.req_id,
                is_extended_id=self.is_extended_id,
                data=ff_data,
            )
            self._bus_send(ff, timeout=send_timeout)
            self.logger.debug("Sent First Frame: total_len=%d", total_len)

            # wait for flow control
            self.logger.debug("Waiting for Flow Control frame")
            wait_count = 0
            deadline = time.monotonic() + fc_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.logger.error("No Flow Control frame received")
                    raise ISOTransportError("No Flow Control frame received")
                fc = self._bus_recv(remaining)
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
                    self.logger.error("Flow control overflow")
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
                    self.logger.error("Flow control WAIT limit exceeded")
                    raise ISOTransportError("Too many Flow Control WAIT frames")
                deadline = time.monotonic() + fc_timeout
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
                    deadline = time.monotonic() + fc_timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.logger.error("Flow control timeout")
                            raise ISOTransportError("Flow control timeout")
                        fc = self._bus_recv(remaining)
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
                            self.logger.error("Flow control overflow")
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
                            self.logger.error("Flow control WAIT limit exceeded")
                            raise ISOTransportError("Too many Flow Control WAIT frames")
                        deadline = time.monotonic() + fc_timeout
                chunk = payload[offset : offset + chunk_len]  # noqa: E203
                if self.address_extension is not None:
                    cf_data = (
                        bytes([self.address_extension, 0x20 | (seq & 0x0F)])
                        + chunk
                        + bytes([self.pad_byte] * (chunk_len - len(chunk)))
                    )
                else:
                    cf_data = (
                        bytes([0x20 | (seq & 0x0F)])
                        + chunk
                        + bytes([self.pad_byte] * (chunk_len - len(chunk)))
                    )
                cf = can.Message(
                    arbitration_id=self.req_id,
                    is_extended_id=self.is_extended_id,
                    data=cf_data,
                )
                self._bus_send(cf, timeout=send_timeout)
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
            self.logger.error("Send failed: %s", exc)
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
                    self.pad_byte,
                    self.pad_byte,
                    self.pad_byte,
                    self.pad_byte,
                ]
            )
        else:
            data = bytes(
                [
                    pci,
                    self.rx_block_size & 0xFF,
                    self.rx_st_min & 0xFF,
                    self.pad_byte,
                    self.pad_byte,
                    self.pad_byte,
                    self.pad_byte,
                    self.pad_byte,
                ]
            )
        fc = can.Message(
            arbitration_id=self.req_id,
            is_extended_id=self.is_extended_id,
            data=data,
        )
        self._bus_send(fc)
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
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.logger.error("UDS response timeout")
                raise ISOTransportError("UDS response timeout")
            msg = self._bus_recv(remaining)
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
                    self.logger.error("Unexpected start-of-frame during reception")
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
                    self.logger.error(
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
                        self.logger.error("Flow control WAIT limit exceeded")
                        raise ISOTransportError("Too many Flow Control WAIT frames")
                else:
                    wait_count = 0
                self._send_fc(status=self._rx_fc_status)
                self.logger.debug("Received First Frame: total_len=%d", total_len)
                deadline = time.monotonic() + timeout
                continue
            if frame_type == 0x2 and state["expected"] > 0:
                seq = data[0] & 0x0F
                if seq != state["next_seq"]:
                    state["expected"] = 0
                    state["payload"] = bytearray()
                    self.logger.error(
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
                            self.logger.error("Flow control WAIT limit exceeded")
                            raise ISOTransportError("Too many Flow Control WAIT frames")
                    else:
                        wait_count = 0
                    self._send_fc(status=self._rx_fc_status)
                    state["bs"] = 0
                deadline = time.monotonic() + timeout
                continue
            if state["expected"] > 0:
                state["expected"] = 0
                state["payload"] = bytearray()
                if self.on_reset:
                    self.on_reset()
                if self.error_on_reset:
                    self.logger.error("Unexpected start-of-frame during reception")
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
        self.logger.debug("Sending service 0x%02X with payload %s", service, data.hex())
        self._send(service, data, send_to)
        start = time.monotonic()
        while True:
            remaining = recv_to - (time.monotonic() - start)
            rsp = self._receive(remaining)
            self.logger.debug("Response for service 0x%02X: %s", service, rsp.hex())
            if len(rsp) >= 3 and rsp[0] == 0x7F and rsp[2] == 0x78:
                # NRC 0x78: response pending, wait for final response
                self.logger.info("Service 0x%02X pending (NRC 0x78)", service)
                continue
            if len(rsp) >= 3 and rsp[0] == 0x7F:
                code = rsp[2]
                self.logger.error(
                    "Service 0x%02X failed: NRC 0x%02X - %s",
                    rsp[1],
                    code,
                    _nrc_desc(code),
                )
            else:
                self.logger.info("Service 0x%02X succeeded", service)
            return rsp

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
            self.logger.debug(
                "Send API called for service 0x%02X with payload %s",
                service,
                data.hex(),
            )
            ok = self._send(service, data, timeout)
            if ok:
                self.logger.info("Service 0x%02X send complete", service)
            return ok
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
            self.logger.debug("Waiting for response with timeout %.3f", timeout)
            payload = self._receive(timeout)
            self.logger.info("Received payload: %s", payload.hex())
            return payload
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
        self.logger.info("Requesting session 0x%02X", session)
        rsp = self.request(0x10, bytes([session]), timeout)
        if rsp[:2] == bytes([0x50, session]):
            self.logger.info("Session 0x%02X accepted", session)
            return True
        if len(rsp) >= 3 and rsp[0] == 0x7F:
            code = rsp[2]
            self.logger.error(
                "Session 0x%02X rejected: NRC 0x%02X - %s",
                session,
                code,
                _nrc_desc(code),
            )
        else:
            self.logger.error("Unexpected response to session change: %s", rsp.hex())
        return False

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

        self.logger.info("Requesting security access level %d", level)
        rsp = self.request(0x27, bytes([level * 2 - 1]), timeout)
        if len(rsp) < 2:
            self.logger.error("Invalid seed response")
            raise ISOTransportError("Invalid seed response")
        if rsp[0] == 0x7F:
            code = rsp[2] if len(rsp) > 2 else 0
            self.logger.error(
                "Security seed request denied: NRC 0x%02X - %s",
                code,
                _nrc_desc(code),
            )
            raise ISOTransportError(f"Security seed request denied (NRC 0x{code:02X})")
        if rsp[0] != 0x67 or rsp[1] != level * 2 - 1:
            self.logger.error("Unexpected seed response: %s", rsp.hex())
            raise ISOTransportError("Unexpected seed response")
        seed = rsp[2:]
        if key is None:
            key = self._key_algo(seed)
        self.logger.info("Submitting key for security level %d", level)
        rsp2 = self.request(0x27, bytes([level * 2]) + key, timeout)
        if len(rsp2) < 2:
            self.logger.error("Invalid key response")
            raise ISOTransportError("Invalid key response")
        if rsp2[0] == 0x7F:
            code = rsp2[2] if len(rsp2) > 2 else 0
            self.logger.error(
                "Security access denied: NRC 0x%02X - %s",
                code,
                _nrc_desc(code),
            )
            raise ISOTransportError(f"Security access denied (NRC 0x{code:02X})")
        if rsp2[:2] != bytes([0x67, level * 2]):
            self.logger.error("Unexpected key response: %s", rsp2.hex())
            raise ISOTransportError("Unexpected key response")
        self.logger.info("Security access level %d granted", level)
        return True

    def read_dtc_by_status_mask(self, mask: int = 0xFF, timeout: float = 1.0) -> bytes:
        self.logger.info("Reading DTCs with status mask 0x%02X", mask)
        rsp = self.request(0x19, bytes([0x02, mask]), timeout)
        self.logger.info("Read DTCs response length %d", len(rsp))
        return rsp
