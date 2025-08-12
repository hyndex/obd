# VCU UDS Communication Guide

This document describes, in depth, how the on-board diagnostic tooling in this
repository communicates with the Vehicle Control Unit (VCU) using Unified
Diagnostic Services (UDS).  It covers the complete request/response flow,
configuration details, fault-code mapping and the handling of edge cases.

## Overview of the UDS Link

The CAN monitor establishes an ISO‑TP/UDS channel to the VCU.  Request and
response identifiers, addressing options and flow‑control tuning are defined in
`uds_config.json`.  The same configuration file also defines a ``sequence`` of
startup frames used to enter the diagnostic session, unlock security and poll
for DTCs.

```
UDSClient -> CAN bus -> VCU -> CAN bus -> UDSClient
```

The typical sequence is:

1. **Enter extended diagnostic session** – unlocks enhanced services.
2. **Obtain and submit security access key** – required before protected
   services such as DTC reading.
3. **Read DTCs** – retrieve current Diagnostic Trouble Codes.
4. **Flow control** – govern multi‑frame responses.

Each step and the associated configuration are detailed below.

## Session Initialisation

### Extended Session Request

The monitor sends `02 10 03 00 00 00 00 00` to request an extended diagnostic
session as the first step of the configuration ``sequence``.  The VCU replies
with `50 03` if the transition is successful.

### Security Access
After the session switch the client requests security level 1.  The first
request (`06 27 01 01 01 00 00 00`) asks the VCU for a seed.  The client then
computes the corresponding key and submits it in a follow‑up `0x27` request.  The
algorithm used to derive the key is pluggable: by default the seed bytes are
bit‑wise inverted, but a custom function or ``module:attr`` string can be
supplied in configuration under ``uds.security.algorithm``.

If the VCU does not answer with a positive response (`0x67`) or the computed key
is rejected, :meth:`security_access` raises :class:`ISOTransportError` with the
negative response code, allowing the caller to surface a detailed error and
abort diagnostic polling.

### Multi‑Frame Flow Control

Large responses are segmented using ISO‑TP.  The receiver advertises its
capabilities with a Flow Control (FC) frame.  For the VCU, a CTS frame
`30 01 05 00 00 00 00 00` permits the sender to transmit one consecutive frame at
a time (`block_size` = 1) with a minimum separation of 5 ms (`st_min_ms` = 5).
On Raspberry Pi hardware, increasing `st_min_ms` to 5–10 ms helps prevent missed
frames due to scheduling jitter. These defaults are configurable in the JSON file:

```json
"flow_control": { "block_size": 1, "st_min_ms": 5 }
```

If the VCU responds with an FC status **WAIT** (0x1) transmission pauses but the
`N_Bs` timer continues.  A status of **Overflow** (0x2) or a timeout raises an
`ISOTransportError` and terminates the transfer.

## Diagnostic Trouble Code Retrieval

Once security is unlocked the monitor issues
`03 19 02 FF 00 00 00 00` to read all stored DTCs.  The VCU responds with a
multi‑frame message containing a count and one or more 3‑byte DTC entries.  Each
code is decoded, mapped to metadata defined in the configuration and logged.
Critical codes (those with `"alert": true`) trigger an explicit alert in the
output.

## DTC and P‑Code Mapping

`uds_config.json` contains the complete mapping from P‑codes to descriptive
metadata.  Each entry defines a user‑friendly description, severity and optional
alert flag.  Example:

```json
"P058D": {
  "description": "Dual Aux communication failure",
  "severity": "MAJOR",
  "alert": false,
  "component": "AUX"
}
```

A condensed reference is provided below (Level 1 unless noted otherwise):

| PCODE | DTC Code | Description | Impact on Vehicle |
|-------|---------|-------------|-------------------|
| P058D | P11 00 | Dual_Aux_Com_fail | Auxiliary network fault. |
| P061D | P11 01 | Dual_Aux_Pre_char_flt | Pre‑charge failed; DC link < 200 V. |
| P1010 | P11 02 | Dual_aux_Dev_int_Over_temp | Internal AUX device temperature > 65 °C. |
| P1011 | P11 03 | Pressure_sense1_open | Pressure sensor 1 reading ~12. |
| P161D | P11 04 | Pressure_sense2_open | Pressure sensor 2 reading ~12. |
| P162E | P11 05 | Air_comp_aux_Power_stack | AUX power stack temperature > 65 °C. |
| P1BA4 | P11 06 | Air_Comp_Over_temp_flt | Air compressor motor > 100 °C. |
| P1BA5 | P11 07 | Air_Comp_Over_Curr_flt | Air compressor current > 5 A for >30 s. |
| P20F5 | P11 08 | Air_Comp_Volt_build_flt | Voltage did not reach ~400 V in time. |
| P20F6 | P11 09 | PS_aux_Power_stack | Power‑stack temperature > 65 °C. |
| P20F9 | P11 10 (Level 2) | PS_Over_temp_flt | Power‑stack motor > 100 °C. |
| P20FA | P11 11 | PS_Over_curr_flt | Power‑stack current > 5 A for >30 s. |
| P2114 | P11 12 | PS_Volt_Build_bld | Power‑stack voltage failed to build. |
| P2115 | P11 13 (Level 2) | Air_Comp_Flt | AUX signalled compressor fault. |
| P2116 | P11 14 | PS motor Flt | AUX signalled inverter fault. |
| P2117 | P11 15 | Reserved | Unspecified fault. |
| P2146 | P11 15 | Reserved | Unspecified fault. |
| P21E5 | P11 16 | Reserved | Unspecified fault. |
| P0071–P0074 | P11 17–P11 20 | Ambient air temperature sensor errors. |
| P0075 | – | Intake valve control solenoid circuit. |
| P007C | – | Charge air cooler temperature sensor circuit low. |
| P007D | – | Charge air cooler temperature sensor circuit high. |

The JSON file already contains entries for all of the above codes; no schema
changes are required.

## Configuration Schema and Customisation
The root `uds` object defines the CAN addressing, security level and flow
control parameters used by the monitor:

```json
{
  "ecu_request_id": 2016,
  "ecu_response_id": 2024,
  "is_extended_id": false,
  "source_address": 241,
  "target_address": 16,
  "address_extension": null,
  "session": 3,
  "security": { "level": 1, "key": null },
  "flow_control": { "block_size": 1, "st_min_ms": 5 },
  "dtcs": { ... }
}
```

* **Identifiers** – `ecu_request_id` and `ecu_response_id` are the arbitration
  IDs for requests and responses.  Setting both `source_address` and
  `target_address` activates 29‑bit normal‑fixed addressing automatically.
* **Session** – the `session` field selects the diagnostic session (3 = extended).
  The code automatically invokes `change_session` before other actions.
* **Security** – `security.level` sets the access level.  To supply a fixed key
  instead of computing one, populate `security.key` with an 8‑byte hex string.
* **Flow Control** – `block_size` and `st_min_ms` tune ISO‑TP reception.  Larger
  block sizes trade memory for throughput; a non‑zero `st_min_ms` throttles the
  sender.  Raspberry Pi systems typically require `st_min_ms` of 5–10 ms to
  reliably pace responses.
* **DTC Mapping** – extend the `dtcs` dictionary with additional P‑codes as
  needed.  Each entry must include `description`, `severity` and `component`.  An
  optional `alert` flag promotes the code to a high‑priority log entry.

Changes take effect the next time the monitor loads the configuration.  No code
modifications are required unless a new security algorithm is needed.

## Edge‑Case Handling

The ISO‑TP implementation guards against a variety of failure modes:

* **Missing FC frames** – if no Flow Control frame arrives after a First Frame,
  an `ISOTransportError` is raised and transmission aborts.
* **Flow Control Overflow** – an FC status of Overflow also raises the same
  error.
* **Timeouts during blocks** – failing to receive a new FC frame after the
  advertised block size triggers a timeout exception.
* **Security or Session failure** – negative responses or lack of response for
  session change or security access halt further communication.

All exceptions propagate to the monitor, which logs the error and stops
interacting with the VCU until restarted.

## Updating the Configuration

1. **Add or modify DTCs** – edit `uds_config.json` and append new entries under
   `dtcs`.  Follow the existing schema.  Reload the monitor to apply.
2. **Adjust flow control** – change `flow_control.block_size` or
   `flow_control.st_min_ms` to tune throughput vs. bus load.  When running on a
   Raspberry Pi, values around 5–10 ms are typical.
3. **Change security level or key** – set `security.level` and optionally provide
   a `security.key` for ECUs requiring a known key.
4. **Alter session or CAN IDs** – modify `session`, `ecu_request_id` or
   `ecu_response_id` to match different ECUs.

These settings allow the tooling to adapt to future firmware updates or entirely
new vehicles without code changes.

## Summary

The UDS implementation follows the complete extended‑session and security
handshake before polling DTCs.  It honours the configuration sequence shipped
with this repository and provides hooks to customise security algorithms, flow
control and DTC metadata.  Robust error handling ensures the session terminates
cleanly when unexpected conditions occur.
