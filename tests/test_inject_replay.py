"""Automated replay: due-logic, dump window, subprocess, thread post, cleanup.

Nothing here requests a dump, runs the replay tool, or touches Slack.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    assert stop == NOW - timedelta(seconds=8)     # DM 0: no sweep, margin only
    assert stop < NOW
    assert (stop - start).total_seconds() == 16


def test_the_dump_window_follows_config():
    cfg = ir.replay_cfg({"replay": {"dump_pre_s": 40, "dump_post_s": 5,
                                    "dump_sweep_margin_s": 0}})
    start, stop = ir.dump_window(NOW, cfg)
    assert (stop - start).total_seconds() == 35


def test_the_window_end_carries_the_dm_sweep():
    """The whole dispersed pulse must be inside the dump, not just its head."""
    cfg = ir.replay_cfg({})
    later = NOW + timedelta(seconds=30)        # the request is not the limit
    for dm, sweep in ((647.0, 6.148), (1000.0, 9.502)):
        start, stop = ir.dump_window(NOW, cfg, dm, now=later)
        assert start == NOW - timedelta(seconds=24)
        want = NOW + timedelta(seconds=-10 + sweep + 2.0)
        assert abs((stop - want).total_seconds()) < 0.01


def test_the_window_end_is_clamped_to_the_request_time(caplog):
    """Safety net only: the request is ~17 s after the write, so a DM 1000 end
    1.5 s past inject_utc is already in the ring and nothing is cut."""
    later = NOW + timedelta(seconds=17)
    _, stop = ir.dump_window(NOW, ir.replay_cfg({}), 1000.0, now=later)
    assert stop > NOW                       # no clamp at the real request time
    with caplog.at_level("WARNING"):
        _, stop = ir.dump_window(NOW, ir.replay_cfg({}), 1000.0,
                                 now=NOW - timedelta(seconds=5))
    assert stop == NOW - timedelta(seconds=5)
    assert "clamping" in caplog.text


def test_the_dump_timeout_scales_with_the_window():
    """14 s took ~20 s to write, so the floor covers 24 s but not 40 s."""
    cfg = ir.replay_cfg({})
    assert ir.dump_timeout(cfg, 14.0) == 60.0
    assert ir.dump_timeout(cfg, 24.0) == 60.0
    assert ir.dump_timeout(cfg, 40.0) == 80.0


def test_the_file_count_guard_follows_the_window():
    """10.0 s per file, plus one because the window starts mid-file."""
    assert ir.max_delete_files(14.0) == 3
    assert ir.max_delete_files(24.0) == 4
    assert ir.max_delete_files(0.0) == 1


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
    assert cmd[0].endswith("t3-replay-injection")
    assert cmd[1:3] == ["--inject-id", "667"]
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

UTC0 = datetime(2026, 9, 10, 2, 30, tzinfo=UTC)
BPS = 375e6


def _dada(dump_dir, obs="2026-09-10-02:27:18", offset_s=0.0, dur_s=7.0):
    """A .dada file named the way the dump daemon names them."""
    offset = int(offset_s * BPS)
    path = Path(dump_dir) / f"{obs}_{offset:016d}.000000.dada"
    path.write_bytes(b"x" * int(dur_s * BPS / 1_000_000))   # size/1e6 scaled
    return path


def _dada_real(dump_dir, obs, offset_s, dur_s):
    """As above but with a truthful size, so the span arithmetic is real."""
    offset = int(offset_s * BPS)
    path = Path(dump_dir) / f"{obs}_{offset:016d}.000000.dada"
    with path.open("wb") as fh:
        fh.truncate(int(dur_s * BPS))
    return path


def test_only_the_window_files_go_and_the_directory_survives(tmp_path):
    """The regression that took out stream_0 on 2026-09-10.

    dump_dir is the shared per-stream directory, so cleanup must remove this
    injection's two files and nothing else - least of all the directory.
    """
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    start = t_obs + timedelta(seconds=600)
    stop = start + timedelta(seconds=14)
    mine_a = _dada_real(tmp_path, obs, 600.0, 7.0)
    mine_b = _dada_real(tmp_path, obs, 607.0, 7.0)
    older = _dada_real(tmp_path, obs, 60.0, 7.0)        # someone else's dump
    later = _dada_real(tmp_path, obs, 1200.0, 7.0)      # and another

    n = ir.cleanup_dump(tmp_path, ir.replay_cfg({}), start, stop)
    assert n == 2
    assert not mine_a.exists() and not mine_b.exists()
    assert older.is_file(), "an unrelated earlier dump was deleted"
    assert later.is_file(), "an unrelated later dump was deleted"
    assert tmp_path.is_dir(), "the shared dump directory was removed"


def test_the_directory_always_survives_even_with_nothing_to_keep(tmp_path):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    _dada_real(tmp_path, obs, 600.0, 7.0)
    ir.cleanup_dump(tmp_path, ir.replay_cfg({}),
                    t_obs + timedelta(seconds=600),
                    t_obs + timedelta(seconds=614))
    assert tmp_path.is_dir()
    assert list(tmp_path.glob("*.dada")) == []


def test_too_many_matches_delete_nothing(tmp_path, caplog):
    """If the arithmetic is wrong, deleting nothing is the safe answer."""
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    for i in range(6):
        _dada_real(tmp_path, obs, 600.0 + i * 2.0, 7.0)
    with caplog.at_level("ERROR"):
        n = ir.cleanup_dump(tmp_path, ir.replay_cfg({}),
                            t_obs + timedelta(seconds=600),
                            t_obs + timedelta(seconds=614))
    assert n == 0
    assert len(list(tmp_path.glob("*.dada"))) == 6
    assert "more than the" in caplog.text


def test_no_window_means_no_deletion(tmp_path, caplog):
    """Without a recorded window there is no way to tell whose files these are."""
    _dada_real(tmp_path, "2026-09-10-02:27:18", 600.0, 7.0)
    with caplog.at_level("WARNING"):
        assert ir.cleanup_dump(tmp_path, ir.replay_cfg({}), None, None) == 0
    assert len(list(tmp_path.glob("*.dada"))) == 1
    assert "deleting nothing" in caplog.text


def test_keep_dump_keeps_everything(tmp_path):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    f = _dada_real(tmp_path, obs, 600.0, 7.0)
    cfg = ir.replay_cfg({"replay": {"keep_dump": True}})
    assert ir.cleanup_dump(tmp_path, cfg, t_obs + timedelta(seconds=600),
                           t_obs + timedelta(seconds=614)) == 0
    assert f.is_file()


def test_cleanup_of_a_missing_directory_is_harmless(tmp_path):
    assert ir.cleanup_dump(tmp_path / "gone", ir.replay_cfg({}),
                           UTC0, UTC0 + timedelta(seconds=14)) == 0


def test_an_unparseable_name_is_left_alone(tmp_path):
    odd = tmp_path / "not-a-dump-name.dada"
    odd.write_bytes(b"x")
    assert ir.dump_file_span(odd) is None
    assert ir.cleanup_dump(tmp_path, ir.replay_cfg({}), UTC0,
                           UTC0 + timedelta(seconds=14)) == 0
    assert odd.is_file()


def test_the_span_arithmetic(tmp_path):
    f = _dada_real(tmp_path, "2026-09-10-02:27:18", 600.0, 7.0)
    t0, t1 = ir.dump_file_span(f)
    assert t0 == datetime(2026, 9, 10, 2, 37, 18, tzinfo=UTC)
    assert (t1 - t0).total_seconds() == pytest.approx(7.0)


def test_the_command_is_found_beside_the_interpreter(monkeypatch):
    """systemd's PATH lacks the venv, but the tool sits next to python."""
    import sys
    monkeypatch.setattr(ir.shutil, "which", lambda name: None)
    monkeypatch.setattr(ir.Path, "is_file", lambda self: True)
    got = ir.resolve_command("t3-replay-injection")
    assert got == [str(Path(sys.executable).parent / "t3-replay-injection")]


def test_an_absolute_command_is_left_alone():
    assert ir.resolve_command("/opt/x/t3-replay-injection --flag") == [
        "/opt/x/t3-replay-injection", "--flag"]


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
    assert calls["comment"] == ir.CAPTION
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


# --- a shot the search missed ------------------------------------------------

def test_on_miss_defaults_to_rendering_nothing():
    cfg = ir.replay_cfg({})
    assert cfg["on_miss"] == "none"
    assert cfg["keep_dump_on_miss"] is True


def test_an_unknown_on_miss_falls_back_to_none(caplog):
    with caplog.at_level("WARNING"):
        cfg = ir.replay_cfg({"replay": {"on_miss": "maybe"}})
    assert cfg["on_miss"] == "none"
    assert "none|expected" in caplog.text


@pytest.mark.parametrize("outcome,keep", [
    (oc.MISSED_T1, True),
    (oc.MISSED_T2, True),
    (oc.RECOVERED, False),
    (oc.FIRE_FAILED, False),
])
def test_keep_for_miss(outcome, keep):
    assert ir.keep_for_miss(ir.replay_cfg({}), outcome) is keep


def test_a_missed_shot_keeps_its_dump_and_logs_the_paths(tmp_path, caplog):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    mine = _dada_real(tmp_path, obs, 600.0, 7.0)
    with caplog.at_level("INFO"):
        n = ir.cleanup_dump(tmp_path, ir.replay_cfg({}),
                            t_obs + timedelta(seconds=600),
                            t_obs + timedelta(seconds=614),
                            outcome=oc.MISSED_T1, inj_label="inj_20260910_0007")
    assert n == 0
    assert mine.is_file(), "the raw stream of a miss was deleted"
    assert "inj_20260910_0007" in caplog.text
    assert str(mine) in caplog.text


def test_a_recovered_shot_still_has_its_dump_cleaned(tmp_path):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    mine = _dada_real(tmp_path, obs, 600.0, 7.0)
    n = ir.cleanup_dump(tmp_path, ir.replay_cfg({}),
                        t_obs + timedelta(seconds=600),
                        t_obs + timedelta(seconds=614),
                        outcome=oc.RECOVERED)
    assert n == 1 and not mine.exists()


def test_keep_dump_on_miss_can_be_turned_off(tmp_path):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    mine = _dada_real(tmp_path, obs, 600.0, 7.0)
    cfg = ir.replay_cfg({"replay": {"keep_dump_on_miss": False}})
    n = ir.cleanup_dump(tmp_path, cfg, t_obs + timedelta(seconds=600),
                        t_obs + timedelta(seconds=614), outcome=oc.MISSED_T1)
    assert n == 1 and not mine.exists()


def test_a_rendered_miss_archives_only_the_card(tmp_path):
    """card_only: the numbers, not the pictures."""
    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path)}})
    cmd = ir.build_command(670, "/d", cfg, label="inj_20260910_0007",
                           card_only=True)
    paths = ir.replay_paths(670, cfg, "inj_20260910_0007")
    assert "--card-json" in cmd and str(paths["json"]) in cmd
    assert "--fil" not in cmd
    assert str(paths["png"]) not in cmd        # no PNG kept beside the card


# --- all the window's files reach the replay tool ----------------------------

def test_a_24s_window_accepts_three_files_and_rejects_five(tmp_path, caplog):
    """2-3 files is what a 24 s window is; five means the arithmetic is wrong."""
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    start = t_obs + timedelta(seconds=600)
    stop = start + timedelta(seconds=24)
    for i in range(3):
        _dada_real(tmp_path, obs, 600.0 + i * 10.0, 10.0)
    assert ir.cleanup_dump(tmp_path, ir.replay_cfg({}), start, stop) == 3

    for i in range(5):
        _dada_real(tmp_path, obs, 600.0 + i * 5.0, 6.0)
    with caplog.at_level("ERROR"):
        n = ir.cleanup_dump(tmp_path, ir.replay_cfg({}), start, stop)
    assert n == 0 and len(list(tmp_path.glob("*.dada"))) == 5
    assert "more than the" in caplog.text


def test_every_file_of_the_window_reaches_the_replay_command(tmp_path):
    obs = "2026-09-10-02:27:18"
    t_obs = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
    dumps = tmp_path / "stream_0"
    dumps.mkdir()
    mine = [_dada_real(dumps, obs, 600.0 + i * 10.0, 10.0) for i in range(3)]
    _dada_real(dumps, obs, 60.0, 10.0)             # someone else's dump
    start = t_obs + timedelta(seconds=600)
    stop = start + timedelta(seconds=24)

    arg = ir.replay_dump_arg(dumps, start, stop)
    assert arg.split(",") == [str(f) for f in mine]   # all of them, time order

    cfg = ir.replay_cfg({"replay": {"events_root": str(tmp_path / "ev")}})
    cmd = ir.build_command(671, arg, cfg)
    assert cmd[cmd.index("--dump") + 1] == arg
    cmd = ir.build_command(671, mine, cfg)            # a list works too
    assert cmd[cmd.index("--dump") + 1].split(",") == [str(f) for f in mine]


def test_without_a_window_the_directory_is_passed(tmp_path, caplog):
    assert ir.replay_dump_arg(tmp_path, None, None) == str(tmp_path)
    with caplog.at_level("WARNING"):
        t0 = datetime(2026, 9, 10, 2, 27, 18, tzinfo=UTC)
        assert ir.replay_dump_arg(tmp_path, t0, t0 + timedelta(seconds=24)) \
            == str(tmp_path)
    assert "passing the directory" in caplog.text
