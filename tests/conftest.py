"""Shared fixtures: in-memory event DB and candidate/cluster builders.

Nothing here opens a socket, sleeps, or touches the real database.
"""

import asyncio
import math
import random
from datetime import datetime, timezone

import pytest

from casm_t2 import db, timing
from casm_t2.cluster import Cluster, SkyTable
from casm_t2.wire import Candidate

SKY_ROWS, SKY_COLS, SKY_SPACING_DEG = 16, 32, 3.1


def _synthetic_pointings(seed: int = 0) -> tuple[dict, dict]:
    """A 512-beam pointing table whose index order is NOT sky order.

    Beams sit on a 16 x 32 grid of 3.1 deg spacing about the zenith, the real
    grid's spacing, but the grid cell each beam index lands on is a
    seeded shuffle, so index adjacency says nothing about sky adjacency.
    That is the property the deployed grid has (consecutive indices a median
    16 deg apart) and the one every sky-clustering test needs.

    Returns (pointings dict as weights_registry.pointings_for gives it,
    {(row, col): beam}).
    """
    cells = [(r, c) for r in range(SKY_ROWS) for c in range(SKY_COLS)]
    order = list(range(len(cells)))
    random.Random(seed).shuffle(order)
    alt = [0.0] * len(order)
    az = [0.0] * len(order)
    beam_at: dict[tuple[int, int], int] = {}
    for beam, cell_i in enumerate(order):
        row, col = cells[cell_i]
        x = (col - (SKY_COLS - 1) / 2) * SKY_SPACING_DEG
        y = (row - (SKY_ROWS - 1) / 2) * SKY_SPACING_DEG
        alt[beam] = 90.0 - math.hypot(x, y)
        az[beam] = math.degrees(math.atan2(x, y)) % 360.0
        beam_at[(row, col)] = beam
    return ({"weights_id": "synthetic", "alt_deg": alt, "az_deg": az}, beam_at)


@pytest.fixture
def pointings():
    """The synthetic 512-beam alt/az table, as the registry would return it."""
    return _synthetic_pointings()[0]


@pytest.fixture
def sky():
    """(SkyTable, {(row, col): beam}) for the synthetic grid."""
    p, beam_at = _synthetic_pointings()
    return SkyTable.from_pointings(p), beam_at


@pytest.fixture
def conn():
    """A fresh in-memory event DB with the current schema."""
    c = db.connect(":memory:")
    yield c
    c.close()


def _make_cand(snr=20.0, beam=5, width=3, samp=1000, dm=250.0, dm_idx=100):
    return Candidate(snr=snr, samp=samp, time_days=0.0, width=width,
                     dm_idx=dm_idx, dm=dm, beam=beam)


def _make_cluster(snr=20.0, beam=5):
    c = _make_cand(snr=snr, beam=beam)
    return Cluster(peak=c, n_members=4, n_beams=1, beam_lo=beam, beam_hi=beam,
                   dm_lo=c.dm - 1, dm_hi=c.dm + 1,
                   samp_lo=c.samp - 10, samp_hi=c.samp + 10)


def _cluster_row(name, snr=20.0, beam=5, gulp=1):
    """One row in the tuple shape db.insert_clusters expects."""
    return (_make_cluster(snr=snr, beam=beam), "2026-07-31-00:00:00", gulp,
            "2026-07-31T00:00:00.000+00:00", "B", "", name)


@pytest.fixture
def make_cand():
    return _make_cand


@pytest.fixture
def make_cluster():
    return _make_cluster


@pytest.fixture
def cluster_row():
    return _cluster_row


@pytest.fixture
def daemon(tmp_path):
    """Factory for a T2Daemon on a throwaway DB, with dumps suppressed.

    `dumps_enabled: false` keeps _trigger off the network while still
    recording the decision, so trigger rows are the assertion surface.
    """
    from casm_t2.apps import t2d

    made = []

    def _make(**overrides):
        # coalesce_jobs 1: the `ingest` fixture drives every batch through
        # job 0 and awaits the flush itself, so "all expected jobs reported"
        # is true as soon as the first batch lands and the flush is immediate
        # (coalesce_s 0). Tests that exercise the coalescer set their own.
        cfg = {"db": str(tmp_path / f"t2_{len(made)}.sqlite"),
               "coalesce_s": 0.0, "coalesce_jobs": 1, "dumps_enabled": False,
               # never read the production weights registry from a test;
               # tests that want pointings monkeypatch registry.pointings_for
               "weights_registry": str(tmp_path / "registry"),
               "tiers": {"A": 30.0, "B": 18.0, "C": 12.0},
               "trigger": {"fast_path": False}, "known_sources": []}
        cfg.update(overrides)
        d = t2d.T2Daemon(cfg, shadow=False)
        made.append(d)
        return d

    yield _make
    for d in made:
        d.conn.close()


class FakeReader:
    """asyncio.StreamReader stand-in that yields one canned payload."""

    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self, _n: int = -1) -> bytes:
        return self._payload


class FakeWriter:
    def close(self) -> None:
        pass


def recent_utc_start() -> str:
    """A DADA UTC string anchored to now.

    The fast/slow reconciliation in _process drops pending_fast entries more
    than 120 s old, so a hard-coded date would make every fast trigger look
    stale and fire twice. Production gulps are always seconds old.
    """
    return timing.format_dada_utc(datetime.now(timezone.utc))


def make_payload(cands, utc_start=None, gulp=3):
    """Wire-format batch: preamble line then one line per candidate."""
    utc_start = utc_start or recent_utc_start()
    lines = [f"{gulp} {utc_start} 0 1048.576"]
    lines += [f"{c.snr} {c.samp} 0.0 {c.width} {c.dm_idx} {c.dm} {c.beam}"
              for c in cands]
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def ingest():
    """Drive batches through _handle, then run everything they spawned.

    Batches land under one coalescer key before the flush runs, which is how
    the eight hella jobs actually arrive. Tasks are collected rather than
    scheduled so the whole thing is deterministic and needs no sleeps.
    """
    def _ingest(daemon_, *batches, utc_start=None, gulp=3):
        utc_start = utc_start or recent_utc_start()

        async def run():
            spawned = []
            daemon_._spawn = spawned.append
            for cands in batches:
                await daemon_._handle(
                    FakeReader(make_payload(cands, utc_start, gulp)),
                    FakeWriter(), job=0)
            for coro in spawned:      # grows as tasks spawn more work
                await coro
        asyncio.run(run())
    return _ingest

