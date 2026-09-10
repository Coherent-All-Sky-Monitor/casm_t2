"""Automated replay: due-logic, dump window, subprocess, thread post, cleanup.

Nothing here requests a dump, runs the replay tool, or touches Slack.
"""

from datetime import datetime, timedelta, timezone

import pytest

from casm_t2 import inject_outcome as oc, inject_replay as ir, inject_slack


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 2, 30, tzinfo=UTC)


# --- config -----------------------------------------------------------------

def test_defaults_are_filled_in():
    cfg = ir.replay_cfg({})
    assert cfg["post"] == "daily"
    assert (cfg["dump_pre_s"], cfg["dump_post_s"]) == (24.0, 10.0)
    assert cfg["keep_dump"] is False
    assert cfg["command"] == "t3-replay-injection"


def test_an_unknown_post_mode_disables_rather_than_guesses(caplog):
    with caplog.at_level("WARNING"):
        cfg = ir.replay_cfg({"replay": {"post": "sometimes"}})
    assert cfg["post"] == "never"
    assert "not one of" in caplog.text


def test_the_shipped_config_is_valid():
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    cfg = ir.replay_cfg(icfg)
    assert cfg["post"] in ir.POST_MODES
    assert cfg["post"] != "never"          # shipped enabled at daily
    assert icfg["reconcile_wait_s"] == 90


# --- the dump window --------------------------------------------------------

def test_the_dump_window_is_before_inject_utc():
    """The pulse precedes the FIFO write: the sidecar joins an older gulp."""
    start, stop = ir.dump_window(NOW, ir.replay_cfg({}))
    assert start == NOW - timedelta(seconds=24)
    assert stop == NOW - timedelta(seconds=10)
    assert stop < NOW
    assert (stop - start).total_seconds() == 14


def test_the_dump_window_follows_config():
    cfg = ir.replay_cfg({"replay": {"dump_pre_s": 40, "dump_post_s": 5}})
    start, stop = ir.dump_window(NOW, cfg)
    assert (stop - start).total_seconds() == 35


def test_a_dump_is_requested_unless_posting_is_off():
    for mode in ("daily", "on_miss", "always"):
        assert ir.dump_due(ir.replay_cfg({"replay": {"post": mode}})) is True
    assert ir.dump_due(ir.replay_cfg({"replay": {"post": "never"}})) is False


# --- when to post -----------------------------------------------------------

@pytest.mark.parametrize("mode,outcome,last_day,expect", [
    ("never",   oc.RECOVERED, None,         False),
    ("never",   oc.MISSED_T1, None,         False),
    ("always",  oc.RECOVERED, "2026-09-10", True),
    ("always",  oc.MISSED_T1, "2026-09-10", True),
    ("on_miss", oc.RECOVERED, None,         False),
    ("on_miss", oc.MISSED_T1, "2026-09-10", True),
    ("on_miss", oc.MISSED_T2, "2026-09-10", True),
    # daily: every miss, plus the first recovered shot of the day
    ("daily",   oc.MISSED_T1, "2026-09-10", True),
    ("daily",   oc.RECOVERED, None,         True),
    ("daily",   oc.RECOVERED, "2026-09-09", True),   # yesterday -> due again
    ("daily",   oc.RECOVERED, "2026-09-10", False),  # already posted today
])
def test_post_due(mode, outcome, last_day, expect):
    cfg = ir.replay_cfg({"replay": {"post": mode}})
    assert ir.post_due(cfg, outcome, last_day, NOW) is expect


def test_daily_rearms_across_the_utc_day_boundary():
    cfg = ir.replay_cfg({"replay": {"post": "daily"}})
    before = datetime(2026, 9, 10, 23, 59, 30, tzinfo=UTC)
    after = datetime(2026, 9, 11, 0, 0, 30, tzinfo=UTC)
    assert ir.post_due(cfg, oc.RECOVERED, "2026-09-10", before) is False
    assert ir.post_due(cfg, oc.RECOVERED, "2026-09-10", after) is True


def test_fire_failed_does_not_count_as_a_miss_for_posting():
    """Nothing reached the stream, so there is nothing to replay."""
    cfg = ir.replay_cfg({"replay": {"post": "on_miss"}})
    assert ir.post_due(cfg, oc.FIRE_FAILED, None, NOW) is False


def test_last_replay_day_reads_the_ledger(conn):
    for rid, day, posted in ((1, "2026-09-09", 1), (2, "2026-09-10", 0)):
        conn.execute(
            "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc, replay_posted)"
            " VALUES (?,?,0,4,300.0,5.0,5.0,'f',?,?)",
            (rid, f"{day}T02:00:00.000+00:00", f"{day}T02:00:00.000+00:00",
             posted))
    conn.commit()
    assert ir.last_replay_day(conn) == "2026-09-09"


# --- where the pulse should have been ---------------------------------------

def test_expected_event_utc_uses_the_recent_median_lead(conn):
    now = "2026-09-10T02:00:00.000+00:00"
    for rid, lead in ((1, -14.0), (2, -18.0), (3, -16.0)):
        conn.execute(
            "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc, outcome, rec_lead_s)"
            " VALUES (?,?,0,4,300.0,5.0,5.0,'f',?,?,?)",
            (rid, now, now, oc.RECOVERED, lead))
    conn.commit()
    t0 = datetime(2026, 9, 10, 2, 30, tzinfo=UTC)
    assert ir.expected_event_utc(conn, t0) == t0 - timedelta(seconds=16)


def test_expected_event_utc_falls_back_when_nothing_recovered(conn):
    t0 = datetime(2026, 9, 10, 2, 30, tzinfo=UTC)
    assert ir.expected_event_utc(conn, t0) == t0 + timedelta(
        seconds=ir.DEFAULT_LEAD_S)
    assert ir.DEFAULT_LEAD_S < 0        # the pulse precedes the write


# --- the subprocess command -------------------------------------------------

def test_command_for_a_recovered_shot(tmp_path):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})
    cmd = ir.build_command(667, "/dumps/x", cfg, db_path="/db/t2.sqlite")
    assert cmd[:3] == ["t3-replay-injection", "--inject-id", "667"]
    assert "--dump" in cmd and "/dumps/x" in cmd
    assert str(tmp_path / "inj667" / "inj667.png") in cmd
    assert str(tmp_path / "inj667" / "inj667.json") in cmd
    assert str(tmp_path / "inj667" / "inj667.fil") in cmd
    assert "--db" in cmd and "/db/t2.sqlite" in cmd
    assert "--event-utc" not in cmd       # it was found; no need to say where


def test_a_miss_passes_event_utc(tmp_path):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})
    when = datetime(2026, 9, 10, 2, 29, 44, tzinfo=UTC)
    cmd = ir.build_command(670, "/dumps/x", cfg, event_utc=when)
    assert "--event-utc" in cmd
    assert cmd[cmd.index("--event-utc") + 1].startswith("2026-09-10T02:29:44")


def test_a_multi_word_command_is_split(tmp_path):
    """So `python -m casm_t3.apps.replay_injection` works as a command."""
    cfg = ir.replay_cfg({"replay": {"command": "python -m casm_t3.apps.x",
                                    "events_root": str(tmp_path)}})
    assert ir.build_command(1, "/d", cfg)[:3] == [
        "python", "-m", "casm_t3.apps.x"]


def test_run_replay_reports_a_nonzero_exit(tmp_path, caplog):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})

    class Res:
        returncode = 2
        stderr = "boom"
    with caplog.at_level("WARNING"):
        assert ir.run_replay(1, "/d", cfg, runner=lambda *a, **k: Res()) is None
    assert "exited 2" in caplog.text


def test_run_replay_notices_a_missing_png(tmp_path, caplog):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})

    class Res:
        returncode = 0
        stderr = ""
    with caplog.at_level("WARNING"):
        assert ir.run_replay(1, "/d", cfg, runner=lambda *a, **k: Res()) is None
    assert "no PNG" in caplog.text


def test_run_replay_returns_the_png(tmp_path):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})
    paths = ir.replay_paths(9, cfg)

    class Res:
        returncode = 0
        stderr = ""

    def runner(cmd, **kw):
        paths["png"].parent.mkdir(parents=True, exist_ok=True)
        paths["png"].write_bytes(b"png")
        return Res()

    assert ir.run_replay(9, "/d", cfg, runner=runner) == paths["png"]


def test_a_runner_that_raises_is_swallowed(tmp_path, caplog):
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})

    def boom(*a, **k):
        raise OSError("no such tool")
    with caplog.at_level("WARNING"):
        assert ir.run_replay(1, "/d", cfg, runner=boom) is None
    assert "failed to run" in caplog.text


# --- cleanup ----------------------------------------------------------------

def test_the_dump_is_deleted_by_default(tmp_path):
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "a.dada").write_bytes(b"x" * 10)
    assert ir.cleanup_dump(dump, ir.replay_cfg({})) is True
    assert not dump.exists()


def test_keep_dump_keeps_it(tmp_path):
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "a.dada").write_bytes(b"x")
    cfg = ir.replay_cfg({"replay": {"keep_dump": True}})
    assert ir.cleanup_dump(dump, cfg) is False
    assert (dump / "a.dada").is_file()


def test_cleanup_of_a_missing_dump_is_harmless(tmp_path):
    assert ir.cleanup_dump(tmp_path / "gone", ir.replay_cfg({})) is False


# --- the thread post --------------------------------------------------------

def test_replay_posts_as_a_thread_reply(tmp_path, monkeypatch):
    poster = inject_slack.SlackPoster(enabled=True)
    calls = {}

    def fake_post_file(png, title, thread_ts=None, comment=None):
        calls.update(png=str(png), title=title, thread_ts=thread_ts,
                     comment=comment)
        return True

    monkeypatch.setattr(poster, "_post_file", fake_post_file)
    row = {"id": 667, "slack_ts": "1757000000.001"}
    assert poster.post_replay(row, tmp_path / "x.png", ir.CAPTION) is True
    assert calls["thread_ts"] == "1757000000.001"     # a reply, not a new post
    assert calls["comment"] == "replay: injected pulse added to the stream dump"
    assert calls["title"] == "inj667 replay"


def test_replay_is_not_posted_loose_without_a_ts(tmp_path, monkeypatch, caplog):
    poster = inject_slack.SlackPoster(enabled=True)
    monkeypatch.setattr(poster, "_post_file",
                        lambda *a, **k: pytest.fail("posted loose"))
    with caplog.at_level("WARNING"):
        assert poster.post_replay({"id": 1, "slack_ts": None},
                                  tmp_path / "x.png", ir.CAPTION) is False
    assert "not posting the replay loose" in caplog.text


def test_dry_run_writes_the_thread_message(tmp_path):
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path)
    assert poster.post_replay({"id": 667, "slack_ts": "123.4"},
                              tmp_path / "x.png", ir.CAPTION) is True
    written = list(tmp_path.glob("*replay_667.txt"))
    assert written
    body = written[0].read_text()
    assert "thread_ts=123.4" in body
    assert ir.CAPTION in body
