"""UDS client with ISO-TP transport support."""
from __future__ import annotations

import time

try:
    import can
except ImportError:  # pragma: no cover - optional dependency
    can = None  # type: ignore

from isotp_primitives import TDataPrimitive


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
        key_algo: callable, optional
        Function applied to the received seed to generate the security
        access key.  When not provided a simple bitwise inversion is used.
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
        key_algo: "Callable[[bytes], bytes] | None" = None,
        source_address: "int | None" = None,
        target_address: "int | None" = None,
        address_extension: "int | None" = None,
        t_data: "TDataPrimitive | None" = None,
    ) -> None:
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.is_extended_id = is_extended_id
        self.rx_block_size = rx_block_size
        self.rx_st_min = rx_st_min
        self._key_algo = key_algo
        self.source_address = source_address
        self.target_address = target_address
        self.address_extension = address_extension
        self.t_data = t_data

        if self.source_address is not None and self.target_address is not None:
            base = 0x18DA
            self.req_id = (base << 16) | (self.target_address << 8) | self.source_address
            self.resp_id = (base << 16) | (self.source_address << 8) | self.target_address
            self.is_extended_id = True

    # ------------------------------------------------------------------
    # sending
    def send(self, service: int, data: bytes, timeout: float = 1.0) -> bool:
        payload = bytes([service]) + data
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
                    frame_data = bytes([pci]) + payload + bytes(
                        single_limit - len(payload)
                    )
                frame = can.Message(
                    arbitration_id=self.req_id,
                    is_extended_id=self.is_extended_id,
                    data=frame_data,
                )
                self.bus.send(frame, timeout=timeout)
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
            self.bus.send(ff, timeout=timeout)

            # wait for flow control
            start = time.monotonic()
            while True:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise ISOTransportError("No Flow Control frame received")
                fc = self.bus.recv(remaining)
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
                    raise ISOTransportError("Flow control overflow")
                if fs == 0x0:
                    block_size = data_fc[1]
                    st_delay = _calc_st_delay(data_fc[2])
                    break
                # fs == 0x1 -> wait
            seq = 1
            offset = first_len
            sent_in_block = 0
            chunk_len = 7 if self.address_extension is None else 6
            while offset < len(payload):
                if block_size != 0 and sent_in_block >= block_size:
                    # need next flow control
                    start = time.monotonic()
                    while True:
                        remaining = timeout - (time.monotonic() - start)
                        if remaining <= 0:
                            raise ISOTransportError("Flow control timeout")
                        fc = self.bus.recv(remaining)
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
                            raise ISOTransportError("Flow control overflow")
                        if fs == 0x0:
                            block_size = data_fc[1]
                            st_delay = _calc_st_delay(data_fc[2])
                            sent_in_block = 0
                            break
                chunk = payload[offset : offset + chunk_len]
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
                self.bus.send(cf, timeout=timeout)
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
            if self.t_data and self.t_data.con:
                self.t_data.con(False, exc)
            raise

    # ------------------------------------------------------------------
    # receiving
    def _send_fc(self) -> None:
        if self.address_extension is not None:
            data = bytes(
                [
                    self.address_extension,
                    0x30,
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
                [0x30, self.rx_block_size & 0xFF, self.rx_st_min & 0xFF, 0, 0, 0, 0, 0]
            )
        fc = can.Message(
            arbitration_id=self.req_id,
            is_extended_id=self.is_extended_id,
            data=data,
        )
        self.bus.send(fc)

    def receive(self, timeout: float = 1.0) -> bytes:
        state: dict[str, any] = {"expected": 0, "payload": bytearray(), "next_seq": 0, "bs": 0}
        start = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
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
            if frame_type == 0x0:  # single
                length = data[0] & 0x0F
                payload = data[1 : 1 + length]
                if self.t_data and self.t_data.ind:
                    self.t_data.ind(payload)
                return payload
            if frame_type == 0x1:  # first frame
                total_len = ((data[0] & 0x0F) << 8) | data[1]
                state["payload"] = bytearray(data[2:])
                state["expected"] = total_len - len(state["payload"])
                state["next_seq"] = 1
                state["bs"] = 0
                if self.t_data and self.t_data.som_ind:
                    self.t_data.som_ind()
                self._send_fc()
                continue
            if frame_type == 0x2 and state["expected"] > 0:
                seq = data[0] & 0x0F
                if seq != state["next_seq"]:
                    state["expected"] = 0
                    state["payload"] = bytearray()
                    raise ISOTransportError("Sequence number mismatch")
                take = min(
                    state["expected"], 7 if self.address_extension is None else 6
                )
                state["payload"].extend(data[1 : 1 + take])
                state["expected"] -= take
                state["next_seq"] = (state["next_seq"] + 1) & 0x0F
                state["bs"] += 1
                if state["expected"] <= 0:
                    payload = bytes(state["payload"])
                    state["payload"] = bytearray()
                    state["expected"] = 0
                    if self.t_data and self.t_data.ind:
                        self.t_data.ind(payload)
                    return payload
                if self.rx_block_size > 0 and state["bs"] >= self.rx_block_size:
                    self._send_fc()
                    state["bs"] = 0
                continue
            # new SF/FF while in progress -> reset
            state["expected"] = 0
            state["payload"] = bytearray()

    # ------------------------------------------------------------------
    def request(self, service: int, data: bytes, timeout: float = 1.0) -> bytes:
        if self.t_data and self.t_data.req:
            self.t_data.req(service, data)
        self.send(service, data, timeout)
        return self.receive(timeout)

    # high-level services ------------------------------------------------
    def change_session(self, session: int, timeout: float = 1.0) -> bool:
        rsp = self.request(0x10, bytes([session]), timeout)
        return rsp[:2] == bytes([0x50, session])

    def _default_key_algo(self, seed: bytes) -> bytes:
        """Generate a key from a seed using a basic bitwise inversion.

        This simple algorithm flips all bits of the seed.  Real-world ECUs
        use proprietary algorithms; this serves as a deterministic example
        for testing and demonstration purposes.
        """

        return bytes((b ^ 0xFF) & 0xFF for b in seed)

    def security_access(
        self, level: int, key: "bytes | None" = None, timeout: float = 1.0
    ) -> bool:
        """Request security access at ``level``.

        If ``key`` is ``None`` the key is derived from the ECU-provided seed
        using ``key_algo`` passed at construction time or a default
        inversion-based algorithm.
        """

        rsp = self.request(0x27, bytes([level * 2 - 1]), timeout)
        if not rsp or rsp[0] != 0x67:
            return False
        seed = rsp[2:]
        if key is None:
            algo = self._key_algo or self._default_key_algo
            key = algo(seed)
        rsp2 = self.request(0x27, bytes([level * 2]) + key, timeout)
        return rsp2[:2] == bytes([0x67, level * 2])

    def read_dtc_by_status_mask(self, mask: int = 0xFF, timeout: float = 1.0) -> bytes:
        return self.request(0x19, bytes([0x02, mask]), timeout)
