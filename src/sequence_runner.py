import logging
import threading
import time
from typing import Any, Iterable

try:
    import can
except ImportError:  # pragma: no cover - optional dependency
    can = None  # type: ignore


class SequenceRunner:
    """Send a configured sequence of CAN frames in a loop.

    Parameters
    ----------
    bus: can.BusABC
        CAN bus instance used for transmission and reception.
    sequence: Iterable[dict[str, Any]]
        Iterable of steps with keys ``can_id`` (int), ``payload`` (hex string)
        and optional ``response_id`` (int).  ``is_extended_id`` defaults to
        ``False`` when not specified.
    interval_ms: int, optional
        Delay between sequence repetitions in milliseconds.  Defaults to
        ``500``.
    flow_control: dict[str, int] | None, optional
        ``{"block_size": int, "st_min_ms": int}`` settings advertised in
        automatically transmitted Flow Control frames when multi-frame
        responses are observed.
    logger: logging.Logger, optional
        Logger used for debug output.  Defaults to a module-level logger.
    """

    def __init__(
        self,
        bus: "can.BusABC",
        sequence: Iterable[dict[str, Any]],
        *,
        interval_ms: int = 500,
        flow_control: dict[str, int] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.bus = bus
        self.sequence = list(sequence)
        self.interval = interval_ms / 1000.0
        self.logger = logger or logging.getLogger(__name__)
        self.block_size = (flow_control or {}).get("block_size", 0)
        self.st_min = (flow_control or {}).get("st_min_ms", 0)
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background thread."""

        threading.Thread(target=self._run_loop, daemon=True).start()

    def stop(self) -> None:
        """Signal the runner to stop after the current iteration."""

        self._stop.set()

    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self.interval):
                break

    def run_once(self) -> None:
        """Execute the sequence a single time."""

        for step in self.sequence:
            data = bytes.fromhex(step["payload"])
            msg = can.Message(
                arbitration_id=step["can_id"],
                data=data,
                is_extended_id=step.get("is_extended_id", False),
            )
            self.bus.send(msg)
            resp_id = step.get("response_id")
            if resp_id is not None:
                self._await_response(resp_id, step["can_id"])

    # ------------------------------------------------------------------
    def _await_response(self, resp_id: int, req_id: int) -> None:
        """Wait for a response and handle multi-frame transfers."""

        expected = 0
        bs_count = 0
        timeout = time.monotonic() + 0.5
        while True:
            remaining = timeout - time.monotonic()
            if remaining <= 0:
                return
            rsp = self.bus.recv(remaining)
            if not rsp or rsp.arbitration_id != resp_id:
                continue
            data = bytes(rsp.data)
            if not data:
                continue
            pci = data[0]
            ftype = pci >> 4
            if ftype == 0x0:  # single frame
                return
            if ftype == 0x1:  # first frame
                length = ((pci & 0xF) << 8) | data[1]
                expected = length - (len(data) - 2)
                bs_count = 0
                self._send_fc(req_id)
                if expected <= 0:
                    return
                continue
            if ftype == 0x2 and expected > 0:  # consecutive frame
                take = min(expected, 7)
                expected -= take
                bs_count += 1
                if expected <= 0:
                    return
                if self.block_size > 0 and bs_count >= self.block_size:
                    self._send_fc(req_id)
                    bs_count = 0
                continue
            # ignore other frame types

    def _send_fc(self, req_id: int) -> None:
        """Transmit a Flow Control frame to the sender."""

        data = bytes(
            [0x30, self.block_size & 0xFF, self.st_min & 0xFF, 0, 0, 0, 0, 0]
        )
        fc = can.Message(arbitration_id=req_id, data=data, is_extended_id=False)
        self.bus.send(fc)
        self.logger.debug(
            "Sent Flow Control: bs=%d st=%d", self.block_size, self.st_min
        )
