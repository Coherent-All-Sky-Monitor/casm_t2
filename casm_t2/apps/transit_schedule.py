"""Generate a per-beam transit schedule CSV for a known source.

This wraps bf_weights_generator's plot_source_transit.py, which knows how to
turn a deployed beamforming weights file into per-beam transit windows. We
run it with --time-tz UTC and parse its stdout table into a CSV consumed by
t2-source-watch:

    beam,utc_start,utc_end
    3,2026-06-10T18:49:00+00:00,2026-06-10T19:29:00+00:00
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from datetime import datetime, timezone

PLOT_SOURCE_TRANSIT = "/home/casm/software/dev/bf_weights_generator/examples/plot_source_transit.py"

_ROW_RE = re.compile(
    r"beam_(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
)


def parse_transit_table(text: str) -> list[tuple[int, datetime, datetime]]:
    """Extract (beam, start, end) rows from plot_source_transit stdout (UTC)."""
    rows = []
    for m in _ROW_RE.finditer(text):
        beam = int(m.group(1))
        start = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        end = datetime.strptime(m.group(3), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        rows.append((beam, start, end))
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Write a source transit schedule CSV")
    p.add_argument("weights_h5", help="deployed beamforming weights file")
    p.add_argument("--source", default="b0329+54")
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument("--script", default=PLOT_SOURCE_TRANSIT)
    args = p.parse_args()

    result = subprocess.run(
        [sys.executable, args.script, args.weights_h5,
         "--sources", args.source, "--time-tz", "UTC"],
        capture_output=True, text=True, timeout=600,
    )
    rows = parse_transit_table(result.stdout)
    if not rows:
        sys.exit(f"no transit rows parsed; script output was:\n{result.stdout}\n{result.stderr}")

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["beam", "utc_start", "utc_end"])
        for beam, start, end in sorted(rows, key=lambda r: r[1]):
            writer.writerow([beam, start.isoformat(), end.isoformat()])
    print(f"wrote {len(rows)} transit windows to {args.out}")


if __name__ == "__main__":
    main()
