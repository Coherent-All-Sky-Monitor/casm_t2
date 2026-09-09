"""Injection Slack bot: migration, outcome enum, message text, figures.

Nothing here opens a socket. The dry-run test monkeypatches `requests` to
raise on import-time attribute access, so a poster that reached the network
would fail loudly instead of silently posting from a test run.
"""

import sqlite3

import pytest

from casm_t2 import db, inject_calib, inject_outcome as oc, inject_slack


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
    # sigma_ms stays: the ledger keeps the Gaussian sigma even though every
    # user-facing number is now FWHM.
    assert "sigma_ms" in cols
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

# Modelled on id 661: sigma 5 ms in the ledger is FWHM 11.8 ms, and the
# matched trial ibox 4 has a kernel FWHM of 11.5 ms.
RECOVERED_ROW = {
    "id": 661, "inject_utc": "2026-09-09T22:17:55.642+00:00", "stream": 1,
    "beam": 90, "dm": 300.0, "amp": 8.0, "sigma_ms": 5.0, "est_snr": 19.662,
    "target_snr": 25.0, "sigma_n": 62.39, "nchan_usable": 2600,
    "gate_t1": 1, "gate_t2": 1, "gate_trigger": 1, "rec_snr": 35.5246,
    "rec_dm": 299.818, "rec_width": 4, "rec_beam": 90, "rec_samp": 12345,
    "rec_lead_s": -19.32, "outcome": oc.RECOVERED, "fail_reason": None,
}

MISSED_ROW = {
    "id": 656, "inject_utc": "2026-09-01T00:54:00.702+00:00", "stream": 1,
    "beam": 102, "dm": 578.82, "amp": 38.47, "sigma_ms": 9.955,
    "est_snr": 333.7, "target_snr": None, "sigma_n": None,
    "gate_t1": 0, "gate_t2": 0, "gate_trigger": 0, "rec_snr": None,
    "rec_dm": None, "outcome": oc.MISSED_T1, "fail_reason": "t1_no_detection",
}

NBSP = inject_slack.NBSP


def test_sent_text_is_the_short_form():
    text = inject_slack.sent_text(RECOVERED_ROW)
    assert text.split("\n")[0] == (
        f"injection 661 sent: beam 90 (stream 1), DM 300, "
        f"FWHM 11.8{NBSP}ms, amp 8{NBSP}counts, expected S/N 25")
    assert text.endswith("_awaiting recovery..._")
    # sigma and the live std are deliberately gone from user-facing text
    assert "sigma" not in text
    assert "std" not in text


def test_expected_snr_falls_back_to_est_snr_with_a_tilde():
    """Legacy amp_range rows have no target_snr: scale est_snr by the ratio."""
    text = inject_slack.sent_text(MISSED_ROW)
    # est_snr 333.7 at FWHM 23.44 ms -> ratio 1.22 -> ~407
    ratio = inject_calib.rec_per_true(9.955 * 2.355)
    assert f"expected S/N ~{333.7 * ratio:.0f}" in text
    snr, approx = inject_slack.expected_snr(MISSED_ROW)
    assert approx is True and snr == pytest.approx(333.7 * ratio)


def test_expected_snr_is_the_target_when_the_solver_set_one():
    snr, approx = inject_slack.expected_snr(RECOVERED_ROW)
    assert snr == 25.0 and approx is False


def test_no_slack_text_says_target():
    for text in (inject_slack.sent_text(RECOVERED_ROW),
                 inject_slack.summary_text([RECOVERED_ROW], "2026-09-09")):
        assert "target" not in text.lower()


def test_sent_text_says_n_a_with_nothing_to_go_on():
    assert "expected S/N n/a" in inject_slack.sent_text(
        {"id": 1, "beam": 4, "stream": 0, "dm": 100.0, "amp": 5.0,
         "sigma_ms": 5.0})


def test_injected_fwhm_is_2355_sigma():
    assert inject_slack.injected_fwhm_ms(RECOVERED_ROW) == pytest.approx(11.775)
    assert inject_slack.injected_fwhm_ms({"sigma_ms": None}) is None


def test_outcome_text_recovered_uses_the_kernel_fwhm():
    text = inject_slack.outcome_text(RECOVERED_ROW)
    # ibox 4 is a 16-sample trial, but the kernel that matched is 11 samples
    # wide: 11.5 ms, not 16.8 ms.
    assert text == (f"recovered: S/N 35.5, DM 299.8, "
                    f"width 11.5{NBSP}ms (ibox 4)")
    assert inject_slack.outcome_color(RECOVERED_ROW) == inject_slack.COLOR_RECOVERED


def test_outcome_text_carries_nothing_else():
    text = inject_slack.outcome_text(RECOVERED_ROW)
    for word in ("lead", "ratio", "beam", "samp", "delta"):
        assert word not in text


def test_outcome_text_missed():
    text = inject_slack.outcome_text(MISSED_ROW)
    assert text == "NOT recovered: not detected by hella (T1)"
    assert inject_slack.outcome_color(MISSED_ROW) == inject_slack.COLOR_MISSED


@pytest.mark.parametrize("outcome,expect", [
    (oc.MISSED_T1, "NOT recovered: not detected by hella (T1)"),
    (oc.MISSED_T2, "NOT recovered: dropped by T2 clustering"),
    (oc.MISSED_TRIGGER, "NOT recovered: dropped by T2 filter criteria"),
])
def test_miss_lines_are_short_and_name_only_the_stage(outcome, expect):
    text = inject_slack.outcome_text(dict(MISSED_ROW, outcome=outcome))
    assert text == expect
    # no mechanism, no tier names, no parentheticals beyond "(T1)"
    for word in ("tier", "DM floor", "beam veto", "coincid", "window"):
        assert word not in text


def test_fire_failed_is_not_phrased_as_a_miss():
    row = dict(MISSED_ROW, outcome=oc.FIRE_FAILED,
               fail_reason="fifo_write_failed:[Errno 6] No such device")
    assert inject_slack.outcome_text(row) == (
        "injection not fired: FIFO write failed")
    assert inject_slack.outcome_color(row) == inject_slack.COLOR_NEUTRAL


def test_streak_uses_the_same_short_phrase():
    text = inject_slack.streak_text(5, [656], oc.MISSED_TRIGGER)
    assert text.endswith("latest loss stage: dropped by T2 filter criteria")


def test_streak_and_summary_text():
    streak = inject_slack.streak_text(5, [656, 655, 654], oc.MISSED_T1)
    assert "5 consecutive test injections missed" in streak
    assert "`656`" in streak

    text = inject_slack.summary_text([RECOVERED_ROW, MISSED_ROW], "2026-09-09")
    assert "2 injected, 1 recovered" in text
    assert "1 missed_t1" in text
    assert "reported/expected S/N: median 1.42" in text   # 35.5246 / 25.0


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
        "sent_661.txt", "outcome_661.txt", "streak_5.txt",
        "summary_2026-09-09.txt"]
    assert "recovered: S/N 35.5" in (out / names[1]).read_text()
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
