"""Regression for the sky/sky_row name collision in T2Daemon._process.

Rebinding the per-gulp SkyTable to the per-cluster pointing dict made
``_classify`` hand that dict to ``_inj_beams`` from the second cluster of a gulp
on, which crashed on ``sky.weights_id`` whenever the cluster fell inside the
injection window and dropped the whole gulp through the caller's
``except Exception``.

The test drives two real clusters through ``_process`` with a real ``SkyTable``,
so ``_inj_beams`` takes the sky-neighbour branch, plus an injection-ledger entry
matching the second cluster.
"""
from __future__ import annotations

import asyncio

import pytest

from casm_t2 import cluster, db
from casm_t2.apps import t2d
from casm_t2.wire import Candidate


def make_daemon(tmp_path) -> t2d.T2Daemon:
    cfg = {
        "db": str(tmp_path / "t2.sqlite"),
        "weights_registry": str(tmp_path / "registry"),
        "trigger": {"fast_path": False},
    }
    return t2d.T2Daemon(cfg, shadow=True)


def make_cluster(beam: int, samp: int, dm: float, dm_idx: int,
                 snr: float = 13.0) -> cluster.Cluster:
    peak = Candidate(snr=snr, samp=samp, time_days=0.0, width=2,
                     dm_idx=dm_idx, dm=dm, beam=beam)
    return cluster.Cluster(peak=peak, n_members=1, n_beams=1,
                           beam_lo=beam, beam_hi=beam, dm_lo=dm, dm_hi=dm,
                           samp_lo=samp, samp_hi=samp)


def test_process_does_not_rebind_sky_table_to_pointing_dict(tmp_path):
    daemon = make_daemon(tmp_path)

    # A real pointing table so _inj_beams takes the sky-neighbour branch
    # (the bug never reproduced through the index-window fallback, sky=None).
    sky = cluster.SkyTable([45.0, 45.0], [10.0, 10.5], weights_id="wtest")
    daemon._sky_table = lambda key: sky
    # _sky is the per-cluster pointing lookup: a plain dict, never a
    # SkyTable. This is exactly the object the bug rebound "sky" to; if
    # _process rebinds it, the next cluster's _inj_beams call crashes on
    # dict.weights_id.
    daemon._sky = lambda event_utc, beam, radec=False, sun=False: {
        "weights_id": "wtest", "alt_deg": 45.0, "az_deg": 10.0}

    utc_start_s = "2026-09-10-00:00:00"
    utc_start = t2d.timing.parse_dada_utc(utc_start_s)
    event_epoch = t2d.timing.samp_to_utc(2000, utc_start).timestamp()
    # ledger entry the SECOND cluster (beam 1) must match within 60 s / dm tol
    daemon._inj_cache = [(event_epoch, 1, 50.0)]
    daemon._inj_cache_ts = t2d.time.monotonic()

    clusters = [
        make_cluster(beam=0, samp=1000, dm=80.0, dm_idx=200),
        make_cluster(beam=1, samp=2000, dm=50.0, dm_idx=150),
    ]

    asyncio.run(daemon._process((utc_start_s, 0), clusters,
                                n_jobs=1, n_cands=2, clustering_ms=1.0))

    rows = daemon.conn.execute(
        "SELECT beam, tags FROM clusters ORDER BY beam").fetchall()
    assert [r[0] for r in rows] == [0, 1]
    assert "injection" in rows[1][1]
