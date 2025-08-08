"""UDS client with ISO-TP transport support."""
from __future__ import annotations

import time

try:
    import can
except ImportError:  # pragma: no cover - optional dependency
    can = None  # type: ignore


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
    """Minimal UDS client implementing ISO-TP segmentation."""

    def __init__(
        self,
        bus: "can.BusABC",
        req_id: int,
        resp_id: int,
        *,
        is_extended_id: bool = False,
        rx_block_size: int = 0,
        rx_st_min: int = 0,
    ) -> None:
        self.bus = bus
        self.req_id = req_id
        self.resp_id = resp_id
        self.is_extended_id = is_extended_id
        self.rx_block_size = rx_block_size
        self.rx_st_min = rx_st_min

    # ------------------------------------------------------------------
    # sending
    def send(self, service: int, data: bytes, timeout: float = 1.0) -> None:
        payload = bytes([service]) + data
        if len(payload) <= 7:
            pci = len(payload) & 0x0F
            frame = can.Message(
                arbitration_id=self.req_id,
                is_extended_id=self.is_extended_id,
                data=bytes([pci]) + payload + bytes(7 - len(payload)),
            )
            self.bus.send(frame, timeout=timeout)
            return

        total_len = len(payload)
        pci_high = 0x10 | ((total_len >> 8) & 0x0F)
        pci_low = total_len & 0xFF
        first_payload = payload[:6]
        ff = can.Message(
            arbitration_id=self.req_id,
            is_extended_id=self.is_extended_id,
            data=bytes([pci_high, pci_low])
            + first_payload
            + bytes(8 - 2 - len(first_payload)),
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
        offset = 6
        sent_in_block = 0
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
            chunk = payload[offset : offset + 7]
            cf = can.Message(
                arbitration_id=self.req_id,
                is_extended_id=self.is_extended_id,
                data=bytes([0x20 | (seq & 0x0F)])
                + chunk
                + bytes(7 - len(chunk)),
            )
            self.bus.send(cf, timeout=timeout)
            offset += len(chunk)
            seq = (seq + 1) & 0x0F
            sent_in_block += 1
            if offset < len(payload):
                time.sleep(st_delay)
                # loop continues

    # ------------------------------------------------------------------
    # receiving
    def _send_fc(self) -> None:
        fc = can.Message(
            arbitration_id=self.req_id,
            is_extended_id=self.is_extended_id,
            data=bytes([0x30, self.rx_block_size & 0xFF, self.rx_st_min & 0xFF, 0, 0, 0, 0, 0]),
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
            frame_type = data[0] >> 4
            if frame_type == 0x0:  # single
                length = data[0] & 0x0F
                return data[1 : 1 + length]
            if frame_type == 0x1:  # first frame
                total_len = ((data[0] & 0x0F) << 8) | data[1]
                state["payload"] = bytearray(data[2:])
                state["expected"] = total_len - len(state["payload"])
                state["next_seq"] = 1
                state["bs"] = 0
                self._send_fc()
                continue
            if frame_type == 0x2 and state["expected"] > 0:
                seq = data[0] & 0x0F
                if seq != state["next_seq"]:
                    state["expected"] = 0
                    state["payload"] = bytearray()
                    continue
                take = min(state["expected"], 7)
                state["payload"].extend(data[1 : 1 + take])
                state["expected"] -= take
                state["next_seq"] = (state["next_seq"] + 1) & 0x0F
                state["bs"] += 1
                if state["expected"] <= 0:
                    payload = bytes(state["payload"])
                    state["payload"] = bytearray()
                    state["expected"] = 0
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
        self.send(service, data, timeout)
        return self.receive(timeout)

    # high-level services ------------------------------------------------
    def change_session(self, session: int, timeout: float = 1.0) -> bool:
        rsp = self.request(0x10, bytes([session]), timeout)
        return rsp[:2] == bytes([0x50, session])

    def security_access(self, level: int, key: bytes, timeout: float = 1.0) -> bool:
        rsp = self.request(0x27, bytes([level * 2 - 1]), timeout)
        if not rsp or rsp[0] != 0x67:
            return False
        _seed = rsp[2:]
        rsp2 = self.request(0x27, bytes([level * 2]) + key, timeout)
        return rsp2[:2] == bytes([0x67, level * 2])

    def read_dtc_by_status_mask(self, mask: int = 0xFF, timeout: float = 1.0) -> bytes:
        return self.request(0x19, bytes([0x02, mask]), timeout)
