"""Transport modules for sending serialized frames."""

from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import Any, Dict, Optional


class Transport(ABC):
    """Abstract transport with retry logic."""

    def __init__(self, retries: int = 3, delay: float = 0.5) -> None:
        self.retries = retries
        self.delay = delay

    def send(self, payload: str) -> None:
        attempts = 0
        while True:
            try:
                self._send_once(payload)
                return
            except Exception:
                attempts += 1
                if attempts >= self.retries:
                    raise
                time.sleep(self.delay)

    @abstractmethod
    def _send_once(self, payload: str) -> None:
        """Send the payload a single time."""


class HTTPTransport(Transport):
    """HTTP POST transport."""

    def __init__(
        self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.headers = headers or {}

    def _send_once(
        self, payload: str
    ) -> None:  # pragma: no cover - network errors not deterministic
        from urllib import request

        req = request.Request(
            self.url, data=payload.encode(), headers=self.headers, method="POST"
        )
        with request.urlopen(req, timeout=5) as resp:
            resp.read()


class MQTTTransport(Transport):
    """MQTT transport using ``paho-mqtt``."""

    def __init__(self, topic: str, hostname: str = "localhost", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.topic = topic
        self.hostname = hostname

    def _send_once(
        self, payload: str
    ) -> None:  # pragma: no cover - depends on external lib
        try:
            from paho.mqtt import publish
        except Exception as exc:  # ImportError
            raise RuntimeError("paho-mqtt is required for MQTTTransport") from exc
        publish.single(self.topic, payload=payload, hostname=self.hostname)
