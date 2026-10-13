"""Replay command with hella's mask records, and the raw replay figure in the Slack thread.

Nothing here runs the replay tool or opens a socket.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from casm_t2 import inject_replay as ir, inject_slack
from casm_t2.apps import inject_daemon as dd


def _cfg(tmp_path, **kw):
    return ir.replay_cfg({"replay": {"events_root": str(tmp_path), **kw}})


# --- config and command ------------------------------------------------------------

def test_mask_records_defaults_to_off_and_the_shipped_config_names_it():
    import yaml
    assert ir.replay_cfg({})["mask_records"] == ""
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    assert icfg["replay"]["mask_records"] == ""


def test_every_command_gets_a_raw_out_beside_the_png(tmp_path):
    cmd = ir.build_command(667, "/dumps/x", _cfg(tmp_path), label="inj_20261013_0001")
    d = tmp_path / "inj_20261013_0001"
    assert cmd[cmd.index("--out") + 1] == str(d / "inj_20261013_0001.png")
    assert cmd[cmd.index("--raw-out") + 1] == str(d / "inj_20261013_0001_raw.png")
    assert "--mask-records" not in cmd


def test_mask_records_are_passed_when_set(tmp_path):
    cmd = ir.build_command(667, "/dumps/x", _cfg(tmp_path, mask_records="/mnt/nvme4/data/casm/hella_masks"))
    assert cmd[cmd.index("--mask-records") + 1] == "/mnt/nvme4/data/casm/hella_masks"
    assert "--raw-out" in cmd and "--clean" not in cmd          # the other flags are untouched


def test_card_only_commands_send_both_pngs_to_scratch(tmp_path):
    cmd = ir.build_command(670, "/d", _cfg(tmp_path), label="inj_x", card_only=True)
    d = tmp_path / "inj_x"
    assert cmd[cmd.index("--out") + 1] == str(d / "_scratch_inj_x.png")
    assert cmd[cmd.index("--raw-out") + 1] == str(d / "_scratch_inj_x_raw.png")
    assert "--fil" not in cmd


def test_replay_paths_name_the_raw_png(tmp_path):
    paths = ir.replay_paths(5, _cfg(tmp_path), "inj_a")
    assert paths["raw_png"] == tmp_path / "inj_a" / "inj_a_raw.png"


def test_raw_png_path_is_none_until_the_file_exists(tmp_path):
    cfg = _cfg(tmp_path)
    assert ir.raw_png_path(5, cfg, "inj_a") is None
    p = ir.replay_paths(5, cfg, "inj_a")["raw_png"]
    p.parent.mkdir(parents=True)
    p.write_bytes(b"png")
    assert ir.raw_png_path(5, cfg, "inj_a") == p


# --- the Slack poster ----------------------------------------------------------------

def _poster(monkeypatch, **kw):
    poster = inject_slack.SlackPoster(enabled=True, **kw)
    calls = []

    def fake_post_file(png, title, thread_ts=None, comment=None, want_ts=False):
        calls.append(dict(png=str(png), title=title, thread_ts=thread_ts, comment=comment))
        return True
    monkeypatch.setattr(poster, "_post_file", fake_post_file)
    return poster, calls


def test_raw_replay_is_a_thread_reply(tmp_path, monkeypatch):
    poster, calls = _poster(monkeypatch)
    row = {"id": 667, "file_id": "inj_20261013_0001", "slack_ts": "1757000000.001"}
    assert poster.post_replay_raw(row, tmp_path / "x_raw.png", "caption") is True
    assert calls == [{"png": str(tmp_path / "x_raw.png"), "title": "inj667 replay, raw",
                      "thread_ts": "1757000000.001", "comment": "caption"}]


def test_raw_replay_is_not_posted_loose(tmp_path, monkeypatch, caplog):
    poster, calls = _poster(monkeypatch)
    with caplog.at_level("WARNING"):
        assert poster.post_replay_raw({"id": 1, "slack_ts": None}, tmp_path / "x.png", "c") is False
    assert calls == [] and "not posting the raw replay loose" in caplog.text


def test_raw_replay_failures_are_fail_soft(tmp_path, monkeypatch, caplog):
    poster = inject_slack.SlackPoster(enabled=True)

    def boom(*a, **k):
        raise RuntimeError("slack down")
    monkeypatch.setattr(poster, "_post_file", boom)
    with caplog.at_level("WARNING"):
        assert poster.post_replay_raw({"id": 1, "slack_ts": "1.2"}, tmp_path / "x.png", "c") is False
    assert "raw replay post for injection 1 failed" in caplog.text
    assert inject_slack.SlackPoster(enabled=False).post_replay_raw(
        {"id": 1, "slack_ts": "1.2"}, tmp_path / "x.png", "c") is False


def test_raw_replay_dry_run_writes_the_thread_message(tmp_path):
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path)
    assert poster.post_replay_raw({"id": 667, "slack_ts": "123.4"}, tmp_path / "x_raw.png", "cap") is True
    body = next(tmp_path.glob("*replay_raw_667.txt")).read_text()
    assert "thread_ts=123.4" in body and "cap" in body and "x_raw.png" in body


# --- do_replay posts it after the masked figure --------------------------------------------

class FakePoster:
    """Records the order of posts; the main post gives the shot a ts."""

    def __init__(self, mode, main_ts="9.9", raw_raises=False):
        self.mode, self.main_ts, self.raw_raises = mode, main_ts, raw_raises
        self.calls = []

    def post_injection(self, row, png):
        self.calls.append(("main", str(png)))
        return self.main_ts

    def post_replay(self, row, png, caption):
        self.calls.append(("main", str(png)))
        return bool(self.main_ts)

    def post_replay_raw(self, row, png, caption):
        self.calls.append(("raw", str(png), row.get("slack_ts"), caption))
        if self.raw_raises:
            raise RuntimeError("raw post blew up")
        return True


@pytest.fixture
def shot(conn, tmp_path, monkeypatch):
    # a fresh schema has no slack_ts column (a comma is missing in db.SCHEMA); production has it
    if "slack_ts" not in {r[1] for r in conn.execute("PRAGMA table_info(injections)")}:
        conn.execute("ALTER TABLE injections ADD COLUMN slack_ts TEXT")
    conn.execute("INSERT INTO injections (id, inject_utc, stream, beam, dm, amp, sigma_ms, file_id,"
                 " created_utc, outcome, slack_ts) VALUES (7, '2026-10-13T20:00:00.000+00:00', 1, 100,"
                 " 300.0, 5.0, 5.0, 'inj_20261013_0007', '2026-10-13T20:00:00.000+00:00',"
                 " 'recovered', '5.5')")
    conn.commit()
    cfg = {"injection": {"replay": {"post": "always", "events_root": str(tmp_path / "EVENTS")}},
           "db": ":memory:"}
    paths = ir.replay_paths(7, ir.replay_cfg(cfg["injection"]), "inj_20261013_0007")
    paths["dir"].mkdir(parents=True)

    def fake_run(inj_id, dump_arg, rcfg, **kw):
        paths["png"].write_bytes(b"png")
        paths["raw_png"].write_bytes(b"raw")
        return paths["png"]
    monkeypatch.setattr(ir, "run_replay", fake_run)
    monkeypatch.setattr(ir, "cleanup_dump", lambda *a, **k: 0)
    return SimpleNamespace(conn=conn, cfg=cfg, paths=paths)


@pytest.mark.parametrize("mode", [inject_slack.MODE_SENT_THEN_UPDATE, inject_slack.MODE_SINGLE,
                                  inject_slack.MODE_SENT_THEN_EDIT])
def test_the_raw_figure_follows_the_masked_one_in_every_mode(shot, mode):
    poster = FakePoster(mode)
    dd.do_replay(shot.conn, shot.cfg, poster, 7, "/dumps/stream_1")
    assert [c[0] for c in poster.calls] == ["main", "raw"]
    raw = poster.calls[1]
    assert raw[1] == str(shot.paths["raw_png"])
    # the thread is the shot's own message: the fire-time ts, or the one the main post returned
    assert raw[2] == ("5.5" if mode == inject_slack.MODE_SENT_THEN_EDIT else "9.9")
    assert "inj_20261013_0007" in raw[3] and "before hella RFI flagging" in raw[3]


def test_the_raw_post_sees_the_ts_the_main_post_stored(shot):
    poster = FakePoster(inject_slack.MODE_SENT_THEN_UPDATE, main_ts="1234.5")
    dd.do_replay(shot.conn, shot.cfg, poster, 7, "/dumps/stream_1")
    assert poster.calls[1][2] == "1234.5"


def test_a_failing_raw_post_does_not_break_the_replay(shot, caplog):
    poster = FakePoster(inject_slack.MODE_SENT_THEN_UPDATE, raw_raises=True)
    with caplog.at_level("ERROR"):
        png = dd.do_replay(shot.conn, shot.cfg, poster, 7, "/dumps/stream_1")
    assert png == shot.paths["png"] and "raw replay post failed" in caplog.text
    assert shot.conn.execute("SELECT replay_posted FROM injections WHERE id=7").fetchone()[0] == 1


def test_no_raw_figure_means_no_raw_post(shot, monkeypatch):
    def no_raw(inj_id, dump_arg, rcfg, **kw):
        shot.paths["png"].write_bytes(b"png")
        return shot.paths["png"]
    monkeypatch.setattr(ir, "run_replay", no_raw)
    poster = FakePoster(inject_slack.MODE_SENT_THEN_UPDATE)
    dd.do_replay(shot.conn, shot.cfg, poster, 7, "/dumps/stream_1")
    assert [c[0] for c in poster.calls] == ["main"]


def test_no_raw_post_when_the_main_post_failed(shot):
    poster = FakePoster(inject_slack.MODE_SENT_THEN_UPDATE, main_ts=None)
    dd.do_replay(shot.conn, shot.cfg, poster, 7, "/dumps/stream_1")
    assert [c[0] for c in poster.calls] == ["main"]
