"""TCP client for the Fourier Space casm_cand_dump control interface.

The dump daemons accept a single ASCII message per connection::

    COMMAND DUMP
    DUMP_UTC_START 2026-06-10-18:49:03.250
    DUMP_UTC_STOP 2026-06-10-18:49:08.250

followed by a shutdown of the write side, and reply with "OK" or an error
string. The requested window must still be inside the daemon's ring buffer
(roughly the last 20 seconds), so callers are expected to fire promptly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from datetime import datetime, timedelta, timezone

from .beams import stream_location
from .timing import format_dada_utc, parse_dada_utc

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0


def build_dump_command(utc_start: datetime, utc_stop: datetime) -> str:
    """Render the dump command for a UTC window."""
    if utc_stop <= utc_start:
        raise ValueError("dump window is empty or inverted")
    return (
        "COMMAND DUMP\n"
        f"DUMP_UTC_START {format_dada_utc(utc_start)}\n"
        f"DUMP_UTC_STOP {format_dada_utc(utc_stop)}"
    )


def request_dump(host: str, port: int, utc_start: datetime, utc_stop: datetime,
                 timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Send a dump command and return the daemon's reply (blocking)."""
    cmd = build_dump_command(utc_start, utc_stop)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(cmd.encode())
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(timeout)
        reply = sock.recv(4096).decode(errors="replace").strip()
    logger.info("dump %s:%d [%s .. %s] -> %r", host, port,
                format_dada_utc(utc_start), format_dada_utc(utc_stop), reply)
    return reply


async def request_dump_async(host: str, port: int, utc_start: datetime, utc_stop: datetime,
                             timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Async variant of :func:`request_dump`."""
    cmd = build_dump_command(utc_start, utc_stop)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.write(cmd.encode())
        await writer.drain()
        writer.write_eof()
        reply_bytes = await asyncio.wait_for(reader.read(4096), timeout)
    finally:
        writer.close()
        await writer.wait_closed()
    reply = reply_bytes.decode(errors="replace").strip()
    logger.info("dump %s:%d [%s .. %s] -> %r", host, port,
                format_dada_utc(utc_start), format_dada_utc(utc_stop), reply)
    return reply


def main() -> None:
    """Manual dump trigger, mainly for smoke tests.

    Examples:
        t2-dump --stream 0 --last 5            # the 5 s ending 2 s ago
        t2-dump --stream 6 --start 2026-06-10-19:01:02 --stop 2026-06-10-19:01:07
    """
    p = argparse.ArgumentParser(description="Trigger a beam intensity dump")
    p.add_argument("--stream", type=int, required=True, help="global stream index 0-7")
    p.add_argument("--start", help="DUMP_UTC_START (PSRDADA UTC)")
    p.add_argument("--stop", help="DUMP_UTC_STOP (PSRDADA UTC)")
    p.add_argument("--last", type=float, help="dump this many seconds, ending 2 s before now")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.last is not None:
        stop = datetime.now(timezone.utc) - timedelta(seconds=2)
        start = stop - timedelta(seconds=args.last)
    elif args.start and args.stop:
        start, stop = parse_dada_utc(args.start), parse_dada_utc(args.stop)
    else:
        p.error("provide either --last or both --start and --stop")

    loc = stream_location(args.stream)
    reply = request_dump(loc.host, loc.control_port, start, stop, timeout=args.timeout)
    print(f"{loc.host}:{loc.control_port} replied: {reply}")


if __name__ == "__main__":
    main()
