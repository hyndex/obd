"""Simple in-process metrics collection and exposure."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional

_metrics: Dict[str, int] = {
    "bus_errors": 0,
    "restarts": 0,
    "decoding_failures": 0,
}

_output_file: Optional[str] = None
_server: Optional[HTTPServer] = None


def get_metrics() -> Dict[str, int]:
    """Return a snapshot of the current metrics."""
    return dict(_metrics)


def reset_metrics() -> None:
    """Reset all counters to zero."""
    for key in _metrics:
        _metrics[key] = 0
    _write()


def set_output_file(path: str | None) -> None:
    """Write metrics to ``path`` whenever they change."""
    global _output_file
    _output_file = path
    _write()


def record_bus_error() -> None:
    _metrics["bus_errors"] += 1
    _write()


def record_restart() -> None:
    _metrics["restarts"] += 1
    _write()


def record_decoding_failure() -> None:
    _metrics["decoding_failures"] += 1
    _write()


def _write() -> None:
    if _output_file:
        with open(_output_file, "w", encoding="utf-8") as f:
            json.dump(_metrics, f)


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pragma: no cover - trivial
        payload = json.dumps(_metrics).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Suppress default logging
    def log_message(
        self, format: str, *args: object
    ) -> None:  # pragma: no cover - noise
        return


def start_http_server(port: int = 8000) -> HTTPServer:
    """Start a thread serving metrics via HTTP."""
    global _server
    _server = HTTPServer(("", port), _MetricsHandler)
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    return _server


def stop_http_server() -> None:
    """Shut down the metrics HTTP server if running."""  # pragma: no cover - trivial
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None
