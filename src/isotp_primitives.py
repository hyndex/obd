"""ISO-TP service primitive callbacks.

This module defines the :class:`TDataPrimitive` container which groups
callbacks corresponding to ISO-TP service primitives.  Applications can
provide callables to observe or modify transport-layer events.

The primitives include:

``req``
    Invoked when a transport layer request is made.
``con``
    Confirmation callback triggered after transmission completes.  It
    receives ``(success, error)`` where ``success`` is ``True`` when the
    request finished without raising and ``error`` contains the exception
    instance on failure.
``ind``
    Indication callback fired when a complete response payload has been
    reassembled.
``som_ind``
    Optional "start-of-message" indication fired when the first frame of a
    multi-frame response is received.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class TDataPrimitive:
    """Container for ISO-TP transport service callbacks."""

    req: Optional[Callable[[int, bytes], None]] = None
    ind: Optional[Callable[[bytes], None]] = None
    con: Optional[Callable[[bool, Exception | None], None]] = None
    som_ind: Optional[Callable[[], None]] = None
