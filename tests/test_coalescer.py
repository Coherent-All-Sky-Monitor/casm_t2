"""Gulp coalescing: wait for all eight jobs, not for a fixed hold.

The hella jobs finish seconds apart, so a fixed sleep after the first batch
fragments a gulp and each per-gulp veto sees only its own fragment. These tests
pin the rule: flush when all expected jobs have reported, or at coalesce_max_s,
whichever comes first.

Timings are compressed to tenths of a second, with arrivals spread far wider
than ``coalesce_s``, which is the shape that fragments.
"""
from __future__ import annotations

import asyncio

from conftest import FakeReader, FakeWriter, make_payload, recent_utc_start
from casm_t2.wire import Candidate

GULP = 1218


def job_cands(job: int, n: int = 6) -> list[Candidate]:
    """A few trials in one job's own beams, distinct enough to survive dedup."""
    return [Candidate(snr=13.0 + i, samp=1000 + 8192 * GULP, time_days=0.0,
                      width=2 + (i % 2), dm_idx=300 + 20 * i,
                      dm=140.0 + 8 * i, beam=job * 64 + i)
            for i in range(n)]


def drive(d, jobs, gap_s, utc_start=None, pause_s=0.0):
    """Feed one batch per job, gap_s apart, then let every spawned task finish."""
    utc_start = utc_start or recent_utc_start()

    async def run():
        for job in jobs:
            await d._handle(
                FakeReader(make_payload(job_cands(job), utc_start, GULP)),
                FakeWriter(), job=job)
            await asyncio.sleep(gap_s)
        if pause_s:
            await asyncio.sleep(pause_s)
        while d.tasks:
            await asyncio.gather(*list(d.tasks))
    asyncio.run(run())
    return utc_start


def gulp_rows(d):
    return d.conn.execute(
        "SELECT gulp, n_jobs, n_cands, coalesce_wait_ms, skipped, n_clusters"
        " FROM gulp_stats ORDER BY id").fetchall()


def n_clusters(d):
    return d.conn.execute("SELECT count(*) FROM clusters").fetchone()[0]


def n_triggers(d):
    return d.conn.execute("SELECT count(*) FROM triggers").fetchone()[0]


def test_eight_jobs_spread_out_flush_once(daemon):
    """Arrivals spread over 10x coalesce_s still make one whole gulp."""
    d = daemon(coalesce_s=0.05, coalesce_jobs=8, coalesce_max_s=5.0)
    drive(d, range(8), gap_s=0.05)
    rows = gulp_rows(d)
    assert len(rows) == 1
    gulp, n_jobs, n_cands, wait_ms, skipped, _ = rows[0]
    assert (gulp, n_jobs, n_cands, skipped) == (GULP, 8, 48, 0)
    # flushed on job completion, nowhere near the 5 s cap
    assert 0.05e3 <= wait_ms < 5.0e3
    assert d.n_late_batches == 0
    assert d.n_skipped_gulps == 0


def test_missing_jobs_skip_the_gulp(daemon, caplog):
    """Five jobs then silence: the gulp is DROPPED, not clustered on part of
    the sky. The row exists so the gap stays visible in the duty cycle."""
    d = daemon(coalesce_s=0.05, coalesce_jobs=8, coalesce_max_s=0.4)
    with caplog.at_level("WARNING", logger="t2d"):
        drive(d, range(5), gap_s=0.02)

    rows = gulp_rows(d)
    assert len(rows) == 1
    gulp, n_jobs, n_cands, wait_ms, skipped, n_cl = rows[0]
    assert (gulp, n_jobs, n_cands) == (GULP, 5, 30)
    assert skipped == 1
    assert n_cl == 0                       # never clustered
    assert wait_ms >= 400.0
    assert d.n_skipped_gulps == 1
    # nothing downstream ran
    assert n_clusters(d) == 0
    assert n_triggers(d) == 0
    assert any("skipped: only 5/8 jobs" in r.getMessage()
               for r in caplog.records)


def test_complete_gulp_at_max_wait_is_not_skipped(daemon):
    """All jobs in, but trickling: reaching coalesce_max_s is not a skip."""
    d = daemon(coalesce_s=5.0, coalesce_jobs=4, coalesce_max_s=0.4)
    drive(d, range(4), gap_s=0.02)
    rows = gulp_rows(d)
    assert len(rows) == 1
    assert rows[0][1] == 4                 # n_jobs
    assert rows[0][4] == 0                 # not skipped
    assert rows[0][3] >= 400.0             # it did hit the maximum wait
    assert d.n_skipped_gulps == 0


def test_late_batch_after_flush_warns_and_forms_its_own_fragment(daemon, caplog):
    """A ninth-or-later batch is still processed, but it is now counted."""
    d = daemon(coalesce_s=0.0, coalesce_jobs=1, coalesce_max_s=5.0)
    utc_start = recent_utc_start()

    async def run():
        for job in (0, 1):
            await d._handle(
                FakeReader(make_payload(job_cands(job), utc_start, GULP)),
                FakeWriter(), job=job)
            while d.tasks:
                await asyncio.gather(*list(d.tasks))

    with caplog.at_level("WARNING", logger="t2d"):
        asyncio.run(run())

    rows = gulp_rows(d)
    assert len(rows) == 2                      # two fragments, as before
    assert [r[1] for r in rows] == [1, 1]
    assert [r[4] for r in rows] == [0, 0]      # both complete, neither skipped
    assert d.n_late_batches == 1
    assert any("late batch for gulp" in r.getMessage() for r in caplog.records)


def test_quiet_hold_absorbs_a_burst_after_the_last_job(daemon):
    """coalesce_s is measured from the LAST batch, so a trailing burst lands."""
    d = daemon(coalesce_s=0.3, coalesce_jobs=8, coalesce_max_s=5.0)
    # eight jobs arriving 0.1 s apart: each one restarts the 0.3 s quiet hold
    drive(d, range(8), gap_s=0.1)
    rows = gulp_rows(d)
    assert len(rows) == 1
    assert rows[0][1] == 8
    assert rows[0][4] == 0
    assert rows[0][3] >= 300.0                 # held 0.3 s past the last batch
    assert d.n_late_batches == 0


def test_coalesce_jobs_defaults_to_port_count(tmp_path):
    """Built straight from a config, so the fixture's overrides do not apply."""
    from casm_t2.apps import t2d

    base = {"db": str(tmp_path / "d.sqlite"), "dumps_enabled": False,
            "known_sources": [], "weights_registry": str(tmp_path / "reg")}
    d = t2d.T2Daemon(dict(base, ports=[1, 2, 3, 4]), shadow=False)
    assert d.coalesce_jobs == 4
    assert (d.coalesce_s, d.coalesce_max_s) == (0.25, 8.0)
    d.conn.close()

    d = t2d.T2Daemon(base, shadow=False)       # no ports key: the eight live ones
    assert d.coalesce_jobs == 8
    d.conn.close()


def test_empty_gulp_still_records_the_wait(daemon):
    """An all-vetoed gulp gets its row, with the coalescer wait on it."""
    d = daemon(coalesce_s=0.0, coalesce_jobs=2, coalesce_max_s=0.3,
               veto_widths=[2, 3])
    drive(d, range(2), gap_s=0.01)
    rows = gulp_rows(d)
    assert len(rows) == 1
    assert rows[0][1] == 2
    assert rows[0][2] == 12                    # n_cands is the veto count
    assert rows[0][3] >= 0.0
    assert rows[0][4] == 0                     # complete, just empty
