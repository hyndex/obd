# OBD CAN Monitor

This project provides a small utility for monitoring a SocketCAN bus and
optionally decoding frames using a DBC file.  It is intended as a minimal
example for interacting with CAN hardware on Linux systems.

## Installation

The package uses the `src/` layout and can be installed in editable mode:

```bash
pip install -e .
```

## Configuration

Default values for the CAN interface, bitrate and log file can be supplied via
an INI configuration file.  By default a file named `can_monitor.ini` in the
current directory is read.  Example:

```ini
[can]
interface = can0
bitrate = 500000
log_path = can.log
```

Command line arguments override these settings.

## Usage

```bash
python -m obd.can_monitor --interface can0 --bitrate 500000 --log can.log
```

The script will configure the interface, connect to the bus and continuously
log both raw and decoded messages.

## Hardware setup

1. Connect your CAN transceiver to the host machine and ensure the SocketCAN
   driver is loaded.
2. Attach the CAN high and CAN low lines to the vehicle or device under test.
3. Ensure the specified bitrate matches that of the bus.

## Troubleshooting

- **Permission denied**: make sure your user has permission to access CAN
  devices or run the script with elevated privileges.
- **No messages logged**: check wiring and bitrate. Use a known-good CAN node
  to verify activity on the bus.
- **DBC decoding disabled**: install the `cantools` package and ensure the
  `OBD.dbc` file is present.

## Running tests

Tests are executed with `pytest` and require the optional `python-can` and
`cantools` dependencies:

```bash
pytest
```
