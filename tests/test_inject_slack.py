"""Injection Slack bot: migration, outcome enum, message text, figures.

Nothing here opens a socket. The dry-run test monkeypatches `requests` to
raise on import-time attribute access, so a poster that reached the network
would fail loudly instead of silently posting from a test run.
"""

import sqlite3

import pytest

from casm_t2 import db, inject_outcome as oc, inject_slack


# --- schema migration -------------------------------------------------------

def test_migration_is_idempotent_and_keeps_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    # A pre-migration injections table: the columns as of the deployed code.
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE injections (
            id INTEGER PRIMARY KEY, inject_utc TEXT NOT NULL,
            stream INTEGER NOT NULL, beam INTEGER NOT NULL, dm REAL NOT NULL,
            amp REAL NOT NULL, sigma_ms REAL NOT NULL, est_snr REAL,
            file_id TEXT NOT NULL, gate_t1 INTEGER, gate_t2 INTEGER,
            gate_trigger INTEGER, gate_ml INTEGER, matched_cluster INTEGER,
            rec_snr REAL, rec_dm REAL, fail_reason TEXT,
            created_utc TEXT NOT NULL);
    """)
    old.execute(
        "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
        " sigma_ms, est_snr, file_id, gate_t1, gate_t2, gate_trigger,"
        " rec_snr, rec_dm, created_utc) VALUES"
        " (659,'2026-09-09T04:58:50.327+00:00',0,40,150.0,5.0,5.0,19.821,"
        "'inj_x',1,1,1,25.8361,150.298,'2026-09-09T04:58:50.327+00:00')")
    old.commit()
    old.close()

    new_cols = {"target_snr", "sigma_n", "nchan_usable", "rec_width",
                "rec_beam", "rec_samp", "rec_lead_s", "slack_ts", "outcome"}

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(injections)")}
    assert new_cols <= cols
    conn.close()

    # Second connect must be a no-op, not a duplicate-column error.
    conn = db.connect(path)
    cols2 = {r[1] for r in conn.execute("PRAGMA table_info(injections)")}
    assert cols2 == cols
    row = conn.execute(
        "SELECT id, dm, rec_snr, outcome FROM injections").fetchall()
    assert row == [(659, 150.0, 25.8361, None)]
    conn.close()


# --- outcome classification -------------------------------------------------

@pytest.mark.parametrize("gates,fail,expect", [
    ((1, 1, 1), None, oc.RECOVERED),
    ((0, 0, 0), "t1_no_detection", oc.MISSED_T1),
    ((1, 0, 0), None, oc.MISSED_T2),
    ((1, 1, 0), "trigger_filters(tier=C,nbeam=1)", oc.MISSED_TRIGGER),
    ((None, None, None), "fifo_write_failed:[Errno 6]", oc.FIRE_FAILED),
])
def test_classify(gates, fail, expect):
    assert oc.classify(*gates, fail) == expect


def test_every_outcome_has_an_explanation():
    for outcome in oc.ALL:
        if outcome != oc.RECOVERED:
            assert oc.explain(outcome) == oc.EXPLANATIONS[outcome]
    assert "outcome=" in oc.explain("nonsense_value")


# --- message text -----------------------------------------------------------

RECOVERED_ROW = {
    "id": 659, "inject_utc": "2026-09-09T04:58:50.327+00:00", "stream": 0,
    "beam": 40, "dm": 150.0, "amp": 5.0, "sigma_ms": 5.0, "est_snr": 19.821,
    "target_snr": 20.0, "sigma_n": 41.3, "nchan_usable": 2600,
    "gate_t1": 1, "gate_t2": 1, "gate_trigger": 1, "rec_snr": 25.8361,
    "rec_dm": 150.298, "rec_width": 3, "rec_beam": 41, "rec_samp": 12345,
    "rec_lead_s": -12.5, "outcome": oc.RECOVERED, "fail_reason": None,
}

MISSED_ROW = {
    "id": 656, "inject_utc": "2026-09-01T00:54:00.702+00:00", "stream": 1,
    "beam": 102, "dm": 578.82, "amp": 38.47, "sigma_ms": 9.955,
    "est_snr": 333.7, "target_snr": None, "sigma_n": None,
    "gate_t1": 0, "gate_t2": 0, "gate_trigger": 0, "rec_snr": None,
    "rec_dm": None, "outcome": oc.MISSED_T1, "fail_reason": "t1_no_detection",
}


def test_sent_text_recovered_row():
    text = inject_slack.sent_text(RECOVERED_ROW)
    assert text.startswith("injection sent: `659`")
    assert "beam 40 (stream 0)" in text
    assert "DM 150.0 pc/cc" in text
    # FWHM = 2.355 sigma
    assert "sigma 5.0 ms (FWHM 11.8 ms)" in text
    assert "amp 5 counts" in text
    assert "live std 41.30" in text
    assert "target reported S/N 20.0" in text
    assert text.endswith("_awaiting recovery..._")


def test_sent_text_tolerates_missing_solver_fields():
    text = inject_slack.sent_text(MISSED_ROW)
    assert "live std n/a" in text
    assert "target reported S/N n/a" in text


def test_outcome_text_recovered():
    text = inject_slack.outcome_text(RECOVERED_ROW)
    assert text.startswith("recovered: S/N 25.8 at DM 150.3 (delta +0.3)")
    # ibox 3 -> 8 samples -> 8 * 1.048576 = 8.4 ms
    assert "width 2^3 = 8 samp = 8.4 ms" in text
    assert "beam 41 (+1)" in text
    assert "ratio reported/target 1.29" in text
    assert "lead -12.5 s" in text
    assert inject_slack.outcome_color(RECOVERED_ROW) == inject_slack.COLOR_RECOVERED


def test_width_conversion():
    assert inject_slack.width_ms(0) == (1, pytest.approx(1.048576))
    assert inject_slack.width_ms(5) == (32, pytest.approx(33.554432))


def test_outcome_text_missed():
    text = inject_slack.outcome_text(MISSED_ROW)
    assert text.startswith("NOT recovered: ")
    assert oc.EXPLANATIONS[oc.MISSED_T1] in text
    assert inject_slack.outcome_color(MISSED_ROW) == inject_slack.COLOR_MISSED


def test_streak_and_summary_text():
    streak = inject_slack.streak_text(5, [656, 655, 654], oc.MISSED_T1)
    assert "5 consecutive test injections missed" in streak
    assert "`656`" in streak

    text = inject_slack.summary_text([RECOVERED_ROW, MISSED_ROW], "2026-09-09")
    assert "2 injected, 1 recovered" in text
    assert "1 missed_t1" in text
    assert "median 1.29" in text


# --- figures ----------------------------------------------------------------

def test_summary_figures_written(tmp_path):
    paths = inject_slack.render_summary_figures(
        [RECOVERED_ROW, MISSED_ROW], tmp_path / "figs")
    assert [p.name for p in paths] == ["snr_recovery.png", "outcomes.png",
                                       "dm_accuracy.png"]
    assert all(p.is_file() and p.stat().st_size > 0 for p in paths)


def test_render_card(tmp_path):
    png = inject_slack.render_card(inject_slack.outcome_text(RECOVERED_ROW),
                                   inject_slack.COLOR_RECOVERED,
                                   tmp_path / "card.png")
    assert png.is_file() and png.stat().st_size > 0


# --- transport --------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch):
    """Any use of requests inside the poster becomes a hard failure."""
    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"the poster opened a socket: requests.{name}")
    monkeypatch.setitem(__import__("sys").modules, "requests", Boom())


def test_disabled_poster_is_a_noop(no_network):
    poster = inject_slack.SlackPoster(enabled=False)
    assert poster.post_sent(RECOVERED_ROW) is None
    assert poster.post_outcome(RECOVERED_ROW) is None
    assert poster.post_streak(5, [1], oc.MISSED_T1) is None


def test_dry_run_writes_files_and_never_posts(tmp_path, no_network):
    out = tmp_path / "dry"
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=out)
    ts = poster.post_sent(RECOVERED_ROW)
    assert ts and ts.startswith("dry-")
    poster.post_outcome(RECOVERED_ROW)
    poster.post_streak(5, [656], oc.MISSED_T1)
    poster.post_summary([RECOVERED_ROW, MISSED_ROW], "2026-09-09", out)

    # The numeric prefix keeps the files in posting order.
    names = sorted(p.name for p in out.glob("*.txt"))
    assert [n.split("_", 1)[1] for n in names] == [
        "sent_659.txt", "outcome_659.txt", "streak_5.txt",
        "summary_2026-09-09.txt"]
    assert "recovered: S/N 25.8" in (out / names[1]).read_text()
    assert (out / "snr_recovery.png").is_file()


def test_miss_streak_reads_the_ledger(conn):
    now = "2026-09-09T00:00:00.000+00:00"
    for rid, outcome in [(1, oc.RECOVERED), (2, oc.MISSED_T1),
                         (3, oc.MISSED_TRIGGER), (4, oc.MISSED_T1)]:
        conn.execute(
            "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc, outcome)"
            " VALUES (?,?,0,4,150.0,5.0,5.0,'f',?,?)", (rid, now, now, outcome))
    conn.commit()
    n, ids, latest = inject_slack.miss_streak(conn)
    assert n == 3
    assert ids == [4, 3, 2]
    assert latest == oc.MISSED_T1


def test_check_streak_posts_at_multiples(tmp_path, conn, no_network):
    now = "2026-09-09T00:00:00.000+00:00"
    for rid in range(1, 6):
        conn.execute(
            "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
            " sigma_ms, file_id, created_utc, outcome)"
            " VALUES (?,?,0,4,150.0,5.0,5.0,'f',?,?)",
            (rid, now, now, oc.MISSED_T1))
    conn.commit()
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path,
                                      streak_every=5)
    assert inject_slack.check_streak(conn, poster) == 5
    assert list(tmp_path.glob("*streak_5.txt"))
