"""Known-source matching for clustered events.

A cluster matches a known source when its peak beam is inside one of the
source's transit windows AND the cluster's DM envelope [dm_lo, dm_hi]
overlaps the source DM window. Peak DM is deliberately not used: bright
pulses merge with their own broadband/sidelobe response, dragging the
cluster's peak-SNR trial toward the low-DM search floor while the envelope
still covers the true DM (B0329 storms, 2026-06-10).

Transit schedules come from t2-transit-schedule CSVs (beam, utc_start,
utc_end), same format the Phase-0 watcher used.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from casm_t2.cluster import Cluster

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TransitWindow:
    beam: int
    start: datetime
    end: datetime


class KnownSource:
    """One watched source: transit schedule plus a DM window."""

    def __init__(self, name: str, dm_min: float, dm_max: float,
                 schedule_csv: str | Path, pad_s: float = 120.0):
        self.name = name
        self.dm_min = dm_min
        self.dm_max = dm_max
        self._pad = timedelta(seconds=pad_s)
        self._by_beam: dict[int, list[TransitWindow]] = {}
        with open(schedule_csv, newline="") as f:
            for row in csv.DictReader(f):
                w = TransitWindow(int(row["beam"]),
                                  datetime.fromisoformat(row["utc_start"]),
                                  datetime.fromisoformat(row["utc_end"]))
                self._by_beam.setdefault(w.beam, []).append(w)
        logger.info("source %s: %d transit windows, DM %.1f-%.1f",
                    name, sum(len(v) for v in self._by_beam.values()), dm_min, dm_max)

    def matches(self, cl: Cluster, event_utc: datetime) -> bool:
        if cl.dm_hi < self.dm_min or cl.dm_lo > self.dm_max:
            return False
        return any(w.start - self._pad <= event_utc <= w.end + self._pad
                   for w in self._by_beam.get(cl.peak.beam, ()))


def load_sources(cfg_blocks: list[dict]) -> list[KnownSource]:
    """Build KnownSource objects from t2d.yaml known_sources blocks."""
    out = []
    for b in cfg_blocks or []:
        try:
            out.append(KnownSource(b["name"], b["dm_min"], b["dm_max"],
                                   b["schedule_csv"], b.get("schedule_pad_s", 120.0)))
        except (KeyError, OSError) as exc:
            logger.error("known source %r not loaded: %s", b.get("name"), exc)
    return out
