"""CAN bus setup utilities.

This module centralizes system-level commands required to configure a
SocketCAN interface.  A pluggable command interface allows the commands to
be mocked during unit tests or replaced for alternative hardware variants.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol, Sequence


class CommandRunner(Protocol):
    """Protocol for objects capable of running system commands."""

    def modprobe(self, module: str) -> int:
        """Load a kernel module."""

    def ip(self, args: Sequence[str]) -> int:
        """Run an ``ip`` command with the given arguments."""


class SystemCommands:
    """Run commands on the host system using :func:`subprocess.run`."""

    def modprobe(self, module: str) -> int:
        return subprocess.run(["modprobe", module], check=False).returncode

    def ip(self, args: Sequence[str]) -> int:
        return subprocess.run(["ip", *args], check=False).returncode


class MockCommands:
    """Record commands instead of executing them.

    This implementation is useful in unit tests where side effects are
    undesirable.  Commands are stored in the :attr:`commands` list.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []

    def modprobe(self, module: str) -> int:  # pragma: no cover - simple
        cmd = f"modprobe {module}"
        self.commands.append(cmd)
        return 0

    def ip(self, args: Sequence[str]) -> int:  # pragma: no cover - simple
        cmd = "ip " + " ".join(args)
        self.commands.append(cmd)
        return 0


def setup_interface(
    interface: str,
    bitrate: int,
    listen_only: bool = False,
    *,
    commands: CommandRunner | None = None,
) -> None:
    """Configure a SocketCAN interface.

    Parameters
    ----------
    interface:
        Name of the interface (e.g. ``"can0"``).
    bitrate:
        Bus bitrate in bits per second.
    listen_only:
        If ``True``, the interface is placed in listen-only mode.
    commands:
        Optional command runner.  Defaults to :class:`SystemCommands`.
    """

    cmd = commands or SystemCommands()

    if cmd.modprobe("can") != 0:
        logging.warning("Failed to load 'can' kernel module")
    if cmd.modprobe("can_raw") != 0:
        logging.warning("Failed to load 'can_raw' kernel module")
    if cmd.ip(["link", "set", interface, "down"]) != 0:
        logging.warning("Failed to bring down %s", interface)

    up_args = [
        "link",
        "set",
        interface,
        "up",
        "type",
        "can",
        "bitrate",
        str(bitrate),
    ]
    if cmd.ip(up_args) != 0:
        logging.warning("Failed to configure %s", interface)

    if listen_only:
        if (
            cmd.ip(
                [
                    "link",
                    "set",
                    interface,
                    "type",
                    "can",
                    "listen-only",
                    "on",
                ]
            )
            != 0
        ):
            logging.warning("Failed to enable listen-only mode on %s", interface)
