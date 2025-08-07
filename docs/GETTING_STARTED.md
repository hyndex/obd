# Getting Started with the OBD Toolkit

This guide walks through setting up the hardware and software, running the provided
utilities, and diagnosing common problems.  It assumes you are using a Linux system
with [SocketCAN](https://www.kernel.org/doc/Documentation/networking/can.txt) support.

## 1. Prerequisites

### Hardware
- An MCP2515-based CAN interface with transceiver.
- OBD-II cable or breakout to access the vehicle's CAN high (pin 6) and CAN low (pin 14).
- Reliable 5 V power source for the MCP2515 module and a common ground between all devices.

### Software
- Python 3.10 or newer.
- `git` to clone the repository.
- Build tools for Python packages (e.g. `gcc`, `python3-dev` on Debian/Ubuntu).

## 2. Clone and Install

```bash
git clone https://example.com/obd.git
cd obd
python -m venv .venv && source .venv/bin/activate  # optional but recommended
pip install -r requirements.txt
```

If you see errors such as `ImportError: No module named can`, double-check that the
virtual environment is activated and that `pip install` completed successfully.

## 3. Hardware Wiring

The repository's `README.md` contains pin mappings and diagrams.  In short:

1. Power the MCP2515 board with 5 V (or 3.3 V if the board lacks a regulator).
2. Connect SPI pins (`SCK`, `MOSI`, `MISO`, `CS`) to your MCU or single-board computer.
3. Tie grounds together.
4. Connect `CANH` to OBD-II pin 6 and `CANL` to pin 14.

**Common mistakes**
- Forgetting the ground connection between boards.
- Mixing up `CANH` and `CANL` lines.
- Using the wrong voltage level for the module.

## 4. Configure SocketCAN

The `can_monitor.py` tool automatically runs `modprobe` and `ip` commands, but you can
also configure the interface manually:

```bash
sudo modprobe can
sudo modprobe can_raw
sudo ip link set can0 down
sudo ip link set can0 up type can bitrate 500000
```

Enable listen-only mode if you do not intend to transmit:

```bash
sudo ip link set can0 type can listen-only on
```

If the interface name differs (e.g. `can1`), adjust the commands accordingly.

**Troubleshooting tips**
- `Cannot find device "can0"`: check the interface name or SPI wiring.
- `RTNETLINK answers: Invalid argument`: the bitrate is not supported by the transceiver.
- Permissions error: ensure you run the commands with `sudo` or adequate privileges.

## 5. Monitor a Live Bus

Run the monitor with sensible defaults:

```bash
python -m can_monitor --interface can0 --bitrate 500000
```

Additional options:
- `--log can.log` – change log file path.
- `--log-level DEBUG` – increase verbosity.
- `--listen-only` – avoid transmitting frames.
- `--print-raw` – include raw CAN payloads in the log.

- `--config settings.json` – load options from a JSON file (`log_level`, startup
  `patches`, etc.).

Example `vcu_security_patch.json`:

```json
{
  "log_level": "INFO",
  "patches": {
    "vcu_enter_extended_session": {
      "can_id": 2016,
      "payload": "02 10 03 00 00 00 00 00",
      "response_id": 2024,
      "timeout_ms": 500,
      "retries": 1
    },
    "vcu_security_level1": {
      "can_id": 2016,
      "payload": "06 27 01 01 01 00 00 00",
      "response_id": 2024,
      "timeout_ms": 500,
      "retries": 2
    },
    "vcu_read_dtc": {
      "can_id": 2016,
      "payload": "03 19 02 FF 00 00 00 00",
      "response_id": 2024,
      "timeout_ms": 500,
      "retries": 1
    }
  }
}
```

Run the monitor with the patch enabled:

```bash
python -m can_monitor --interface can0 --bitrate 250000 --config vcu_security_patch.json
```

When `--print-raw` is supplied, the log records the raw CAN payload for each
frame and, when decoding succeeds, the decoded signal dictionary.  When decoding
fails, the log includes a message and the metrics counter `decoding_failures` is
incremented.

**Potential issues**
- `python-can is required but not installed`: install dependencies with pip.
- Repeated `CAN error: Bus-off state detected`: verify wiring, termination resistors,
  and that all devices share the same bitrate.
- Log file not written: check file permissions and disk space.

## 6. Decoding BLF Log Files

To decode an offline BLF capture:

```bash
python -m blf_decoder PV11-yadwad_0004465_20250102_012231.blf
```

Use `--dbc` to specify an alternate database:

```bash
python -m blf_decoder sample.blf --dbc path/to/custom.dbc
```

**Common pitfalls**
- Wrong DBC path produces `DBC file not found` warnings.
- Missing dependencies (`cantools`, `python-can`) cause runtime errors.

## 7. Serializing and Transporting Frames

The monitor can serialize frames and forward them via HTTP or MQTT using the
classes in `transport.py` and `serialization.py`.  Example snippet:

```python
from transport import HTTPTransport
from serialization import serialize_frame

transport = HTTPTransport("https://example.com/endpoint")
payload = serialize_frame(0x123, b"\x01\x02", {"speed": 10}, "json")
transport.send(payload)
```

If you select the MQTT transport, ensure `paho-mqtt` is installed.  Network failures
are retried automatically but ultimately raise exceptions after the configured number
of retries.

## 8. Metrics and Health Checks

The `metrics` module tracks simple counters (`bus_errors`, `restarts`,
`decoding_failures`).  To expose metrics over HTTP:

```python
from metrics import start_http_server
start_http_server(port=8000)
```

To write metrics to a file:

```python
from metrics import set_output_file
set_output_file("metrics.json")
```

## 9. Running Tests

Execute the unit tests to verify the environment:

```bash
pytest
```

All tests should pass.  Failures often indicate missing dependencies or a Python
version mismatch.

## 10. Troubleshooting Summary

| Symptom | Likely Cause | Resolution |
|--------|--------------|-----------|
| `python-can is required but not installed` | Dependencies missing | Run `pip install -r requirements.txt` |
| `DBC file not found` | Wrong path or file missing | Provide correct path or copy `OBD.dbc` into `src/` |
| `Bus-off state detected` | Wiring fault, incorrect bitrate | Check wiring, termination, bitrate settings |
| Interface `can0` does not exist | SPI module not detected or different name | Verify wiring, run `ip link` to list interfaces |
| Empty logs | No frames on bus or interface down | Ensure vehicle ignition is on and interface is up |

## 11. Further Help

If issues persist, increase log verbosity with `--log-level DEBUG`, review the
hardware connections, and consult the `python-can` and `cantools` documentation for
additional guidance.

