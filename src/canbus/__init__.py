"""Utilities for CAN bus management.

Configuring interfaces requires either root privileges or the
``CAP_NET_ADMIN`` capability.  Grant the capability with ``setcap
cap_net_admin+ep`` on the Python interpreter to avoid running as root.
"""

from .setup import setup_interface, SystemCommands, MockCommands

__all__ = ["setup_interface", "SystemCommands", "MockCommands"]
