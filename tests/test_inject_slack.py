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

    new_cols = {"target_snr", "inject_snr", "sigma_n", "nchan_usable",
                "rec_width", "rec_beam", "rec_samp", "rec_lead_s", "slack_ts",
                "outcome"}

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
    "target_snr": 25.0, "inject_snr": 12.0, "sigma_n": 62.39,
    "nchan_usable": 2600, "file_id": "inj_20260909_221751_b090",
    "rec_offset_arcsec": 15.0, "rec_name": None,
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
        f"injection 661 sent: beam 90, DM 300, "
        f"FWHM 11.8{NBSP}ms, injected S/N 20")
    assert text.endswith("_awaiting recovery..._")
    # sigma, the live std, the stream and the raw counts are all gone
    assert "sigma" not in text
    assert "std" not in text
    assert "stream" not in text
    assert "counts" not in text


def test_injected_snr_prefers_the_generator_estimate():
    """est_snr describes the pulse as WRITTEN; inject_snr is what was asked for."""
    assert inject_slack.injected_snr(RECOVERED_ROW) == pytest.approx(19.662)
    assert inject_slack.injected_snr(
        dict(RECOVERED_ROW, est_snr=None)) == pytest.approx(12.0)
    assert inject_slack.injected_snr({"est_snr": None}) is None


def test_no_slack_text_says_target_or_expected():
    for text in (inject_slack.sent_text(RECOVERED_ROW),
                 inject_slack.summary_text([RECOVERED_ROW], "2026-09-09")):
        assert "target" not in text.lower()
        assert "expected" not in text.lower()


def test_sent_text_says_n_a_with_nothing_to_go_on():
    assert "injected S/N n/a" in inject_slack.sent_text(
        {"id": 1, "beam": 4, "stream": 0, "dm": 100.0, "amp": 5.0,
         "sigma_ms": 5.0})


def test_injected_fwhm_is_2355_sigma():
    assert inject_slack.injected_fwhm_ms(RECOVERED_ROW) == pytest.approx(11.775)
    assert inject_slack.injected_fwhm_ms({"sigma_ms": None}) is None


def test_outcome_text_recovered_is_the_dsa_shape():
    text = inject_slack.outcome_text(RECOVERED_ROW, "http://host:8050")
    # ratio 35.5246/19.662 = 1.81; ibox 4 kernel FWHM 11.5 ms, not 2^4=16.8
    assert text == (
        "recovered -> <http://host:8050/injections/plot/"
        "inj_20260909_221751_b090|inj_20260909_221751_b090> | "
        "SNR 35.5 (ratio 1.81) | DM 299.8 (delta -0.2) | "
        f"beam 90 | width 11.5{NBSP}ms (ibox 4)")
    assert inject_slack.outcome_color(RECOVERED_ROW) == inject_slack.COLOR_RECOVERED


def test_outcome_link_prefers_the_event_page_when_the_cluster_triggered():
    """A cluster with a candname has a dump and a full event page."""
    row = dict(RECOVERED_ROW, rec_name="260909abcdef")
    assert inject_slack.outcome_link(row, "http://host:8050") == (
        "<http://host:8050/event/260909abcdef|260909abcdef>")
    assert "/event/260909abcdef|" in inject_slack.outcome_text(
        row, "http://host:8050")


def test_outcome_link_defaults_to_the_local_web_base():
    assert inject_slack.DEFAULT_WEB_BASE == "http://127.0.0.1:8050"
    assert inject_slack.outcome_link(RECOVERED_ROW).startswith(
        "<http://127.0.0.1:8050/injections/plot/")


def test_same_beam_needs_no_offset():
    """Landing where it was put is the ordinary case: no number needed."""
    assert inject_slack.recovered_beam_text(RECOVERED_ROW) == "beam 90"
    assert "offset" not in inject_slack.outcome_text(RECOVERED_ROW)


def test_a_different_beam_carries_the_offset():
    row = dict(RECOVERED_ROW, rec_beam=92, rec_offset_arcsec=3600.0)
    assert inject_slack.recovered_beam_text(row) == (
        f"beam 92 (offset 3600{NBSP}arcsec)")
    assert f"beam 92 (offset 3600{NBSP}arcsec)" in inject_slack.outcome_text(row)


def test_a_different_beam_without_a_pointing_table():
    row = dict(RECOVERED_ROW, rec_beam=92, rec_offset_arcsec=None)
    assert inject_slack.recovered_beam_text(row) == "beam 92 (offset n/a)"


def test_no_recovered_beam_at_all():
    row = dict(RECOVERED_ROW, rec_beam=None)
    assert inject_slack.recovered_beam_text(row) == "beam n/a"


def test_kernel_width_is_last_so_it_is_easy_to_drop():
    text = inject_slack.outcome_text(RECOVERED_ROW)
    assert text.split(" | ")[-1] == f"width 11.5{NBSP}ms (ibox 4)"


def test_outcome_text_missed_prints_the_detail_reconcile_wrote():
    detail = ("lost at T1: no cluster in the window [-40 s, +90 s] "
              "in beam 102 (+-2) at DM 579 (+-87)")
    row = dict(MISSED_ROW, fail_reason=detail)
    assert inject_slack.outcome_text(row) == "NOT recovered: " + detail
    assert inject_slack.outcome_color(row) == inject_slack.COLOR_MISSED


@pytest.mark.parametrize("detail", [
    "lost at T1: no cluster in the window [-40 s, +90 s] in beam 200 (+-2) at DM 500 (+-75)",
    "lost at T2 filters: cluster at S/N 15.8 below tier B (18)",
    "lost at T2 filters: cluster tagged dm_floor by the low-DM storm veto",
    "lost at T2 filters: cluster tagged occupancy:34 by the beam-occupancy veto",
    "lost at T2 filters: cluster peaked in vetoed beam 200",
    "lost at T2 filters: cluster at DM 12.4 below the floor (20)",
])
def test_each_miss_reason_is_printed_verbatim(detail):
    row = dict(MISSED_ROW, fail_reason=detail)
    assert inject_slack.outcome_text(row) == "NOT recovered: " + detail
    # the injection tag is by design and must never be the reported reason
    assert "injection" not in detail


def test_legacy_rows_still_read_sensibly():
    """Rows written before reconcile() stored a detail."""
    row = dict(MISSED_ROW, fail_reason="t1_no_detection")
    assert inject_slack.outcome_text(row) == (
        "NOT recovered: lost at T1: no cluster in the reconcile window")
    row = dict(MISSED_ROW, outcome=oc.MISSED_TRIGGER,
               fail_reason="trigger_filters(tier=C,nbeam=1)")
    assert "trigger_filters(tier=C,nbeam=1)" in inject_slack.outcome_text(row)


def test_missing_detail_falls_back_to_the_enum_phrase():
    row = dict(MISSED_ROW, fail_reason=None)
    assert inject_slack.outcome_text(row) == (
        "NOT recovered: " + oc.EXPLANATIONS[oc.MISSED_T1])


def test_fire_failed_is_not_phrased_as_a_miss():
    row = dict(MISSED_ROW, outcome=oc.FIRE_FAILED,
               fail_reason="fifo_write_failed:[Errno 6] No such device")
    assert inject_slack.outcome_text(row) == (
        "injection not fired: FIFO write failed")
    assert inject_slack.outcome_color(row) == inject_slack.COLOR_NEUTRAL


def test_streak_uses_the_same_phrases():
    text = inject_slack.streak_text(5, [656], oc.MISSED_TRIGGER)
    assert text.endswith(
        "latest loss stage: " + oc.EXPLANATIONS[oc.MISSED_TRIGGER])


def test_streak_and_summary_text():
    streak = inject_slack.streak_text(5, [656, 655, 654], oc.MISSED_T1)
    assert "5 consecutive test injections missed" in streak
    assert "`656`" in streak

    text = inject_slack.summary_text([RECOVERED_ROW, MISSED_ROW], "2026-09-09")
    assert "2 injected, 1 recovered" in text
    assert "1 missed_t1" in text
    assert "recovered/injected S/N: median 1.81" in text  # 35.5246 / 19.662


# --- figures ----------------------------------------------------------------

def test_summary_figures_written(tmp_path):
    paths = inject_slack.render_summary_figures(
        [RECOVERED_ROW, MISSED_ROW], tmp_path / "figs")
    assert [p.name for p in paths] == ["snr_recovery.png", "outcomes.png"]
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
    assert "SNR 35.5" in (out / names[1]).read_text()
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


def test_summary_posts_one_message_with_the_figures_in_its_thread(tmp_path,
                                                                  monkeypatch):
    """One top-level text message; each figure a reply carrying its ts."""
    poster = inject_slack.SlackPoster(enabled=True)
    calls = {"posts": [], "files": []}

    def fake_post(text, attachments=None, thread_ts=None):
        calls["posts"].append((text, thread_ts))
        return "1757000000.001"

    def fake_post_file(png, title, thread_ts=None):
        calls["files"].append((png.name, thread_ts))
        return True

    monkeypatch.setattr(poster, "_post", fake_post)
    monkeypatch.setattr(poster, "_post_file", fake_post_file)
    ts = poster.post_summary([RECOVERED_ROW, MISSED_ROW], "2026-09-09", tmp_path)

    assert ts == "1757000000.001"
    # exactly one top-level message, itself not a reply
    assert len(calls["posts"]) == 1
    assert calls["posts"][0][1] is None
    assert calls["posts"][0][0].startswith("test injections: 24 h summary")
    # both figures replied into that message's thread
    assert [n for n, _ in calls["files"]] == ["snr_recovery.png", "outcomes.png"]
    assert all(t == ts for _, t in calls["files"])
