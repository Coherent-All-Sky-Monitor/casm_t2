"""Clustering on sky position instead of beam index (2026-09-09).

The deployed 512-beam grid is not sky-ordered, so the old ``beam`` axis
grouped trials that share nothing and separated trials from the same patch
of sky. These tests pin the replacement: a tangent-plane (x, y) pair in
degrees, a real ``sky_extent_deg`` on every cluster, and the beam-index
fallback for when no pointing table exists.
"""
from __future__ import annotations

import math

import pytest

from casm_t2 import cluster, db
from casm_t2.cluster import ClusterParams, SkyTable, cluster_candidates, unproject
from casm_t2.wire import Candidate

from conftest import SKY_COLS, _synthetic_pointings

PARAMS = ClusterParams()


def trials(beams, samp=1000, dm_idxs=(100, 116), widths=(2, 3), snr=20.0):
    """Four trials per beam: two DM cells x two boxcar widths.

    Enough to make every trial a DBSCAN core point *only* when its beam has
    a neighbour within one sky scale — which is exactly the merge/split
    behaviour under test. Fewer trials per beam and nothing is ever core;
    more and single beams cluster on their own.
    """
    out = []
    for b in beams:
        for di in dm_idxs:
            for w in widths:
                out.append(Candidate(snr=snr, samp=samp, time_days=0.0, width=w,
                                     dm_idx=di, dm=20.5 + di * 0.4, beam=b))
    return out


# ------------------------------------------------------------- projection

def test_projection_round_trip(sky):
    """Every beam's (x, y) unprojects back to the alt/az it came from."""
    table, _ = sky
    for beam in range(0, table.n, 7):
        alt, az = table.altaz(beam)
        x, y = table.xy(beam)
        alt_r, az_r = unproject(x, y)
        assert alt_r == pytest.approx(alt, abs=1e-6)
        assert (az_r - az + 180) % 360 - 180 == pytest.approx(0.0, abs=1e-6)


def test_projection_is_local_distance(sky):
    """Near the zenith the tangent-plane distance is the real angle."""
    table, beam_at = sky
    mid = beam_at[(8, 16)], beam_at[(8, 17)]
    dx = abs(table.x[mid[0]] - table.x[mid[1]])
    dy = abs(table.y[mid[0]] - table.y[mid[1]])
    assert math.hypot(dx, dy) == pytest.approx(
        table.separation_deg(*mid), rel=0.05)


def test_separation_and_extent(sky):
    table, beam_at = sky
    one = beam_at[(0, 0)]
    assert table.max_separation_deg([one]) == 0.0
    row = [beam_at[(0, c)] for c in range(SKY_COLS)]
    # the whole row spans far more than any real source could
    assert table.max_separation_deg(row) > 60.0


def test_bad_pointings_give_no_table():
    assert SkyTable.from_pointings(None) is None
    assert SkyTable.from_pointings({}) is None
    assert SkyTable.from_pointings({"alt_deg": [1.0, 2.0], "az_deg": [1.0]}) is None


# ---------------------------------------------------- merging and splitting

def _pick_sky_adjacent_far_in_index(table, beam_at):
    """Two beams 3.1 deg apart on sky whose indices are nowhere near."""
    pairs = [(beam_at[(0, c)], beam_at[(0, c + 1)]) for c in range(SKY_COLS - 1)]
    a, b = max(pairs, key=lambda p: abs(p[0] - p[1]))
    assert abs(a - b) > 4 and table.separation_deg(a, b) < 4.0
    return a, b


def _pick_index_adjacent_far_on_sky(table):
    """Two consecutive beam indices that are far apart on the sky."""
    b = max(range(table.n - 1), key=lambda i: table.separation_deg(i, i + 1))
    assert table.separation_deg(b, b + 1) > 25.0
    return b, b + 1


def test_sky_adjacent_beams_merge(sky):
    """Adjacent on sky, 372 apart in index: one cluster, not two."""
    table, beam_at = sky
    a, b = _pick_sky_adjacent_far_in_index(table, beam_at)
    cls = cluster_candidates(trials([a, b]), PARAMS, table)
    real = [c for c in cls if c.n_beams > 1]
    assert len(real) == 1
    assert real[0].n_members == 8
    assert real[0].sky_extent_deg == pytest.approx(table.separation_deg(a, b),
                                                   abs=0.01)
    # the old beam-index axis fragments the same trials into singletons
    old = cluster_candidates(trials([a, b]), PARAMS, None)
    assert all(c.n_beams == 1 for c in old)


def test_index_adjacent_but_sky_far_do_not_merge(sky):
    """Consecutive indices 100 deg apart on sky stay separate."""
    table, _ = sky
    a, b = _pick_index_adjacent_far_on_sky(table)
    cls = cluster_candidates(trials([a, b]), PARAMS, table)
    assert all(c.n_beams == 1 for c in cls)
    assert all(c.sky_extent_deg == 0.0 for c in cls)
    # the old beam-index axis merged them, which is the bug
    old = cluster_candidates(trials([a, b]), PARAMS, None)
    assert any(c.n_beams == 2 for c in old)


# ------------------------------------------------------------- rfi_wide

def _snake(beam_at, n):
    """A chain of n sky-adjacent beams: row 0 left to right, then row 1 back."""
    cells = [(0, c) for c in range(SKY_COLS)]
    cells += [(1, c) for c in range(SKY_COLS - 1, -1, -1)]
    return [beam_at[c] for c in cells[:n]]


def test_broadband_forty_beams_is_one_wide_cluster(sky, daemon):
    """40 beams across the sky: one cluster, huge extent, tagged rfi_wide."""
    table, beam_at = sky
    beams = _snake(beam_at, 40)
    cls = cluster_candidates(trials(beams), PARAMS, table)
    wide = max(cls, key=lambda c: c.n_beams)
    assert wide.n_beams == 40
    assert wide.n_members == 160
    assert wide.sky_extent_deg > 60.0
    d = daemon()
    tier, tags = d._classify(wide, None)
    assert "rfi_wide" in tags
    assert d._wants_trigger(wide, tier, tags) is None


def test_sky_extent_catches_what_max_nbeam_misses(sky, daemon):
    """12 beams over 33 deg: under max_nbeam 32, over max_sky_extent_deg 25.

    This is the class the old cut could not see — a burst lighting up a
    dozen beams spread right across the sky counts as 12 beams and sailed
    through ``n_beams > 32``.
    """
    table, beam_at = sky
    beams = [beam_at[(0, c)] for c in range(10, 22)]
    cls = cluster_candidates(trials(beams), PARAMS, table)
    wide = max(cls, key=lambda c: c.n_beams)
    assert wide.n_beams == 12
    assert 25.0 < wide.sky_extent_deg < 60.0

    d = daemon()
    assert wide.n_beams <= d.max_nbeam            # the old cut does not fire
    _, tags = d._classify(wide, None)
    assert "rfi_wide" in tags

    off = daemon(filters={"max_sky_extent_deg": 0.0})
    _, tags_off = off._classify(wide, None)
    assert "rfi_wide" not in tags_off


def test_compact_source_is_not_tagged(sky, daemon):
    """Two sky-adjacent beams — a real source's footprint — stay clean."""
    table, beam_at = sky
    a, b = _pick_sky_adjacent_far_in_index(table, beam_at)
    cls = cluster_candidates(trials([a, b], dm_idxs=(700, 716)), PARAMS, table)
    src = max(cls, key=lambda c: c.n_beams)
    assert src.sky_extent_deg < 8.0
    d = daemon()
    tier, tags = d._classify(src, None)
    assert "rfi_wide" not in tags
    assert d._wants_trigger(src, tier, tags) == "tier_B"


# -------------------------------------------------------------- fallback

def test_fallback_without_pointings(sky, caplog):
    """No table: beam-index axis, extent left unmeasured, warned once."""
    cluster._warned_no_pointings = False
    with caplog.at_level("WARNING", logger="casm_t2.cluster"):
        cls = cluster_candidates(trials([10, 11]), PARAMS, None)
        cluster_candidates(trials([10, 11]), PARAMS, None)
    assert all(c.sky_extent_deg == 0.0 for c in cls)
    warnings = [r for r in caplog.records if "pointing table" in r.message]
    assert len(warnings) == 1


def test_daemon_sky_table_absent_registry(daemon, tmp_path):
    """An empty weights registry degrades to the fallback, it does not raise."""
    d = daemon(weights_registry=str(tmp_path / "no_registry"))
    assert d._sky_table(("2026-09-09-21:12:15", 5)) is None
    assert d._sky_table((None, 5)) is None


def test_daemon_sky_table_cached(daemon, tmp_path, pointings, monkeypatch):
    """One registry lookup per gulp; the built table is reused by weights id."""
    d = daemon()
    calls = []

    def fake(utc):
        calls.append(utc)
        return pointings

    monkeypatch.setattr(d.registry, "pointings_for", fake)
    t1 = d._sky_table(("2026-09-09-21:12:15", 5))
    t2 = d._sky_table(("2026-09-09-21:12:15", 6))
    assert t1 is t2 is not None
    assert t1.weights_id == "synthetic"
    assert len(calls) == 2          # looked up per gulp
    assert len(d._sky_tables) == 1  # but built once


def test_daemon_clusters_on_sky(daemon, ingest, pointings, monkeypatch, sky):
    """End to end: a gulp clusters on sky and stores the extent."""
    table, beam_at = sky
    d = daemon(veto_widths=[], occupancy={"min_beams": 0},
               filters={"beam_veto": [], "max_nbeam": 32,
                        "max_sky_extent_deg": 25.0})
    monkeypatch.setattr(d.registry, "pointings_for", lambda utc: pointings)
    a, b = _pick_sky_adjacent_far_in_index(table, beam_at)
    ingest(d, trials([a, b], snr=25.0))
    rows = d.conn.execute("SELECT n_beams, sky_extent_deg, tags FROM clusters"
                          " ORDER BY snr DESC").fetchall()
    assert rows
    assert rows[0][0] == 2
    assert rows[0][1] == pytest.approx(table.separation_deg(a, b), abs=0.01)
    assert "rfi_wide" not in rows[0][2]


# ------------------------------------------------------------- migration

OLD_CLUSTERS = """
CREATE TABLE clusters (
    id INTEGER PRIMARY KEY, obs_utc_start TEXT NOT NULL, gulp INTEGER,
    event_utc TEXT NOT NULL, samp INTEGER NOT NULL, snr REAL NOT NULL,
    dm REAL NOT NULL, dm_idx INTEGER NOT NULL, width INTEGER NOT NULL,
    beam INTEGER NOT NULL, n_members INTEGER NOT NULL, n_beams INTEGER NOT NULL,
    beam_lo INTEGER NOT NULL, beam_hi INTEGER NOT NULL, dm_lo REAL NOT NULL,
    dm_hi REAL NOT NULL, samp_lo INTEGER NOT NULL, samp_hi INTEGER NOT NULL,
    tier TEXT NOT NULL, tags TEXT NOT NULL, name TEXT, created_utc TEXT NOT NULL
);
CREATE TABLE gulp_stats (
    id INTEGER PRIMARY KEY, obs_utc_start TEXT NOT NULL, gulp INTEGER,
    gulp_utc TEXT NOT NULL, n_jobs INTEGER NOT NULL, n_cands INTEGER NOT NULL,
    n_clusters INTEGER NOT NULL, n_stored INTEGER NOT NULL,
    n_would INTEGER NOT NULL, clustering_ms REAL NOT NULL,
    created_utc TEXT NOT NULL
);
"""


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_migration_adds_columns_once(tmp_path, cluster_row):
    """An old DB gains sky_extent_deg / coalesce_wait_ms / skipped, and
    connecting again neither duplicates them nor raises."""
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(OLD_CLUSTERS)
    old.commit()
    old.close()

    for _ in range(3):
        conn = db.connect(path)
        ccols, gcols = _cols(conn, "clusters"), _cols(conn, "gulp_stats")
        assert ccols.count("sky_extent_deg") == 1
        assert gcols.count("coalesce_wait_ms") == 1
        assert gcols.count("skipped") == 1
        conn.close()

    conn = db.connect(path)
    db.insert_clusters(conn, [cluster_row("260909aaaaaa")])
    db.insert_gulp_stats(conn, "2026-09-09-21:12:15", 1, "", 8, 10, 1, 1, 0,
                         1.0, coalesce_wait_ms=1234.5)
    db.insert_gulp_stats(conn, "2026-09-09-21:12:15", 2, "", 5, 30, 0, 0, 0,
                         0.0, coalesce_wait_ms=8000.0, skipped=1)
    assert conn.execute("SELECT sky_extent_deg FROM clusters").fetchone()[0] == 0.0
    assert conn.execute(
        "SELECT coalesce_wait_ms FROM gulp_stats").fetchone()[0] == 1234.5
    assert [r[0] for r in conn.execute(
        "SELECT skipped FROM gulp_stats ORDER BY id")] == [0, 1]
    conn.close()


def test_synthetic_grid_is_not_sky_ordered():
    """The fixture reproduces the property that motivated the change."""
    p, _ = _synthetic_pointings()
    table = SkyTable.from_pointings(p)
    seps = [table.separation_deg(b, b + 1) for b in range(table.n - 1)]
    seps.sort()
    assert seps[len(seps) // 2] > 10.0    # median consecutive-index separation
