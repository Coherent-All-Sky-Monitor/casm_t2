"""Regression for the injection-ledger race that dumped two live injections.

The bot writes the ``injections`` row at FIFO write time, 16-24 s after the
pulse is already in the stream; t2d classifies the cluster ~47 s after the
pulse. With a 30 s cache the last refresh can predate the row, so the cluster
is classified blind (tier B, dm >= dm_floor), triggers a dump and posts a T3
card with no pulse in it (dumps tap upstream of the injection merge).

``_process`` / ``_fast_path`` now force a ledger re-read before any blind
(tier_A/tier_B) trigger.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from casm_t2 import cluster, timing
from casm_t2.apps import t2d
from casm_t2.wire import Candidate


def make_daemon(tmp_path) -> t2d.T2Daemon:
    cfg = {
        "db": str(tmp_path / "t2.sqlite"),
        "weights_registry": str(tmp_path / "registry"),
        "trigger": {"fast_path": False},
    }
    return t2d.T2Daemon(cfg, shadow=True)


def make_cluster(beam: int, samp: int, dm: float, snr: float = 25.0) -> cluster.Cluster:
    peak = Candidate(snr=snr, samp=samp, time_days=0.0, width=2,
                     dm_idx=200, dm=dm, beam=beam)
    return cluster.Cluster(peak=peak, n_members=1, n_beams=1,
                           beam_lo=beam, beam_hi=beam, dm_lo=dm, dm_hi=dm,
                           samp_lo=samp, samp_hi=samp)


def insert_injection(daemon: t2d.T2Daemon, epoch: float, beam: int,
                     dm: float) -> None:
    """One ledger row as the inject bot writes it at fire time."""
    iso = datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds")
    with daemon.conn:
        daemon.conn.execute(
            "INSERT INTO injections (inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc) VALUES (?,?,?,?,?,?,?,?)",
            (iso, beam // 64, beam, dm, 1.0, 1.0, "test", iso))


def prime_cache(daemon: t2d.T2Daemon) -> None:
    """Refresh once with an empty ledger, i.e. the stale-cache state."""
    daemon._refresh_injections()
    assert daemon._inj_cache == []


# The ledger query only looks back 15 minutes, so the gulp has to sit near
# wall-clock now.
UTC_START_S = timing.format_dada_utc(
    datetime.now(timezone.utc) - timedelta(seconds=60))


def run_process(daemon: t2d.T2Daemon, clusters: list[cluster.Cluster]) -> None:
    asyncio.run(daemon._process((UTC_START_S, 0), clusters, n_jobs=1,
                                n_cands=len(clusters), clustering_ms=1.0))


def event_epoch(samp: int) -> float:
    return timing.samp_to_utc(samp, timing.parse_dada_utc(UTC_START_S)).timestamp()


def test_row_written_after_last_refresh_is_still_tagged(tmp_path):
    daemon = make_daemon(tmp_path)
    prime_cache(daemon)
    # Row lands 22 s AFTER the pulse, and after the cache refresh above: the
    # live failure mode (ledger ids 740/741).
    insert_injection(daemon, event_epoch(1000) + 22.0, beam=10, dm=100.0)

    run_process(daemon, [make_cluster(beam=10, samp=1000, dm=100.0)])

    tags = daemon.conn.execute("SELECT tags FROM clusters").fetchone()[0]
    assert "injection" in tags
    assert daemon.conn.execute("SELECT count(*) FROM triggers").fetchone()[0] == 0


def test_no_matching_row_still_triggers(tmp_path):
    daemon = make_daemon(tmp_path)
    prime_cache(daemon)

    run_process(daemon, [make_cluster(beam=10, samp=1000, dm=100.0)])

    tags = daemon.conn.execute("SELECT tags FROM clusters").fetchone()[0]
    assert "injection" not in tags
    rows = daemon.conn.execute("SELECT detail FROM triggers").fetchall()
    assert len(rows) == 1 and "tier_" in rows[0][0]


def test_forced_refresh_only_for_blind_trigger_candidates(tmp_path):
    daemon = make_daemon(tmp_path)
    prime_cache(daemon)
    forced = []
    real = daemon._refresh_injections
    daemon._refresh_injections = lambda force=False: (
        forced.append(force), real(force))[1]

    # Below tier B (snr 13) and below dm_floor (dm 5): neither can trigger.
    run_process(daemon, [make_cluster(beam=10, samp=1000, dm=100.0, snr=13.0),
                         make_cluster(beam=11, samp=2000, dm=5.0, snr=25.0)])
    assert forced == [False]  # the per-gulp refresh only

    forced.clear()
    run_process(daemon, [make_cluster(beam=12, samp=3000, dm=100.0, snr=25.0)])
    assert forced == [False, True]


def test_fast_path_rechecks_before_triggering(tmp_path):
    daemon = make_daemon(tmp_path)
    prime_cache(daemon)
    triggered = []

    async def fake_trigger(*a, **kw):
        triggered.append(a)

    daemon._trigger = fake_trigger

    cand = Candidate(snr=25.0, samp=1000, time_days=0.0, width=2, dm_idx=200,
                     dm=100.0, beam=10)
    batch = t2d.wire.Batch(gulp=0, utc_start=UTC_START_S, cands=[cand],
                           tsamp_s=timing.TSAMP_S)
    insert_injection(daemon, event_epoch(1000) + 22.0, beam=10, dm=100.0)

    asyncio.run(daemon._fast_path(batch))
    assert triggered == []

    # Same batch, ledger cleared (force past the TTL so the pre-filter also
    # sees the empty ledger): the fast trigger goes ahead.
    with daemon.conn:
        daemon.conn.execute("DELETE FROM injections")
    daemon._refresh_injections(force=True)
    asyncio.run(daemon._fast_path(batch))
    assert len(triggered) == 1
