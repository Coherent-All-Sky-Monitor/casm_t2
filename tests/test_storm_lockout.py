"""Storm lockout and storm_skip_gulp (2026-08-13).

Lockout: a blind trigger needs `storm_lockout_s` of blind-eligible quiet;
every eligible blind event restarts the clock whether or not it dumped, so
a sustained storm costs one dump total. Known-source triggers are exempt.

storm_skip_gulp: a gulp over max_cands_per_gulp is dropped whole instead of
shed to the two-stage cap.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from casm_t2.apps import t2d


def _arm(daemon_, monkeypatch):
    """Enable live dumping against a fake daemon that always answers OK."""
    daemon_.dumps_enabled = True
    async def fake_dump(host, port, start, stop):
        return "OK"
    monkeypatch.setattr(t2d, "request_dump_async", fake_dump)
    daemon_.disk.refusal = lambda host, path: None
    daemon_._spawn = lambda coro: coro.close()   # no trigger cards on disk


def _fire(daemon_, cl, name, reason):
    asyncio.run(daemon_._trigger(cl, name, datetime.now(timezone.utc),
                                 "B", reason, None))


def _actions(daemon_):
    return daemon_.conn.execute(
        "SELECT action, detail FROM triggers ORDER BY id").fetchall()


LOCKOUT_TRIG = {"fast_path": False, "storm_lockout_s": 30,
                "intensity": {"min_spacing_s": 0, "daily_max": 100}}


def test_second_blind_within_lockout_is_refused(daemon, make_cluster, monkeypatch):
    d = daemon(trigger=LOCKOUT_TRIG)
    _arm(d, monkeypatch)
    _fire(d, make_cluster(snr=25.0), "260814aaaaaa", "tier_B")
    _fire(d, make_cluster(snr=40.0), "260814bbbbbb", "tier_A")

    assert _actions(d) == [("triggered", "tier_B;OK"),
                           ("refused", "tier_A;storm_lockout")]


def test_locked_out_events_extend_the_lockout(daemon, make_cluster, monkeypatch):
    d = daemon(trigger=LOCKOUT_TRIG)
    _arm(d, monkeypatch)
    for name in ["260814cccccc", "260814dddddd", "260814eeeeee"]:
        _fire(d, make_cluster(snr=25.0), name, "tier_B")

    acts = [a for a, _ in _actions(d)]
    assert acts == ["triggered", "refused", "refused"]

    # 31 s of quiet clears it.
    d._last_blind_eligible = datetime.now(timezone.utc) - timedelta(seconds=31)
    _fire(d, make_cluster(snr=25.0), "260814ffffff", "tier_B")
    assert _actions(d)[-1] == ("triggered", "tier_B;OK")


def test_known_source_is_exempt_from_lockout(daemon, make_cluster, monkeypatch):
    d = daemon(trigger=LOCKOUT_TRIG)
    _arm(d, monkeypatch)
    _fire(d, make_cluster(snr=25.0), "260814gggggg", "tier_B")
    _fire(d, make_cluster(snr=12.0), "260814hhhhhh", "known_source:B0329+54")

    assert _actions(d) == [("triggered", "tier_B;OK"),
                           ("triggered", "known_source:B0329+54;OK")]


def test_lockout_disabled_by_default(daemon, make_cluster, monkeypatch):
    d = daemon(trigger={"fast_path": False,
                        "intensity": {"min_spacing_s": 0, "daily_max": 100}})
    _arm(d, monkeypatch)
    _fire(d, make_cluster(snr=25.0), "260814iiiiii", "tier_B")
    _fire(d, make_cluster(snr=25.0), "260814jjjjjj", "tier_B")

    assert [a for a, _ in _actions(d)] == ["triggered", "triggered"]


def _gulp_stats(daemon_):
    return daemon_.conn.execute(
        "SELECT n_cands, n_shed, n_clusters FROM gulp_stats").fetchall()


def test_storm_skip_drops_the_whole_gulp(daemon, make_cand, ingest):
    d = daemon(storm_skip_gulp=True, max_cands_per_gulp=10, veto_widths=[])
    ingest(d, [make_cand(snr=15.0 + i, beam=i % 8, samp=100 * i)
               for i in range(20)])

    assert _gulp_stats(d) == [(20, 20, 0)]


def test_storm_skip_leaves_under_cap_gulps_alone(daemon, make_cand, ingest):
    d = daemon(storm_skip_gulp=True, max_cands_per_gulp=10, veto_widths=[])
    cands = [make_cand(snr=25.0, beam=5, samp=1000 + i) for i in range(8)]
    ingest(d, cands)

    (n_cands, n_shed, n_clusters), = _gulp_stats(d)
    assert (n_cands, n_shed) == (8, 0)
    assert n_clusters >= 1
