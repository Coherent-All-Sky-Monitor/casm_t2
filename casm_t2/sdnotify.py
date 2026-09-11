"""Minimal sd_notify client for systemd ``Type=notify`` units.

One datagram of ``KEY=value`` text to the AF_UNIX socket named in
``$NOTIFY_SOCKET``; a leading ``@`` means the abstract namespace, spelled in
Python as a leading NUL. Stdlib only. Every call is a no-op when the variable
is unset, and failures are logged, never raised.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)


def notify(state: str) -> bool:
    """Send one sd_notify line. Returns True if it went out."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError as exc:
        logger.debug("sd_notify(%r) failed: %s", state, exc)
        return False
    return True


def ready() -> bool:
    """Announce startup is complete (unit becomes 'active')."""
    return notify("READY=1")


def watchdog() -> bool:
    """Pet the watchdog. Must arrive more often than WatchdogSec."""
    return notify("WATCHDOG=1")


def status(text: str) -> bool:
    """Set the one-line status shown by `systemctl status`."""
    return notify(f"STATUS={text}")
