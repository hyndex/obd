import json
import os
import threading
import logging
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import can
import pytest

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from can_monitor import load_dbc, monitor  # noqa: E402
from serialization import serialize_frame  # noqa: E402
from transport import HTTPTransport, MQTTTransport  # noqa: E402
from metrics import reset_metrics  # noqa: E402


dbc_path = os.path.join(os.path.dirname(__file__), "..", "src", "OBD.dbc")


@pytest.fixture
def log_setup(tmp_path):
    log_file = tmp_path / "can.log"
    logger = logging.getLogger(f"test_{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file)
    logger.addHandler(handler)
    logger.propagate = False

    def info(msg, *args, **kwargs):
        formatted = msg.replace("%%", "%") % args
        logger._log(logging.INFO, formatted, (), **kwargs)

    logger.info = info  # type: ignore[assignment]

    try:
        yield logger, log_file
    finally:
        logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _reset_metrics_auto():
    reset_metrics()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        self.server.calls += 1
        self.server.payloads.append(body)
        if self.server.fail_first and self.server.calls == 1:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()


def _run_server(server: HTTPServer) -> None:
    with server:
        server.serve_forever()


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_http_transport_formats(fmt, log_setup):
    logger, _ = log_setup
    db = load_dbc(dbc_path)

    server = HTTPServer(("localhost", 0), _Handler)
    server.calls = 0  # type: ignore[attr-defined]
    server.payloads = []  # type: ignore[attr-defined]
    server.fail_first = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=_run_server, args=(server,), daemon=True)
    thread.start()
    url = f"http://localhost:{server.server_port}"
    headers = {"Content-Type": "application/json" if fmt == "json" else "text/csv"}
    transport = HTTPTransport(url, headers=headers, retries=1, delay=0)

    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    msg = can.Message(
        arbitration_id=db.messages[0].frame_id,
        is_extended_id=True,
        data=bytes([1, 2, 0, 0, 0, 0, 0, 0]),
    )
    bus.send(msg)

    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, db, logger, serializer=fmt, transport=transport)

    server.shutdown()
    thread.join(1)

    assert server.calls == 1  # type: ignore[attr-defined]
    payload = server.payloads[0]  # type: ignore[index]
    if fmt == "json":
        body = json.loads(payload)
        assert body["id"] == msg.arbitration_id
        assert body["raw"] == msg.data.hex()
    else:
        expected = serialize_frame(
            msg.arbitration_id,
            msg.data,
            db.decode_message(msg.arbitration_id, msg.data),
            "csv",
        )
        assert payload.strip() == expected


def test_http_transport_retry(log_setup):
    logger, _ = log_setup
    db = load_dbc(dbc_path)

    server = HTTPServer(("localhost", 0), _Handler)
    server.calls = 0  # type: ignore[attr-defined]
    server.payloads = []  # type: ignore[attr-defined]
    server.fail_first = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=_run_server, args=(server,), daemon=True)
    thread.start()
    url = f"http://localhost:{server.server_port}"
    transport = HTTPTransport(
        url, headers={"Content-Type": "application/json"}, retries=2, delay=0
    )

    bus = can.interface.Bus(
        bustype="virtual", bitrate=500000, receive_own_messages=True
    )
    msg = can.Message(
        arbitration_id=db.messages[0].frame_id,
        is_extended_id=True,
        data=bytes([1, 2, 0, 0, 0, 0, 0, 0]),
    )
    bus.send(msg)

    orig_recv = bus.recv
    calls = 0

    def fake_recv(timeout=1.0):
        nonlocal calls
        if calls == 0:
            calls += 1
            return orig_recv(timeout)
        raise can.CanError("stop")

    with pytest.raises(can.CanError):
        with patch.object(bus, "recv", side_effect=fake_recv):
            monitor(bus, db, logger, serializer="json", transport=transport)

    server.shutdown()
    thread.join(1)
    assert server.calls == 2  # type: ignore[attr-defined]


def test_mqtt_transport_retry(monkeypatch):
    import paho.mqtt.publish as publish

    call_count = {"n": 0}

    def fake_single(topic, payload=None, hostname=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("fail")
        assert payload == "data"

    monkeypatch.setattr(publish, "single", fake_single)

    transport = MQTTTransport("topic", hostname="localhost", retries=2, delay=0)
    transport.send("data")
    assert call_count["n"] == 2
