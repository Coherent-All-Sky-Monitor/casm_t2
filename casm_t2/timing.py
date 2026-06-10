"""Time conversions between hella sample numbers and absolute UTC.

The native CASM sample interval is 32.768 us x 32 = 1.048576 ms. Note that
hella's own ``time_days`` output column has historically been computed with
an assumed 1.0 ms sample time in some builds, so all absolute timing here
is derived from the integer sample number and TSAMP_S only.

The dump daemons (and PSRDADA headers generally) use UTC strings of the
form ``2026-06-09-01:24:11`` with optional fractional seconds.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 32.768 us x 32. Verified 2026-06-10 against wall clock over a 40 h run.
TSAMP_S = 1.048576e-3

# Band edges for the 3072-channel beam data, descending frequency order.
F_TOP_MHZ = 484.375
CHAN_WIDTH_MHZ = 0.030517578125
NCHAN = 3072
F_BOTTOM_MHZ = F_TOP_MHZ - (NCHAN - 1) * CHAN_WIDTH_MHZ

_DADA_UTC_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2})(\.\d+)?")


def parse_dada_utc(s: str) -> datetime:
    """Parse a PSRDADA-style UTC string (``%Y-%m-%d-%H:%M:%S[.frac]``)."""
    m = _DADA_UTC_RE.fullmatch(s.strip())
    if m is None:
        raise ValueError(f"not a PSRDADA UTC string: {s!r}")
    dt = datetime.strptime(m.group(1), "%Y-%m-%d-%H:%M:%S").replace(tzinfo=timezone.utc)
    if m.group(2):
        dt += timedelta(seconds=float(m.group(2)))
    return dt


def format_dada_utc(dt: datetime) -> str:
    """Format UTC with millisecond precision, as accepted by the dump daemons."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d-%H:%M:%S.%f")[:-3]


def utc_start_from_cands_path(path: str | Path) -> datetime:
    """Extract the observation UTC_START from a hella output filename.

    Hella candidate files are named ``cands_<UTC_START>.dat.<job>``, with
    UTC_START in PSRDADA format.
    """
    m = _DADA_UTC_RE.search(Path(path).name)
    if m is None:
        raise ValueError(f"no UTC_START in filename: {path}")
    return parse_dada_utc(m.group(0))


def samp_to_utc(samp: int, utc_start: datetime, tsamp_s: float = TSAMP_S) -> datetime:
    """Absolute UTC of a sample number, referenced to the top of the band."""
    return utc_start + timedelta(seconds=samp * tsamp_s)


def dispersion_sweep_s(dm: float, f_top_mhz: float = F_TOP_MHZ, f_bottom_mhz: float = F_BOTTOM_MHZ) -> float:
    """Dispersion delay across the band in seconds for a given DM."""
    return 4.148808e3 * dm * (f_bottom_mhz**-2 - f_top_mhz**-2)
