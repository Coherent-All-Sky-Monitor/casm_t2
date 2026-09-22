"""The trigger card's gulp block (casm_t2.gulp_card), written by t2d.

The block reports t2d's own clustering of the coalesced gulp for T3's bottom
row. It must carry every clustered trial, name exactly one triggered cluster,
stay a sane size on a storm gulp, and leave every trigger decision unchanged.
"""

import json
import random

import pytest

from casm_t2 import beams, cluster, gulp_card, timing
from casm_t2.apps import t2d
from casm_t2.wire import Candidate

GULP = 28467
# hella preamble gulp g carries samples g*8192 - 960 .. g*8192 + 7231
G_LO = GULP * 8192 - 960
G_HI = GULP * 8192 + 7231

SCHEMA_KEYS = {"gulp", "utc_start", "samp_lo", "samp_hi", "tsamp_s", "weights_id",
               "n_raw", "n_width_dropped", "n_shed", "n_trials_total",
               "trials_truncated", "n_clusters_total", "clusters_truncated",
               "trials", "clusters", "rules", "storm"}
CLUSTER_KEYS = {"id", "peak", "n_trials", "beams", "sky_extent_deg", "dm_lo",
                "dm_hi", "outcome", "detail"}


def dm_of(idx: int) -> float:
    return round(5.0 + 0.5 * idx, 3)


def cand(snr, samp, beam, dm_idx, width):
    return Candidate(snr=float(snr), samp=int(samp), time_days=0.0,
                     width=int(width), dm_idx=int(dm_idx), dm=dm_of(dm_idx),
                     beam=int(beam))


def synthetic_gulp(seed=0, n_noise=300, frb=True, second=False):
    """One gulp of trials inside G_LO..G_HI.

    A bright dispersed event in beam 100 (DM ~300, widths 2-4), optionally a
    fainter second triggerable event 3 s later, a DM-floor junk burst, noise
    scattered over all beams, and width-6 trials the width veto drops.
    """
    rng = random.Random(seed)
    out = []
    t0 = G_LO + 4000
    if frb:
        for k in range(60):
            out.append(cand(25.0 - 0.1 * k, t0 + rng.randint(-8, 8),
                            100 + (k % 2), 590 + rng.randint(-10, 10),
                            2 + k % 3))
    if second:
        for k in range(40):
            out.append(cand(21.0 - 0.1 * k, t0 + 3000 + rng.randint(-8, 8),
                            300, 800 + rng.randint(-10, 10), 2 + k % 2))
    for _ in range(n_noise):
        out.append(cand(rng.uniform(8.0, 11.0), rng.randint(G_LO, G_HI),
                        rng.randrange(512), rng.randrange(1500),
                        rng.randrange(6)))
    for _ in range(50):
        out.append(cand(rng.uniform(8.0, 11.0), rng.randint(G_LO, G_HI),
                        rng.randrange(512), rng.randrange(1500), 6))
    return out


def arm(d, monkeypatch, tmp_path):
    """Live dumping against a fake daemon; cards land in tmp_path/spool."""
    d.dumps_enabled = True

    async def fake_dump(host, port, start, stop):
        return "OK"
    monkeypatch.setattr(t2d, "request_dump_async", fake_dump)
    d.disk.refusal = lambda host, path: None
    spool = tmp_path / "spool"
    monkeypatch.setattr(t2d.beams, "stream_location", lambda s: beams.StreamLocation(
        stream=s, host=t2d.LOCAL_HOSTNAME, control_port=28000 + s,
        dump_dir=str(tmp_path / f"stream_{s}"), spool_dir=str(spool)))
    return spool


CFG = {"veto_widths": [6], "context": {"delay_s": 0.0},
       "trigger": {"fast_path": False,
                   "intensity": {"min_spacing_s": 0, "daily_max": 100}}}


def one_card(spool):
    cards = sorted(spool.glob("*.json"))
    assert len(cards) == 1
    return json.loads(cards[0].read_text())


def test_card_carries_the_gulp_block(daemon, ingest, monkeypatch, tmp_path):
    d = daemon(**CFG)
    spool = arm(d, monkeypatch, tmp_path)
    cands = synthetic_gulp()
    ingest(d, cands, gulp=GULP)

    card = one_card(spool)
    g = card["gulp"]
    assert set(g) == SCHEMA_KEYS
    assert all(set(cl) == CLUSTER_KEYS for cl in g["clusters"])
    # the gulp number is the one t2d records in the DB
    db_gulp = d.conn.execute("SELECT gulp FROM gulp_stats").fetchone()[0]
    assert g["gulp"] == db_gulp == GULP
    assert (g["samp_lo"], g["samp_hi"]) == (G_LO, G_HI)
    assert g["tsamp_s"] == timing.TSAMP_S
    assert g["n_raw"] == len(cands)
    assert g["n_width_dropped"] == 50
    assert g["n_shed"] == 0
    assert g["trials_truncated"] is False and g["clusters_truncated"] is False
    assert set(g["storm"]) == {"raw_recent", "stormy"}
    assert g["storm"]["raw_recent"][-1] == len(cands) - 50
    assert g["rules"]["snr"] == d.tier_b
    # the card and its triggered cluster agree, and dt_s is relative to card.samp
    trig = [cl for cl in g["clusters"] if cl["outcome"] == "triggered"][0]
    assert trig["peak"] == [0.0, card["beam"], round(card["dm"], 3),
                            round(card["snr"], 2), card["width"]]
    assert G_LO <= card["samp"] <= G_HI
    for dt, *_ in g["trials"]:
        samp = card["samp"] + dt / g["tsamp_s"]
        assert g["samp_lo"] - 0.5 <= samp <= g["samp_hi"] + 0.5
    # the pre-v2 context block survives for old readers
    assert "context" in card


def test_trials_are_every_clustered_candidate(daemon, ingest, monkeypatch, tmp_path):
    d = daemon(**CFG)
    spool = arm(d, monkeypatch, tmp_path)
    cands = synthetic_gulp(seed=3)
    ingest(d, cands, gulp=GULP)

    g = one_card(spool)["gulp"]
    n_cands, n_vetoed, n_shed = d.conn.execute(
        "SELECT n_cands, n_vetoed, n_shed FROM gulp_stats").fetchone()
    clustered = [c for c in cands if c.width != 6]
    assert len(g["trials"]) == len(clustered) == n_cands - n_vetoed - n_shed
    assert g["n_trials_total"] == len(g["trials"])
    assert all(r[4] != 6 for r in g["trials"])
    # every trial sits in a listed cluster and the per-cluster counts add up
    ids = {cl["id"] for cl in g["clusters"]}
    assert {r[5] for r in g["trials"]} <= ids
    assert sum(cl["n_trials"] for cl in g["clusters"]) == len(g["trials"])
    for cl in g["clusters"]:
        rows = [r for r in g["trials"] if r[5] == cl["id"]]
        assert len(rows) == cl["n_trials"]
        assert {r[1] for r in rows} == {b for b, _ in cl["beams"]}
    # time ordered, as the reference writer emits them
    assert [r[0] for r in g["trials"]] == sorted(r[0] for r in g["trials"])


def test_triggered_cluster_appears_exactly_once(daemon, ingest, monkeypatch,
                                                tmp_path):
    d = daemon(**CFG)
    spool = arm(d, monkeypatch, tmp_path)
    ingest(d, synthetic_gulp(seed=5, second=True), gulp=GULP)

    g = one_card(spool)["gulp"]
    outcomes = [cl["outcome"] for cl in g["clusters"]]
    assert outcomes.count("triggered") == 1
    # the fainter second event passed every rule and lost the one-dump-per-gulp
    assert outcomes.count("also_triggerable") == 1
    actions = d.conn.execute(
        "SELECT action, detail FROM triggers ORDER BY id").fetchall()
    assert [a for a, _ in actions] == ["triggered", "suppressed"]
    assert actions[1][1].endswith(";gulp_dup")
    trig = [cl for cl in g["clusters"] if cl["outcome"] == "triggered"][0]
    assert trig["peak"][0] == 0.0
    assert trig["n_trials"] == sum(1 for r in g["trials"] if r[5] == trig["id"])


def storm_report(n, seed=11):
    """A GulpReport over n synthetic trials, clustered the way t2d does."""
    rng = random.Random(seed)
    cands = synthetic_gulp(seed=seed, n_noise=0)
    cands = [c for c in cands if c.width != 6]
    while len(cands) < n:
        cands.append(cand(rng.uniform(8.0, 14.0), rng.randint(G_LO, G_HI),
                          rng.randrange(512), rng.randrange(1500),
                          rng.randrange(6)))
    clusters, labels = cluster.cluster_candidates_labelled(
        cands, cluster.ClusterParams(), None)
    trig = next(k for k, cl in enumerate(clusters) if cl.peak.beam in (100, 101))
    return gulp_card.GulpReport(
        gulp=GULP, utc_start="2026-09-19-05:01:25", weights_id="w",
        n_raw=n, n_width_dropped=0, n_shed=0, cands=cands, labels=labels,
        clusters=clusters, outcomes=[("snr_below", "")] * len(clusters),
        rules={"snr": 18.0}, raw_recent=(0, 0, 0, 0, n)), trig


def test_storm_gulp_block_is_bounded_and_says_so(pointings):
    report, trig = storm_report(20000)
    block = gulp_card.gulp_block(report, trig)
    # a full card: the block plus a 512-beam pointing table, as t2d writes it
    body = gulp_card.card_json({"candname": "x", "pointings": pointings,
                                "gulp": block})
    assert json.loads(body)["gulp"] == json.loads(json.dumps(block))
    assert len(body.encode()) < 1_000_000
    assert block["n_trials_total"] == 20000
    assert block["trials_truncated"] is True
    assert len(block["trials"]) == gulp_card.MAX_CARD_TRIALS
    assert block["n_clusters_total"] == len(report.clusters)
    assert len(block["clusters"]) <= gulp_card.MAX_CARD_CLUSTERS
    # the triggered cluster is listed once and none of its trials were cut
    tc = [cl for cl in block["clusters"] if cl["outcome"] == "triggered"]
    assert len(tc) == 1 and tc[0]["id"] == trig
    assert sum(1 for r in block["trials"] if r[5] == trig) == tc[0]["n_trials"]


def test_small_gulp_is_not_truncated():
    report, trig = storm_report(500)
    block = gulp_card.gulp_block(report, trig)
    assert block["trials_truncated"] is False
    assert len(block["trials"]) == block["n_trials_total"] == 500


@pytest.mark.parametrize("with_sky", [False, True])
def test_labelled_clustering_matches_the_decision_clustering(sky, with_sky):
    table = sky[0] if with_sky else None
    cands = [c for c in synthetic_gulp(seed=7, n_noise=2000, second=True)]
    plain = cluster.cluster_candidates(cands, cluster.ClusterParams(), table)
    labelled, labels = cluster.cluster_candidates_labelled(
        cands, cluster.ClusterParams(), table)
    assert labelled == plain
    assert len(labels) == len(cands)
    for k, cl in enumerate(labelled):
        members = [c for c, lab in zip(cands, labels) if lab == k]
        assert len(members) == cl.n_members
        assert cl.peak in members
        member_beams = {c.beam for c in members}
        assert len(member_beams) == cl.n_beams
        assert (min(member_beams), max(member_beams)) == (cl.beam_lo, cl.beam_hi)


def _decisions(d):
    trig = d.conn.execute("SELECT stream, action, detail, kind FROM triggers"
                          " ORDER BY id").fetchall()
    cl = d.conn.execute("SELECT gulp, samp, snr, dm, beam, width, n_members,"
                        " n_beams, tier, tags FROM clusters ORDER BY id").fetchall()
    gs = d.conn.execute("SELECT gulp, n_cands, n_clusters, n_stored, n_would,"
                        " n_vetoed, n_shed FROM gulp_stats ORDER BY id").fetchall()
    return trig, cl, gs


def test_trigger_decisions_identical_without_the_block(daemon, ingest,
                                                       monkeypatch, tmp_path):
    gulps = [(synthetic_gulp(seed=s, second=(s % 2 == 0), frb=(s != 2)), GULP + s)
             for s in range(5)]

    with_block = daemon(**CFG)
    spool = arm(with_block, monkeypatch, tmp_path / "a")
    for cands, g in gulps:
        ingest(with_block, cands, gulp=g)
    written = sorted(spool.glob("*.json"))
    assert written and all("gulp" in json.loads(p.read_text()) for p in written)

    # The pre-change path: no report is built, clustering unlabelled.
    monkeypatch.setattr(t2d.gulp_card, "GulpReport", lambda **kw: None)
    without = daemon(**CFG)
    spool_b = arm(without, monkeypatch, tmp_path / "b")
    for cands, g in gulps:
        ingest(without, cands, gulp=g)
    cards_b = sorted(spool_b.glob("*.json"))
    assert len(cards_b) == len(written)
    assert all("gulp" not in json.loads(p.read_text()) for p in cards_b)

    assert _decisions(with_block) == _decisions(without)


def test_card_json_round_trips():
    obj = {"a": [1, 2.5, None, "x"], "b": {"c": [[1, 2], [3, 4]], "d": {}},
           "e": [], "f": [{"g": 1}], "h": True}
    assert json.loads(gulp_card.card_json(obj)) == obj
