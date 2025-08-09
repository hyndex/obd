# obd

![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![Test Status](https://img.shields.io/badge/tests-passing-brightgreen)

## Pin Mapping and Power Requirements

Common MCP2515 CAN controller modules use SPI to interface with microcontrollers.
The table below shows typical connections when using an Arduino Uno. Adapt as needed
for your platform.

| MCP2515 Pin | Arduino Uno Pin | Notes |
|-------------|-----------------|------|
| VCC         | 5V              | Some modules have onboard 3.3 V regulator. Use 3.3 V modules directly. |
| GND         | GND             | Common ground between boards. |
| CS          | D10             | Chip select for SPI. |
| SCK         | D13             | SPI clock. |
| SI (MOSI)   | D11             | Master out, slave in. |
| SO (MISO)   | D12             | Master in, slave out. |
| INT         | D2 (optional)   | Interrupt pin used for message alerts. |
| CANH        | OBD-II pin 6    | Connect to the vehicle’s CAN high line. |
| CANL        | OBD-II pin 14   | Connect to the vehicle’s CAN low line. |

**Power:** Most MCP2515 boards require 5 V and draw around 60–70 mA.
If your board is 3.3 V only, ensure the MCU's logic levels are compatible
or use level shifting. Never feed 12 V from the vehicle directly into the board.

## Disclaimers

- Working on vehicle networks can void your manufacturer warranty.
- Ensure the vehicle is secured and follow standard electrical safety practices.
- Check local laws; accessing or modifying in-vehicle networks may be restricted.

## MCP2515 Wiring Diagrams

These diagrams illustrate a typical wiring sequence for a common MCP2515 module.

### Step 1 – Power

![Step 1: Power](docs/mcp2515_step1_power.svg)

### Step 2 – SPI Wiring

![Step 2: SPI Wiring](docs/mcp2515_step2_spi.svg)

### Step 3 – Connect to Vehicle

![Step 3: Connect to Vehicle](docs/mcp2515_step3_can.svg)

## Dependencies

The utilities and tests rely on a few Python packages:

- [`python-can`](https://python-can.readthedocs.io/)
- [`cantools`](https://cantools.readthedocs.io/)
- [`paho-mqtt`](https://www.eclipse.org/paho/)
- [`opendbc`](https://github.com/commaai/opendbc) – optional, used as a fallback set of community DBC files

Install them with `pip install -r requirements.txt`.

If the bundled `OBD.dbc` cannot be loaded, the CAN monitor will
automatically fall back to the community DBC files provided by
[`opendbc`](https://github.com/commaai/opendbc).

## Usage Guide

For step-by-step setup, configuration, and troubleshooting instructions, see
the [getting started guide](docs/GETTING_STARTED.md).

## BLF Log Decoding

A small helper script, `blf_decoder.py`, can decode Vector BLF log files
using the bundled `OBD.dbc` database:

```bash
python -m blf_decoder PV11-yadwad_0004465_20250102_012231.blf
```

Pass `--dbc` to supply an alternative DBC file.  Each decoded frame is
printed as `id`, raw hex payload and the parsed signal dictionary.

## UDS Integration

The CAN monitor can interpret Unified Diagnostic Services (UDS) responses
when provided with an `uds` section in the JSON configuration passed via
`--config`.  The section defines CAN IDs for requests and responses,
diagnostic trouble code (DTC) metadata and ISO-TP flow control options.

```json
{
  "uds": {
    "ecu_request_id": 2016,
    "ecu_response_id": 2024,
    "dtcs": {
      "P20F9": {
        "description": "Power stack motor over-temperature (>100°C)",
        "severity": "CRITICAL",
        "alert": true,
        "component": "PS"
      }
    },
    "flow_control": {"block_size": 0, "st_min_ms": 0}
  }
}
```

Multi-frame UDS responses are reassembled automatically.  When DTC
information (service `0x19`) is received, entries found in `uds.dtcs`
are logged with their description and severity, and any critical codes
emit an alert in the log output.

A sample configuration is bundled as `uds_config.json` for quick access.

The low level ``UDSClient`` helper used by the monitor also exposes a
configurable timeout.  The ``timeout`` argument of ``send`` and ``request`` may
be either a single float or a ``(N_Bs, N_Cr)`` tuple to independently limit how
long the client waits for Flow Control frames and for response data.
