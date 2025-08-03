import json
import urllib.request
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from metrics import (  # noqa: E402
    record_bus_error,
    record_decoding_failure,
    record_restart,
    reset_metrics,
    set_output_file,
    start_http_server,
    stop_http_server,
)


def test_metrics_file_and_reset(tmp_path):
    reset_metrics()
    stats = tmp_path / "stats.json"
    set_output_file(str(stats))

    record_bus_error()
    record_restart()
    record_decoding_failure()

    data = json.loads(stats.read_text())
    assert data == {
        "bus_errors": 1,
        "restarts": 1,
        "decoding_failures": 1,
    }

    reset_metrics()
    data = json.loads(stats.read_text())
    assert data == {
        "bus_errors": 0,
        "restarts": 0,
        "decoding_failures": 0,
    }


def test_metrics_http_endpoint():
    reset_metrics()
    record_bus_error()
    server = start_http_server(0)
    try:
        port = server.server_port
        body = urllib.request.urlopen(f"http://localhost:{port}").read().decode()
        data = json.loads(body)
        assert data["bus_errors"] == 1
    finally:
        stop_http_server()
