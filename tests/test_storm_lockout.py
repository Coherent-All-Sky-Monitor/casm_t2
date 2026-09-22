"""Storm lockout and storm_skip_gulp.

Lockout: a trigger needs `storm_lockout_s` of trigger-eligible quiet;
every eligible event restarts the clock whether or not it dumped, so
a sustained storm costs one dump total.

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
    d._lockout_clock = datetime.now(timezone.utc) - timedelta(seconds=31)
    _fire(d, make_cluster(snr=25.0), "260814ffffff", "tier_B")
    assert _actions(d)[-1] == ("triggered", "tier_B;OK")


TRIGGERED_TRIG = dict(LOCKOUT_TRIG, storm_lockout_reset="triggered")


def _fake_clock(monkeypatch):
    """t2d's datetime.now under test control: set `clock.t` before each call."""
    class Clock:
        t = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    clock = Clock()

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.t
    monkeypatch.setattr(t2d, "datetime", FakeDatetime)
    return clock


def test_triggered_reset_refused_events_do_not_extend(daemon, make_cluster, monkeypatch):
    """storm_lockout_reset: triggered. Locked-out events leave the clock alone,
    so 31 s after the dump the next one goes out even though refused events
    came in between."""
    d = daemon(trigger=TRIGGERED_TRIG)
    _arm(d, monkeypatch)
    clock = _fake_clock(monkeypatch)
    t0 = clock.t
    for dt, name in [(0, "260922aaaaaa"), (10, "260922bbbbbb"), (25, "260922cccccc"),
                     (31, "260922dddddd"), (40, "260922eeeeee")]:
        clock.t = t0 + timedelta(seconds=dt)
        asyncio.run(d._trigger(make_cluster(snr=25.0), name, clock.t,
                               "B", "tier_B", None))
    assert [a for a, _ in _actions(d)] == [
        "triggered", "refused", "refused", "triggered", "refused"]
    assert _actions(d)[1] == ("refused", "tier_B;storm_lockout")


def test_triggered_reset_ignores_a_refused_dump(daemon, make_cluster, monkeypatch):
    """A dump the daemon refuses is no Slack post and does not restart the clock."""
    d = daemon(trigger=TRIGGERED_TRIG)
    _arm(d, monkeypatch)
    replies = iter(["BUSY", "OK"])

    async def fake_dump(host, port, start, stop):
        return next(replies)
    monkeypatch.setattr(t2d, "request_dump_async", fake_dump)
    t0 = datetime.now(timezone.utc)
    for dt, name in [(0, "260922ffffff"), (5, "260922gggggg")]:
        asyncio.run(d._trigger(make_cluster(snr=25.0), name, t0 + timedelta(seconds=dt),
                               "B", "tier_B", None))
    assert [a for a, _ in _actions(d)] == ["refused_daemon", "triggered"]


def test_shipped_config_resets_on_trigger():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config" / "t2d.yaml").read_text())
    assert cfg["trigger"]["storm_lockout_reset"] == "triggered"
    assert cfg["trigger"]["storm_lockout_s"] == 300


def test_bad_reset_value_is_rejected(daemon):
    import pytest
    with pytest.raises(ValueError):
        daemon(trigger=dict(LOCKOUT_TRIG, storm_lockout_reset="sometimes"))


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
