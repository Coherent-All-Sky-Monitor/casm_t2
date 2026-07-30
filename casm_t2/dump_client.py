"""TCP client for the Fourier Space casm_cand_dump control interface.

The dump daemons accept a single ASCII message per connection::

    COMMAND DUMP
    DUMP_UTC_START 2026-06-10-18:49:03.250
    DUMP_UTC_STOP 2026-06-10-18:49:08.250

followed by a shutdown of the write side, and reply with "OK" or an error
string. The requested window must still be inside the daemon's ring buffer
(roughly the last 20 seconds), so callers are expected to fire promptly.

Two CLIs live here: ``t2-dump`` for a single beam intensity stream, and
``casm-voltage-dump`` for the antenna-side raw voltage daemons, whose ring
reaches ~28 s back and whose dumps are large enough to need a disk check
first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .beams import (NVOLTAGE_STREAM, VOLTAGE_DUMP_DIR, VOLTAGE_ENDPOINTS, VOLTAGE_MOUNT,
                    stream_location, voltage_endpoint)
from .timing import format_dada_utc, parse_dada_utc

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0

# Raw voltage rate per antenna stream (the a0 ring's BYTES_PER_SECOND).
# All volumes below are decimal GB, i.e. 1e9 bytes.
VOLTAGE_BYTES_PER_SECOND = 2_062_500_000

# The daemons run with CAND_DUMP_READ_DELAY 28, so a request whose start is
# older than that is refused. Leave 2 s for transit and processing.
VOLTAGE_LOOKBACK_S = 26.0

# --last windows stop this far before now (data still in flight); --next
# windows start this far after now (command transit plus operator slack).
PAST_LAG_S = 2.0
FUTURE_LEAD_S = 5.0

# Each node hosts three antenna streams, and casm_cand_dump_disk demands
# room for all three before it writes anything — commanding fewer streams
# does not lower the bar.
VOLTAGE_STREAMS_PER_NODE = 3

# The casm_t3 janitor keeps each stream_N tree under 150 GB, deleting the
# oldest files first, so a dump longer than ~72 s per stream starts eating
# itself as soon as it lands.
JANITOR_QUOTA_BYTES = 150_000_000_000

# Headroom the live recorders sharing VOLTAGE_MOUNT need; same number as
# t2d's trigger.disk_floor_gb.
DISK_FLOOR_BYTES = 200_000_000_000


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


async def request_voltage_dump_async(utc_start: datetime, utc_stop: datetime,
                                     timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, str]:
    """Request a voltage dump of the window from every antenna-stream daemon.

    All six endpoints (three per node) are commanded concurrently; the
    result maps "host:port" to the daemon reply or the error string. A
    partial dump is still useful, so per-endpoint failures do not raise.
    """
    async def one(host: str, port: int) -> tuple[str, str]:
        try:
            reply = await request_dump_async(host, port, utc_start, utc_stop, timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            reply = f"ERROR {exc}"
        return f"{host}:{port}", reply

    results = await asyncio.gather(*(one(h, p) for h, p in VOLTAGE_ENDPOINTS))
    return dict(results)


def main() -> None:
    """Manual dump trigger, mainly for smoke tests.

    Examples:
        t2-dump --stream 0 --last 5            # the 5 s ending 2 s ago
        t2-dump --stream 6 --start 2026-06-10-19:01:02 --stop 2026-06-10-19:01:07
    """
    p = argparse.ArgumentParser(description="Trigger a beam intensity dump")
    p.add_argument("--stream", type=int, required=True, help="global stream index 0-7")
    p.add_argument("--start",
                   help="DUMP_UTC_START (PSRDADA UTC, e.g. 2026-07-30-22:25:22.958)")
    p.add_argument("--stop",
                   help="DUMP_UTC_STOP (PSRDADA UTC, e.g. 2026-07-30-22:25:24.958)")
    p.add_argument("--last", type=float, help="dump this many seconds, ending 2 s before now")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.last is not None:
        stop = datetime.now(timezone.utc) - timedelta(seconds=PAST_LAG_S)
        start = stop - timedelta(seconds=args.last)
    elif args.start and args.stop:
        start, stop = parse_dada_utc(args.start), parse_dada_utc(args.stop)
    else:
        p.error("provide either --last or both --start and --stop")

    loc = stream_location(args.stream)
    reply = request_dump(loc.host, loc.control_port, start, stop, timeout=args.timeout)
    print(f"{loc.host}:{loc.control_port} replied: {reply}")


def parse_streams(spec: str) -> list[int]:
    """Parse a comma-separated voltage stream list ("3,4") into sorted indices."""
    streams = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            stream = int(token)
        except ValueError:
            raise ValueError(f"not a stream index: {token!r}") from None
        if not 0 <= stream < NVOLTAGE_STREAM:
            raise ValueError(f"voltage stream {stream} out of range 0-{NVOLTAGE_STREAM - 1}")
        streams.add(stream)
    if not streams:
        raise ValueError("no streams selected")
    return sorted(streams)


def voltage_window(now: datetime, *, start: str | None = None, stop: str | None = None,
                   last: float | None = None, ahead: float | None = None) -> tuple[datetime, datetime]:
    """Resolve a requested voltage dump window to (utc_start, utc_stop).

    Exactly one of three forms: an explicit start/stop pair of PSRDADA UTC
    strings, ``last`` seconds of the recent past (ending PAST_LAG_S before
    now), or ``ahead`` seconds of the near future (starting FUTURE_LEAD_S
    from now). ``now`` is passed in rather than read from the clock so the
    arithmetic is testable.
    """
    given = [start is not None or stop is not None, last is not None, ahead is not None]
    if sum(given) != 1:
        raise ValueError("give exactly one of --start/--stop, --last, --next")

    if last is not None:
        if last <= 0:
            raise ValueError("--last must be positive")
        utc_stop = now - timedelta(seconds=PAST_LAG_S)
        utc_start = utc_stop - timedelta(seconds=last)
    elif ahead is not None:
        if ahead <= 0:
            raise ValueError("--next must be positive")
        utc_start = now + timedelta(seconds=FUTURE_LEAD_S)
        utc_stop = utc_start + timedelta(seconds=ahead)
    else:
        if not (start and stop):
            raise ValueError("--start and --stop go together")
        utc_start, utc_stop = parse_dada_utc(start), parse_dada_utc(stop)

    if utc_stop <= utc_start:
        raise ValueError("dump window is empty or inverted")
    return utc_start, utc_stop


def ring_age_refusal(now: datetime, utc_start: datetime) -> str | None:
    """Complaint if the window starts further back than the voltage ring reaches."""
    age_s = (now - utc_start).total_seconds()
    if age_s > VOLTAGE_LOOKBACK_S:
        return (f"window starts {age_s:.1f} s in the past; the voltage ring holds "
                f"only the last {VOLTAGE_LOOKBACK_S:.0f} s")
    return None


def node_volumes(duration_s: float, streams: list[int]) -> dict[str, tuple[int, int, int]]:
    """Per node: (commanded streams, bytes written, bytes the disk guard wants).

    The guard counts all three streams the node hosts even when only one was
    commanded, and casm_cand_dump_disk drops the dump when the mount is under
    it — it logs the refusal, but the client is told nothing.
    """
    per_stream = duration_s * VOLTAGE_BYTES_PER_SECOND
    counts = Counter(voltage_endpoint(stream)[0] for stream in streams)
    return {host: (n, int(per_stream * n), int(per_stream * VOLTAGE_STREAMS_PER_NODE))
            for host, n in counts.items()}


def preflight_issues(duration_s: float, streams: list[int],
                     free_by_host: dict[str, int | None],
                     *, force: bool = False) -> tuple[list[str], list[str]]:
    """Complaints about a proposed voltage dump, as (refusals, warnings).

    A refusal means the dump is a bad idea for a reason no daemon will tell
    you about: it will either be eaten by the janitor or squeeze the live
    recorders off the mount. ``force`` returns every refusal as a warning
    instead, which is what ``--force`` does. Free space is passed in, and
    None for a host means we could not read it.
    """
    refusals: list[str] = []
    warnings: list[str] = []

    per_stream = duration_s * VOLTAGE_BYTES_PER_SECOND
    if per_stream > JANITOR_QUOTA_BYTES:
        quota_s = JANITOR_QUOTA_BYTES / VOLTAGE_BYTES_PER_SECOND
        refusals.append(
            f"{duration_s:.1f} s is {per_stream / 1e9:.0f} GB per stream, over the "
            f"janitor's {JANITOR_QUOTA_BYTES / 1e9:.0f} GB per-stream quota: anything "
            f"past ~{quota_s:.1f} s per stream gets deleted oldest-first, so label or "
            "move the data the moment it lands (--force to proceed anyway)")

    for host, (_, dump_bytes, guard_bytes) in node_volumes(duration_s, streams).items():
        free = free_by_host.get(host)
        if free is None:
            warnings.append(f"could not read free space on {host}; check it by hand")
            continue
        if free - dump_bytes < DISK_FLOOR_BYTES:
            refusals.append(
                f"{host}: writing {dump_bytes / 1e9:.1f} GB leaves "
                f"{(free - dump_bytes) / 1e9:.0f} GB on {VOLTAGE_MOUNT}, under the "
                f"{DISK_FLOOR_BYTES / 1e9:.0f} GB floor the live recorders need")
        if free < guard_bytes:
            warnings.append(f"{host} is already under the disk guard — the dump will "
                            "be discarded silently")
        elif free - dump_bytes < guard_bytes:
            warnings.append(f"{host}: this dump leaves the node under the disk guard — "
                            "the next one will be discarded silently")

    if force:
        return [], refusals + warnings
    return refusals, warnings


def free_bytes(host: str) -> int | None:
    """Free bytes on a node's dump mount, or None if we could not find out.

    The local node is read directly; the other one over ssh, which is only a
    convenience — a failed or slow ssh must not stop a dump.
    """
    if host == socket.gethostname().split(".")[0]:
        try:
            return shutil.disk_usage(VOLTAGE_MOUNT).free
        except OSError:
            return None
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                              "df", "--output=avail", "-B1", VOLTAGE_MOUNT],
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=DEFAULT_TIMEOUT_S, check=True)
        return int(out.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# --gather: after the daemons ack, wait for the files to finish and pull the
# remote node's streams over, so one node holds the whole dump.
# ---------------------------------------------------------------------------

_DUMP_NAME_RE = re.compile(r"^(?P<utc>.+)_(?P<offset>\d{16})\.(?P<num>\d{6})\.dada$")

GATHER_POLL_S = 5.0

# A dump is finished once its new files hold the commanded window's bytes;
# allow a whisker under for the window landing on sample boundaries.
GATHER_SIZE_TOLERANCE = 0.999


def local_hostname() -> str:
    """Short hostname, as the endpoint tables spell it."""
    return socket.gethostname().split(".")[0]


def expected_stream_bytes(duration_s: float) -> int:
    """Payload bytes one stream writes for a window of this length."""
    return int(duration_s * VOLTAGE_BYTES_PER_SECOND * GATHER_SIZE_TOLERANCE)


def stream_dir(stream: int) -> str:
    return f"{VOLTAGE_DUMP_DIR}/stream_{stream}"


def stream_file_sizes(host: str, stream: int) -> dict[str, int] | None:
    """{filename: bytes} for a stream's dump directory, or None if unreadable.

    The local node is read directly; the other one over ssh. A missing
    directory reads as empty — the writer creates it with the first dump.
    """
    path = stream_dir(stream)
    if host == local_hostname():
        try:
            return {e.name: e.stat().st_size for e in os.scandir(path)
                    if e.name.endswith(".dada")}
        except FileNotFoundError:
            return {}
        except OSError:
            return None
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host,
             f"find {path} -maxdepth 1 -name '*.dada' -printf '%f %s\\n' "
             f"2>/dev/null; true"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=DEFAULT_TIMEOUT_S, check=True)
        sizes = {}
        for line in out.stdout.splitlines():
            name, _, size = line.rpartition(" ")
            if name:
                sizes[name] = int(size)
        return sizes
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def stream_dump_state(before: set[str], sizes: dict[str, int],
                      expected_bytes: int) -> tuple[str, list[str]]:
    """("waiting" | "growing" | "done", new files) for one stream.

    New files are the ones absent from the pre-trigger listing; the dump is
    done when they hold the commanded window's payload (each file carries a
    4096-byte header on top).
    """
    new = sorted(n for n in sizes if n not in before)
    if not new:
        return "waiting", []
    payload = sum(sizes[n] for n in new) - 4096 * len(new)
    return ("done" if payload >= expected_bytes else "growing"), new


def reader_prefix(new_by_stream: dict[int, list[str]]) -> tuple[str | None, list[str]]:
    """VoltageReader timestamp prefix for a gathered dump, with any caveats.

    The full UTC_START_OBSOFFSET prefix pins this dump even when another one
    shares its UTC_START, but it only matches a dump's first file — so a
    dump split over several files falls back to the bare UTC_START, with a
    note. Streams that disagree on UTC_START return no prefix at all.
    """
    parsed = {}
    for stream, names in new_by_stream.items():
        matches = [_DUMP_NAME_RE.match(n) for n in names]
        if not matches or None in matches:
            return None, [f"stream {stream} has oddly named files: {names}"]
        parsed[stream] = sorted((m["utc"], m["offset"]) for m in matches)

    utcs = {p[0][0] for p in parsed.values()}
    if len(utcs) != 1:
        return None, [f"streams disagree on UTC_START: {sorted(utcs)} — "
                      "they are different dumps; gather them by hand"]
    utc = utcs.pop()

    first_offsets = {p[0][1] for p in parsed.values()}
    multi_file = any(len(p) > 1 for p in parsed.values())
    if multi_file:
        return utc, ["the dump spans several files per stream, so only the bare "
                     "UTC_START prefix matches them all — if another dump shares "
                     "it, the reader will refuse to stitch (see allow_gaps)"]
    if len(first_offsets) == 1:
        return f"{utc}_{first_offsets.pop()}", []
    return utc, ["streams start at different OBS_OFFSETs — expect the reader "
                 "to refuse them as different dumps"]


def gather_dump(streams: list[int], dest: str, before: dict[int, set[str]],
                stop: datetime, duration_s: float) -> int:
    """Wait for the dump files, pull the remote streams into dest.

    Returns the number of streams that never finished. Blocks from just
    after the trigger until the window has passed and every stream's new
    files hold the expected bytes (or a timeout of the dump length plus a
    minute past the window's end).
    """
    local = local_hostname()
    expected = expected_stream_bytes(duration_s)
    deadline = stop.timestamp() + duration_s + 60.0

    remaining = (stop - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        print(f"gather   window ends in {remaining:.0f} s; waiting")
        time.sleep(remaining)

    states: dict[int, tuple[str, list[str]]] = {s: ("waiting", []) for s in streams}
    while True:
        for stream in streams:
            if states[stream][0] == "done":
                continue
            host, _ = voltage_endpoint(stream)
            sizes = stream_file_sizes(host, stream)
            if sizes is None:
                print(f"gather   stream {stream}: cannot list {host}:{stream_dir(stream)}")
                continue
            states[stream] = stream_dump_state(before[stream], sizes, expected)
        pending = [s for s in streams if states[s][0] != "done"]
        if not pending:
            break
        if time.time() > deadline:
            for stream in pending:
                state, new = states[stream]
                print(f"gather   stream {stream} never finished ({state}"
                      f"{': ' + ', '.join(new) if new else ''}) — the writer may "
                      f"have dropped it (disk guard); check the node's log")
            break
        print(f"gather   waiting on stream(s) {', '.join(str(s) for s in pending)}")
        time.sleep(GATHER_POLL_S)

    done = {s: names for s, (state, names) in states.items() if state == "done"}
    failures = len(streams) - len(done)

    for stream, names in sorted(done.items()):
        host, _ = voltage_endpoint(stream)
        if host == local and dest == VOLTAGE_DUMP_DIR:
            continue  # already where it belongs
        dest_dir = f"{dest}/stream_{stream}"
        os.makedirs(dest_dir, exist_ok=True)
        if host == local:
            # Already on this node's disk: a symlink beats a second copy
            print(f"gather   stream {stream}: {len(names)} symlink(s) to local files")
            for n in names:
                link = os.path.join(dest_dir, n)
                try:
                    if not os.path.lexists(link):
                        os.symlink(os.path.join(stream_dir(stream), n), link)
                except OSError as exc:
                    print(f"gather   stream {stream}: symlink failed: {exc}")
                    failures += 1
                    break
            continue
        print(f"gather   stream {stream}: {len(names)} file(s) from {host}")
        sources = [f"{host}:{stream_dir(stream)}/{n}" for n in names]
        try:
            subprocess.run(["rsync", "-a", *sources, dest_dir + "/"],
                           stdin=subprocess.DEVNULL, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"gather   stream {stream}: rsync failed: {exc}")
            failures += 1

    if done:
        prefix, notes = reader_prefix(done)
        for note in notes:
            print(f"gather   NOTE: {note}")
        if prefix:
            print(f"\ngathered under {dest}/stream_N/ — read it with:")
            print(f"  VoltageReader({dest!r}, {prefix!r})")
    return failures


def voltage_main() -> None:
    """Manual voltage dump trigger for the antenna-stream casm_cand_dump daemons.

    Examples:
        casm-voltage-dump --next 2                  # 2 s starting 5 s from now
        casm-voltage-dump --last 5                  # the 5 s ending 2 s ago
        casm-voltage-dump --streams 3,4 --next 1    # one node only
        casm-voltage-dump --start 2026-07-31-18:00:00 --stop 2026-07-31-18:00:02
        casm-voltage-dump --last 2 --gather         # wait for the files and pull
                                                    # the other node's streams here

    These daemons are part of the Fourier Space stack and know nothing about
    T2, so this works whether or not t2d is running. Unlike
    :func:`request_voltage_dump_async`, which fans out to every endpoint at
    once, this sends to the selected streams one at a time on purpose: an
    operator wants each reply attributed as it arrives.
    """
    p = argparse.ArgumentParser(description="Trigger a raw voltage dump")
    p.add_argument("--start",
                   help="DUMP_UTC_START (PSRDADA UTC, e.g. 2026-07-30-22:25:22.958)")
    p.add_argument("--stop",
                   help="DUMP_UTC_STOP (PSRDADA UTC, e.g. 2026-07-30-22:25:24.958)")
    p.add_argument("--last", type=float, help="dump this many seconds, ending 2 s before now")
    p.add_argument("--next", type=float, dest="ahead", metavar="SECONDS",
                   help="dump this many seconds, starting 5 s from now (long dumps)")
    p.add_argument("--streams", default=",".join(str(s) for s in range(NVOLTAGE_STREAM)),
                   help=f"comma-separated stream list, default all 0-{NVOLTAGE_STREAM - 1}")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    p.add_argument("--force", action="store_true",
                   help="downgrade the janitor-quota and disk-floor refusals to warnings")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--gather", nargs="?", const=VOLTAGE_DUMP_DIR, default=None,
                   metavar="DEST_DIR",
                   help="after the daemons ack, wait for the files, rsync the "
                        "other node's streams into DEST_DIR/stream_N/ (local "
                        "streams are symlinked, not copied; default "
                        f"{VOLTAGE_DUMP_DIR}), then print the reader prefix")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    now = datetime.now(timezone.utc)
    try:
        streams = parse_streams(args.streams)
        start, stop = voltage_window(now, start=args.start, stop=args.stop,
                                     last=args.last, ahead=args.ahead)
    except ValueError as exc:
        p.error(str(exc))

    stale = ring_age_refusal(now, start)
    if stale and args.last is not None:
        raise SystemExit(f"error: {stale}")

    duration_s = (stop - start).total_seconds()
    print(f"window   {format_dada_utc(start)} .. {format_dada_utc(stop)}  ({duration_s:.3f} s)")
    print(f"streams  {', '.join(str(s) for s in streams)}")
    if stale:
        print(f"WARNING: {stale} — expect the daemons to refuse")

    volumes = node_volumes(duration_s, streams)
    free_by_host = {host: free_bytes(host) for host in volumes}
    for host, (n, dump_bytes, guard_bytes) in volumes.items():
        free = free_by_host[host]
        free_txt = "unknown" if free is None else f"{free / 1e9:.0f} GB"
        print(f"{host}  {n} stream(s), writes {dump_bytes / 1e9:.1f} GB, "
              f"needs {guard_bytes / 1e9:.1f} GB free, has {free_txt}")

    refusals, warnings = preflight_issues(duration_s, streams, free_by_host,
                                          force=args.force)
    for msg in warnings:
        print(f"WARNING: {msg}")
    for msg in refusals:
        print(f"REFUSING: {msg}")
    if refusals:
        raise SystemExit("nothing sent; pass --force if you really mean it")

    if args.dry_run:
        print("dry run: nothing sent")
        return

    if not args.yes:
        try:
            answer = input("proceed? [y/N] ")
        except EOFError:
            raise SystemExit("no answer on stdin; pass --yes to dump unattended") from None
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("aborted, nothing sent")

    # The prompt may have cost us the ring: a --last window that was inside
    # the lookback when we printed it can be past it by the time we send.
    stale = ring_age_refusal(datetime.now(timezone.utc), start)
    if stale and args.last is not None:
        raise SystemExit(f"error: the window went stale while you were deciding: {stale}")
    if stale:
        print(f"WARNING: {stale} — expect the daemons to refuse")

    # Snapshot each stream directory before triggering, so the gather step
    # can tell this dump's files from everything already there.
    before: dict[int, set[str]] = {}
    if args.gather is not None:
        for stream in streams:
            host, _ = voltage_endpoint(stream)
            sizes = stream_file_sizes(host, stream)
            if sizes is None:
                print(f"WARNING: cannot list {host}:{stream_dir(stream)} — "
                      f"gather will treat every file there as new")
            before[stream] = set(sizes or ())

    failed = 0
    for stream in streams:
        host, port = voltage_endpoint(stream)
        errored = False
        try:
            reply = request_dump(host, port, start, stop, timeout=args.timeout)
        except OSError as exc:
            reply, errored = f"ERROR {exc}", True
        print(f"stream {stream}  {host}:{port} -> {reply}")
        if errored:
            print("  the command may still have executed — check the stream directory")
            print("  before retrying, or the retry writes a second overlapping dump")
            print("  under the same UTC_START")
        if reply != "OK":
            failed += 1

    print()
    for stream in streams:
        host, _ = voltage_endpoint(stream)
        print(f"files    {host}:{VOLTAGE_DUMP_DIR}/stream_{stream}/")
    print('"OK" only means the daemon accepted the command. If the mount is under')
    print("the disk guard the writer drops the dump and says nothing back to us —")
    print("it logs the refusal on its own node — and slow dumps can still fail")
    print("late, so go and look at the directories above.")

    if args.gather is not None:
        failed += gather_dump(streams, args.gather, before, stop, duration_s)

    if failed:
        raise SystemExit(f"{failed} of {len(streams)} endpoints did not reply OK")


if __name__ == "__main__":
    main()
