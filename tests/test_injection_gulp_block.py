"""Injection gulp blocks: the card's gulp block, written per injection gulp.

t3-replay-injection draws the v3 bottom row from them. t2d writes one file per
injection cluster to ``injection.gulp_dir`` after the gulp's decisions, off the
event loop, and nothing about it may change a decision or raise.
"""

import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

from casm_t2 import gulp_card, timing
from casm_t2.apps import t2d

from test_card_gulp_block import arm, cand

GULP = 7
G_LO = GULP * 8192 - 960
G_HI = GULP * 8192 + 7231
T0 = G_LO + 4000            # the injected event's samples
BEAM, DM_IDX = 100, 590     # DM 300


def utc_start_for_recent_event() -> str:
    """A UTC_START that puts sample T0 60 s before now: the ledger query only
    looks back 15 minutes."""
    return timing.format_dada_utc(datetime.now(timezone.utc)
                                  - timedelta(seconds=60 + T0 * timing.TSAMP_S))


def gulp_cands(seed=0, second=False):
    """An injection-like event at T0 in beam 100, noise, and optionally a
    real triggerable event 3 s later in beam 300."""
    rng = random.Random(seed)
    out = [cand(25.0 - 0.1 * k, T0 + rng.randint(-8, 8), BEAM + (k % 2),
                DM_IDX + rng.randint(-10, 10), 2 + k % 3) for k in range(60)]
    if second:
        out += [cand(21.0 - 0.1 * k, T0 + 3000 + rng.randint(-8, 8), 300,
                     800 + rng.randint(-10, 10), 2 + k % 2) for k in range(40)]
    out += [cand(rng.uniform(8.0, 11.0), rng.randint(G_LO, G_HI),
                 rng.randrange(512), rng.randrange(1500), rng.randrange(6))
            for _ in range(300)]
    return out


def add_injection(d, utc_start: str) -> None:
    ev = timing.samp_to_utc(T0, timing.parse_dada_utc(utc_start))
    iso = (ev + timedelta(seconds=20)).isoformat(timespec="milliseconds")
    with d.conn:
        d.conn.execute(
            "INSERT INTO injections (inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc) VALUES (?,?,?,?,?,?,?,?)",
            (iso, BEAM // 64, BEAM, 300.0, 30.0, 3.0, "test", iso))
    d._refresh_injections(force=True)


def cfg(gulp_dir=None):
    c = {"trigger": {"fast_path": False,
                     "intensity": {"min_spacing_s": 0, "daily_max": 100}},
         "context": {"delay_s": 0.0}}
    if gulp_dir is not None:
        c["injection"] = {"gulp_dir": str(gulp_dir)}
    return c


def test_injection_gulp_writes_a_block(daemon, ingest, monkeypatch, tmp_path):
    gdir = tmp_path / "inj_gulps"
    d = daemon(**cfg(gdir))
    spool = arm(d, monkeypatch, tmp_path)
    utc = utc_start_for_recent_event()
    add_injection(d, utc)
    ingest(d, gulp_cands(second=True), utc_start=utc, gulp=GULP)

    cid, samp, beam, ev = d.conn.execute(
        "SELECT id, samp, beam, event_utc FROM clusters WHERE tags LIKE"
        " '%injection%'").fetchone()
    files = sorted(gdir.glob("*.json"))
    assert [p.name for p in files] == [f"{utc}_g{GULP}_c{cid}.json"]
    block = json.loads(files[0].read_text())
    ref = block["reference"]
    assert ref["cluster_id"] == cid and ref["outcome"] == "injection"
    assert (ref["samp"], ref["beam"], ref["event_utc"]) == (samp, beam, ev)
    assert block["gulp"] == GULP and block["utc_start"] == utc
    inj = [cl for cl in block["clusters"] if cl["id"] == ref["id"]][0]
    assert inj["outcome"] == "injection"
    assert inj["peak"][0] == 0.0 and inj["peak"][1] == beam
    # the real event in the same gulp still dumped, and says so in both places
    assert [cl["outcome"] for cl in block["clusters"]].count("triggered") == 1
    card = json.loads(next(spool.glob("*.json")).read_text())
    assert "reference" not in card["gulp"]
    assert [cl["outcome"] for cl in card["gulp"]["clusters"]].count("injection") == 1
    assert not list(gdir.glob("*.tmp"))


def test_no_block_without_an_injection(daemon, ingest, tmp_path):
    gdir = tmp_path / "inj_gulps"
    d = daemon(**cfg(gdir))
    ingest(d, gulp_cands(second=True), utc_start=utc_start_for_recent_event(),
           gulp=GULP)
    assert d.conn.execute("SELECT count(*) FROM triggers").fetchone()[0] >= 1
    assert not gdir.exists() or not list(gdir.iterdir())


def decisions(d):
    trig = d.conn.execute("SELECT stream, action, detail, kind FROM triggers"
                          " ORDER BY id").fetchall()
    cl = d.conn.execute("SELECT gulp, samp, snr, dm, beam, width, n_members,"
                        " n_beams, tier, tags, name FROM clusters ORDER BY id"
                        ).fetchall()
    gs = d.conn.execute("SELECT gulp, n_cands, n_clusters, n_stored, n_would,"
                        " n_vetoed, n_shed FROM gulp_stats ORDER BY id").fetchall()
    return trig, [r[:-1] for r in cl], gs


def run_gulps(d, ingest, utc):
    add_injection(d, utc)
    for s in range(3):
        ingest(d, gulp_cands(seed=s, second=(s != 1)), utc_start=utc, gulp=GULP + s)


def test_decisions_identical_with_and_without(daemon, ingest, tmp_path):
    utc = utc_start_for_recent_event()
    with_dir = daemon(**cfg(tmp_path / "g"))
    run_gulps(with_dir, ingest, utc)
    without = daemon(**cfg())
    run_gulps(without, ingest, utc)
    assert list((tmp_path / "g").glob("*.json"))
    assert decisions(with_dir) == decisions(without)


def test_writer_failure_is_swallowed_and_logged(daemon, ingest, monkeypatch,
                                                tmp_path, caplog):
    utc = utc_start_for_recent_event()

    def boom(*a, **kw):
        raise OSError("disk on fire")
    monkeypatch.setattr(t2d.gulp_card, "write_injection_blocks", boom)
    broken = daemon(**cfg(tmp_path / "g"))
    with caplog.at_level(logging.ERROR, logger="t2d"):
        run_gulps(broken, ingest, utc)
    assert any("injection gulp block" in r.getMessage() and r.exc_info
               for r in caplog.records)
    monkeypatch.undo()
    fine = daemon(**cfg())
    run_gulps(fine, ingest, utc)
    assert decisions(broken) == decisions(fine)


def test_queueing_failure_is_swallowed(daemon, ingest, monkeypatch, tmp_path,
                                       caplog):
    utc = utc_start_for_recent_event()
    d = daemon(**cfg(tmp_path / "g"))
    monkeypatch.setattr(d, "_write_injection_gulps",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.ERROR, logger="t2d"):
        run_gulps(d, ingest, utc)
    assert any("not queued" in r.getMessage() for r in caplog.records)
    assert d.conn.execute("SELECT count(*) FROM gulp_stats").fetchone()[0] == 3


def test_prune_drops_files_older_than_keep_days(tmp_path):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    stale_tmp = tmp_path / "x.tmp"
    for p in (old, new, stale_tmp):
        p.write_text("{}")
    past = time.time() - 15 * 86400
    os.utime(old, (past, past))
    os.utime(stale_tmp, (past, past))
    assert gulp_card.prune_injection_blocks(tmp_path, keep_days=14) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["new.json"]
