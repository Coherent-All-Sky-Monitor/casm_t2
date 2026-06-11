"""Sample-range reads of hella candidate .dat files.

Hella appends one block of lines per processed gulp, so sample numbers are
monotonic at gulp granularity (within a gulp the trial order is arbitrary).
That makes a byte-offset bisect on the second column safe down to one gulp
of slack, and lets a replay over a few hours seek straight to its slice of
a multi-day, multi-GB file instead of scanning from the top.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Iterator

from casm_t2 import wire

# One hella gulp is 8.192 s ~ 7813 samples; use a generous bound for the
# bisect slack and the end-of-range stop condition.
GULP_SLACK_SAMP = 16384


def _next_samp(f: IO[bytes], pos: int) -> tuple[int, int] | None:
    """(samp, line_offset) of the first parseable line at or after pos."""
    f.seek(pos)
    if pos:
        f.readline()  # discard the partial line we landed in
    while True:
        off = f.tell()
        line = f.readline()
        if not line:
            return None
        parts = line.split()
        if len(parts) >= 7:
            try:
                return int(parts[1]), off
            except ValueError:
                pass


def _seek_samp(f: IO[bytes], samp_target: int, size: int) -> int:
    """Byte offset of a line whose samp is safely before samp_target."""
    lo, hi = 0, size
    while hi - lo > 1 << 20:  # stop bisecting at 1 MiB, cheap to scan
        mid = (lo + hi) // 2
        probe = _next_samp(f, mid)
        if probe is None or probe[0] >= samp_target:
            hi = mid
        else:
            lo = mid
    return lo


def iter_span(path: str | Path, samp_min: int, samp_max: int) -> Iterator[wire.Candidate]:
    """Yield candidates with samp in [samp_min, samp_max] from one .dat file."""
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        start = _seek_samp(f, samp_min - GULP_SLACK_SAMP, size)
        f.seek(start)
        if start:
            f.readline()
        for raw in f:
            c = wire.parse_line(raw.decode(errors="replace"))
            if c is None:
                continue
            if c.samp > samp_max + GULP_SLACK_SAMP:
                break
            if samp_min <= c.samp <= samp_max:
                yield c
