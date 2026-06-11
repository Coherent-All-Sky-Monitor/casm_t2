"""Parsing of the candidate stream emitted by casm-hella (T1).

Each hella job sends one TCP payload per processed gulp: a first line with
the gulp number, then one line per candidate. The deployed binary writes
seven columns::

    snr  samp  time_days  width  dm_idx  dm  beam

Older builds wrote eight columns with the sample number duplicated
(``snr samp samp time_days width dm_idx dm beam``); both layouts are
accepted here. ``beam`` is globally indexed (0-511), i.e. it already
includes the job's BEAM0 offset. ``time_days`` is derived from ``samp``
inside hella and should not be trusted for absolute timing; use
:mod:`casm_t2.timing` instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A single T1 detection, one boxcar/DM/beam trial above threshold."""

    snr: float
    samp: int
    time_days: float
    width: int  # log2 of the boxcar length in samples (0 -> 1 samp)
    dm_idx: int
    dm: float
    beam: int


def parse_line(line: str) -> Candidate | None:
    """Parse one candidate line, returning None for blank or malformed input."""
    fields = line.split()
    if len(fields) == 7:
        snr, samp, time_days, width, dm_idx, dm, beam = fields
    elif len(fields) == 8:
        snr, samp, _, time_days, width, dm_idx, dm, beam = fields
    else:
        if fields:
            logger.warning("unparseable candidate line (%d fields): %r", len(fields), line)
        return None
    try:
        return Candidate(
            snr=float(snr),
            samp=int(samp),
            time_days=float(time_days),
            width=int(width),
            dm_idx=int(dm_idx),
            dm=float(dm),
            beam=int(beam),
        )
    except ValueError:
        logger.warning("unparseable candidate line: %r", line)
        return None


@dataclass(frozen=True, slots=True)
class Batch:
    """One gulp's worth of candidates plus the preamble metadata."""

    gulp: int | None
    utc_start: str | None    # PSRDADA UTC string, when the preamble carries it
    tsamp_s: float | None    # sample interval, when the preamble carries it
    cands: list[Candidate]


def parse_batch(payload: str) -> Batch:
    """Parse one gulp's worth of socket payload.

    The deployed hella preamble is ``<gulp> <utc_start> <x> <tsamp_us>``;
    older builds sent a bare ``<gulp>``. A missing or corrupt preamble
    degrades to (gulp=None) with every line treated as a candidate.
    """
    lines = payload.splitlines()
    if not lines:
        return Batch(None, None, None, [])

    gulp = utc_start = tsamp_s = None
    head = lines[0].split()
    body = lines
    try:
        if len(head) == 4:
            gulp, utc_start, _, tsamp_us = head
            gulp, tsamp_s = int(gulp), float(tsamp_us) * 1e-6
            body = lines[1:]
        elif len(head) == 1:
            gulp = int(head[0])
            body = lines[1:]
    except ValueError:
        gulp = utc_start = tsamp_s = None
        body = lines

    cands = [c for c in (parse_line(line) for line in body) if c is not None]
    return Batch(gulp, utc_start, tsamp_s, cands)
