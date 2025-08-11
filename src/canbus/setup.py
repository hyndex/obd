"""CAN bus setup utilities.

This module centralizes system-level commands required to configure a
SocketCAN interface.  Configuring interfaces requires either root privileges
or the ``CAP_NET_ADMIN`` capability; the latter can be granted with
``setcap cap_net_admin+ep`` on the Python interpreter.  A pluggable command
interface allows the commands to be mocked during unit tests or replaced for
alternative hardware variants.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Protocol, Sequence


def _has_cap_net_admin() -> bool:
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("CapEff:"):
                    value = int(line.split()[1], 16)
                    return bool(value & (1 << 12))  # CAP_NET_ADMIN
    except OSError:
        pass
    return False


class CommandRunner(Protocol):
    """Protocol for objects capable of running system commands."""

    def modprobe(self, module: str) -> int:
        """Load a kernel module."""

    def ip(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Run an ``ip`` command with the given arguments."""


class SystemCommands:
    """Run commands on the host system using :func:`subprocess.run`.

    Each command is executed with an argument list rather than a shell
    string.  This avoids invoking the user's shell and mirrors how the
    :mod:`subprocess` module is typically used in the library code.
    """

    def modprobe(self, module: str) -> int:
        return subprocess.run(["modprobe", module], check=False).returncode

    def ip(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ip", *args], check=False, capture_output=True, text=True
        )


class MockCommands:
    """Record commands instead of executing them.

    This implementation is useful in unit tests where side effects are
    undesirable.  For readability, each command is stored as the string
    that would be executed on the command line.  ``ip`` results may be
    pre-populated via :attr:`ip_results` to simulate failures.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.ip_results: list[tuple[int, str]] = []

    def modprobe(self, module: str) -> int:  # pragma: no cover - simple
        cmd = f"modprobe {module}"
        self.commands.append(cmd)
        return 0

    def ip(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        cmd = "ip " + " ".join(args)
        self.commands.append(cmd)
        rc, err = (0, "")
        if self.ip_results:
            rc, err = self.ip_results.pop(0)
        return subprocess.CompletedProcess(["ip", *args], rc, "", err)


def setup_interface(
    interface: str,
    bitrate: int,
    listen_only: bool = False,
    *,
    commands: CommandRunner | None = None,
) -> None:
    """Configure a SocketCAN interface.

    Requires either root privileges or the ``CAP_NET_ADMIN`` capability to
    execute the underlying ``ip`` commands.

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

    if os.geteuid() != 0 and not _has_cap_net_admin():
        msg = "Configuring CAN interfaces requires root or CAP_NET_ADMIN capability"
        logging.critical(msg)
        raise RuntimeError(msg)

    if cmd.modprobe("can") != 0:
        logging.warning("Failed to load 'can' kernel module")
    if cmd.modprobe("can_raw") != 0:
        logging.warning("Failed to load 'can_raw' kernel module")

    def run_ip(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = cmd.ip(args)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "Operation not permitted" in err:
                raise RuntimeError(f"Permission denied running 'ip {' '.join(args)}'")
            if "Invalid argument" in err:
                raise ValueError(
                    "Invalid argument running 'ip {}' – unsupported bitrate".format(
                        " ".join(args)
                    )
                )
            logging.warning("ip %s failed: %s", " ".join(args), err)
        return result

    run_ip(["link", "set", interface, "down"])

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
    run_ip(up_args)

    if listen_only:
        run_ip(
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
