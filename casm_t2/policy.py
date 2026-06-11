"""Trigger budgets and disk safety.

Dumps are the only expensive, irreversible thing T2 does, and the disks are
nearly full (user directive: "just do a few and stop"). Every dump kind gets
a token-bucket policy — minimum spacing plus a hard daily cap — and every
trigger must pass a free-space check on the target filesystem. Refusals are
returned as reason strings so the caller can record them in the trigger
audit; nothing here raises on a refused trigger.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta


class TriggerBudget:
    """Per-dump-kind rate limiting: minimum spacing plus a daily cap."""

    def __init__(self, min_spacing_s: float, daily_max: int):
        self._min_spacing = timedelta(seconds=min_spacing_s)
        self._daily_max = daily_max
        self._last: datetime | None = None
        self._day: str | None = None
        self._count = 0

    def check(self, now: datetime) -> str | None:
        """Return a refusal reason, or None if a trigger is allowed."""
        if self._last is not None and now - self._last < self._min_spacing:
            return "spacing"
        if now.strftime("%Y-%m-%d") == self._day and self._count >= self._daily_max:
            return "daily_cap"
        return None

    def record(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._count = day, 0
        self._last = now
        self._count += 1


def disk_refusal(path: str, floor_gb: float) -> str | None:
    """Refusal reason if the filesystem holding path is too full to dump into.

    A missing path is itself a refusal (the dump daemon would write to a
    directory we cannot see — fail safe rather than dump blind).
    """
    try:
        st = os.statvfs(path)
    except OSError as exc:
        return f"disk_stat_failed:{exc}"
    free_gb = st.f_bavail * st.f_frsize / 1e9
    if free_gb < floor_gb:
        return f"disk_low:{free_gb:.0f}GB<{floor_gb:.0f}GB"
    return None
