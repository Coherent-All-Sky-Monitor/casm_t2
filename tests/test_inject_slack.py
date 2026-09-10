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

@pytest.mark.parametrize("gates,fail,trials,expect", [
    ((1, 1, 1), None, None, oc.RECOVERED),
    # a matching cluster is recovered at ANY S/N: the trigger gate is policy
    ((1, 1, 0), None, None, oc.RECOVERED),
    ((0, 0, 0), None, 0, oc.MISSED_T1),
    ((0, 0, 0), None, None, oc.MISSED_T1),        # file unavailable
    ((0, 0, 0), None, 7, oc.MISSED_T2),
    ((None, None, None), "fifo_write_failed:[Errno 6]", None, oc.FIRE_FAILED),
])
def test_classify(gates, fail, trials, expect):
    assert oc.classify(*gates, fail, trials) == expect


def test_the_trigger_gate_never_makes_an_outcome():
    """S/N 15.8 below tier B, found by the search: recovered."""
    assert oc.classify(1, 1, 0, None, None) == oc.RECOVERED
    assert "missed_trigger" not in oc.ALL
    assert not hasattr(oc, "MISSED_TRIGGER")


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
    "lost at T1: no matching trial in beam 200 (+-2) within the window at DM 500 (+-75)",
    "lost at T2: 7 matching T1 trials (best S/N 9.2) but no cluster formed (min 5 members)",
    "lost at T1: no cluster in the window in beam 200 (+-2) at DM 500 (+-75) (T1 file unavailable (cands_x.dat.3 not found))",
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
    row = dict(MISSED_ROW, outcome=oc.MISSED_T2, fail_reason=None)
    assert inject_slack.outcome_text(row) == (
        "NOT recovered: " + oc.EXPLANATIONS[oc.MISSED_T2])


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
    text = inject_slack.streak_text(5, [656], oc.MISSED_T2)
    assert text.endswith(
        "latest loss stage: " + oc.EXPLANATIONS[oc.MISSED_T2])


def test_streak_and_summary_text():
    streak = inject_slack.streak_text(5, [656, 655, 654], oc.MISSED_T1)
    assert "5 consecutive test injections missed" in streak
    assert "`656`" in streak

    text = inject_slack.summary_text([RECOVERED_ROW, MISSED_ROW], "2026-09-09")
    assert "2 injected, 1 recovered" in text
    assert "missed: 1 missed by hella (T1)" in text
    assert "recovered/injected S/N: median 1.81" in text  # 35.5246 / 19.662


# --- figures ----------------------------------------------------------------

def test_every_outcome_has_a_plain_label():
    """The enum values stay the DB/wire strings; these are display only."""
    assert oc.LABELS == {
        oc.RECOVERED: "recovered",
        oc.MISSED_T1: "missed by hella (T1)",
        oc.MISSED_T2: "T2 miss (no cluster formed)",
        oc.FIRE_FAILED: "not fired",
    }
    assert set(oc.LABELS) == set(oc.ALL)
    for outcome in oc.ALL:
        assert "_" not in oc.label(outcome)
    assert oc.label("something_new") == "something_new"


def test_summary_missed_line_uses_the_plain_labels():
    rows = [dict(MISSED_ROW, outcome=oc.MISSED_T1),
            dict(MISSED_ROW, outcome=oc.MISSED_T2)]
    text = inject_slack.summary_text(rows, "2026-09-09")
    assert ("missed: 1 missed by hella (T1); "
            "1 T2 miss (no cluster formed)") in text
    for enum_name in ("missed_t1", "missed_t2"):
        assert enum_name not in text


def test_summary_figures_written(tmp_path):
    paths = inject_slack.render_summary_figures(
        [RECOVERED_ROW, MISSED_ROW], tmp_path / "figs")
    assert [p.name for p in paths] == ["snr_recovery.png"]
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
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=out,
                                      mode=inject_slack.MODE_SENT_THEN_EDIT)
    ts = poster.post_sent(RECOVERED_ROW)
    assert ts and ts.startswith("dry-")
    poster.post_outcome(RECOVERED_ROW)
    poster.post_streak(5, [656], oc.MISSED_T1)
    poster.post_summary([RECOVERED_ROW, MISSED_ROW], "2026-09-09", out)

    # The numeric prefix keeps the files in posting order.
    names = sorted(p.name for p in out.glob("*.txt"))
    assert [n.split("_", 1)[1] for n in names] == [
        "inject_661_sent.txt", "outcome_661.txt", "streak_5.txt",
        "summary_2026-09-09.txt"]
    assert "SNR 35.5" in (out / names[1]).read_text()
    # the summary figure is behind slack.summary_figures, off by default
    assert not (out / "snr_recovery.png").exists()


def test_miss_streak_reads_the_ledger(conn):
    now = "2026-09-09T00:00:00.000+00:00"
    for rid, outcome in [(1, oc.RECOVERED), (2, oc.MISSED_T1),
                         (3, oc.MISSED_T2), (4, oc.MISSED_T1)]:
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


def test_summary_posts_one_message_with_the_figure_in_its_thread(tmp_path,
                                                                 monkeypatch):
    """One top-level text message; the figure a reply carrying its ts."""
    poster = inject_slack.SlackPoster(
        enabled=True, icfg={"slack": {"summary_figures": True}})
    calls = {"posts": [], "files": []}

    def fake_post(text, attachments=None, thread_ts=None):
        calls["posts"].append((text, thread_ts))
        return "1757000000.001"

    def fake_post_file(png, title, thread_ts=None, comment=None,
                       want_ts=False):
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
    # the figure replied into that message's thread
    assert [n for n, _ in calls["files"]] == ["snr_recovery.png"]
    assert all(t == ts for _, t in calls["files"])


# --- IB subtraction state in the messages ------------------------------------

def test_sent_line_is_silent_when_subtraction_is_on():
    """The standing state adds nothing."""
    text = inject_slack.sent_text(dict(RECOVERED_ROW, sub_incoh=1))
    assert "IB sub" not in text
    assert inject_slack.sent_text(
        dict(RECOVERED_ROW, sub_incoh=None)).count("IB sub") == 0


def test_sent_line_calls_out_subtraction_off():
    text = inject_slack.sent_text(dict(RECOVERED_ROW, sub_incoh=0))
    assert text.split("\n")[0].endswith("injected S/N 20 (IB sub off)")


def test_summary_splits_the_ratio_line_when_a_day_mixes_states():
    on = dict(RECOVERED_ROW, sub_incoh=1, rec_snr=25.6)      # ratio 1.30
    off = dict(RECOVERED_ROW, sub_incoh=0, rec_snr=40.9)     # ratio 2.08
    text = inject_slack.summary_text([on, off], "2026-09-10")
    assert "recovered/injected S/N (IB sub on): median 1.30" in text
    assert "recovered/injected S/N (IB sub off): median 2.08" in text


def test_summary_keeps_one_ratio_line_for_a_single_state():
    rows = [dict(RECOVERED_ROW, sub_incoh=1)]
    text = inject_slack.summary_text(rows, "2026-09-10")
    assert "recovered/injected S/N: median" in text
    assert "IB sub" not in text


# --- configurable DM bins ----------------------------------------------------

def test_default_dm_bins_follow_the_widened_range():
    assert inject_slack.dm_bin_labels() == [
        "DM < 100", "DM 100-300", "DM 300-500", "DM 500-700", "DM 700-900",
        "DM > 900"]


@pytest.mark.parametrize("dm,label", [
    (50.0, "DM < 100"), (100.0, "DM 100-300"), (299.9, "DM 100-300"),
    (300.0, "DM 300-500"), (699.0, "DM 500-700"), (899.0, "DM 700-900"),
    (950.0, "DM > 900"), (None, "DM > 900"),
])
def test_dm_bucket_edges_are_half_open(dm, label):
    assert inject_slack.dm_bucket(dm)[0] == label


def test_dm_bins_come_from_config():
    icfg = {"summary_dm_bins": [200.0, 600.0]}
    assert inject_slack.dm_bin_labels(icfg) == [
        "DM < 200", "DM 200-600", "DM > 600"]
    assert inject_slack.dm_bucket(400.0, icfg)[0] == "DM 200-600"
    assert inject_slack.dm_bucket(50.0, icfg)[0] == "DM < 200"


def test_summary_per_dm_line_uses_the_configured_bins():
    rows = [dict(RECOVERED_ROW, dm=150.0), dict(RECOVERED_ROW, dm=850.0)]
    text = inject_slack.summary_text(rows, "2026-09-10")
    assert "per DM: DM 100-300: 1/1 | DM 700-900: 1/1" in text


def test_the_default_dm_bins_all_get_distinct_styles():
    """Colour AND marker carry identity, so two bins must not share a style."""
    labels = inject_slack.dm_bin_labels()
    styles = [inject_slack.DM_STYLES[i % len(inject_slack.DM_STYLES)]
              for i in range(len(labels))]
    assert len(set(styles)) == len(labels)


# --- single-message mode -----------------------------------------------------

def test_sent_then_update_is_the_default_mode():
    assert inject_slack.SlackPoster().mode == inject_slack.MODE_SENT_THEN_UPDATE
    import yaml
    with open("config/t2d.yaml") as fh:
        scfg = yaml.safe_load(fh)["injection"]["slack"]
    assert scfg["mode"] == "sent_then_update"
    assert scfg["summary_figures"] is False


def test_an_unknown_mode_falls_back_to_the_default(caplog):
    with caplog.at_level("WARNING"):
        poster = inject_slack.SlackPoster(mode="whatever")
    assert poster.mode == inject_slack.MODE_SENT_THEN_UPDATE
    assert "not one of" in caplog.text


def test_single_mode_posts_nothing_at_fire_time(tmp_path, no_network):
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path,
                                      mode=inject_slack.MODE_SINGLE)
    assert poster.post_sent(RECOVERED_ROW) is None
    assert poster.post_outcome(RECOVERED_ROW) is None
    assert list(tmp_path.glob("*.txt")) == []


def test_sent_then_update_posts_the_sent_line_at_fire_time(tmp_path,
                                                           no_network):
    """The channel must show a shot is in flight before the result exists."""
    import json
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path)
    assert poster.post_sent(RECOVERED_ROW) is not None
    written, = list(tmp_path.glob("*_inject_661_sent.txt"))
    payload = json.loads(written.read_text())
    assert payload["text"].startswith("injection 661 sent:")
    assert payload["text"].endswith("_awaiting recovery..._")
    assert "attachments" not in payload      # nothing to colour yet


def test_the_caption_is_the_sent_line_then_the_outcome_line():
    text = inject_slack.injection_text(RECOVERED_ROW, "http://host:8050")
    first, second = text.split("\n")
    assert first == (f"injection 661 sent: beam 90, DM 300, "
                     f"FWHM 11.8{NBSP}ms, injected S/N 20")
    assert second.startswith("recovered -> <http://host:8050/injections/plot/")
    assert "SNR 35.5 (ratio 1.81)" in second
    # nothing is being awaited by the time this posts
    assert "awaiting recovery" not in text


def test_the_caption_on_a_miss():
    row = dict(MISSED_ROW, fail_reason="lost at T1: no matching trial")
    text = inject_slack.injection_text(row)
    assert text.split("\n")[1] == "NOT recovered: lost at T1: no matching trial"


def _fake_transport(poster, monkeypatch, *, upload="F123", post_err=None,
                    shared=None):
    """Record what the poster would send, without a socket in sight."""
    seen = {"posts": [], "uploads": [], "shares": []}

    def fake_upload(png, title):
        seen["uploads"].append((str(png), title))
        return upload

    def fake_post(text, attachments=None, thread_ts=None, want_error=False):
        seen["posts"].append({"text": text, "attachments": attachments})
        err = post_err if attachments else None
        ts = None if err else f"ts{len(seen['posts'])}"
        return (ts, err) if want_error else ts

    def fake_post_file(png, title, thread_ts=None, comment=None,
                       want_ts=False):
        seen["shares"].append({"png": str(png), "comment": comment})
        return shared

    monkeypatch.setattr(poster, "_upload_unshared", fake_upload)
    monkeypatch.setattr(poster, "_post", fake_post)
    monkeypatch.setattr(poster, "_post_file", fake_post_file)
    return seen


def test_the_inline_payload_shape(tmp_path, monkeypatch):
    """text = the sent line; one coloured bar holding the result then image."""
    poster = inject_slack.SlackPoster(enabled=True,
                                      mode=inject_slack.MODE_SINGLE)
    seen = _fake_transport(poster, monkeypatch)
    ts = poster.post_injection(RECOVERED_ROW, tmp_path / "inj661.png")

    assert ts == "ts1"
    assert seen["uploads"] == [(str(tmp_path / "inj661.png"), "inj661")]
    payload = seen["posts"][0]
    # the notification reads as the sent line, not as JSON or a result
    assert payload["text"] == inject_slack.sent_text(
        RECOVERED_ROW, None).split("\n")[0]
    assert "\n" not in payload["text"]

    att, = payload["attachments"]
    assert att["color"] == inject_slack.COLOR_RECOVERED
    section, image = att["blocks"]
    assert section["type"] == "section"
    assert section["text"]["type"] == "mrkdwn"
    assert section["text"]["text"] == inject_slack.outcome_text(
        RECOVERED_ROW, poster.web_base)
    assert image == {"type": "image", "slack_file": {"id": "F123"},
                     "alt_text": "injection replay"}


@pytest.mark.parametrize("outcome,color", [
    (oc.RECOVERED, inject_slack.COLOR_RECOVERED),
    (oc.MISSED_T1, inject_slack.COLOR_MISSED),
    (oc.MISSED_T2, inject_slack.COLOR_MISSED),
    (oc.FIRE_FAILED, inject_slack.COLOR_NEUTRAL),
])
def test_the_bar_colour_follows_the_outcome(outcome, color):
    row = dict(RECOVERED_ROW, outcome=outcome)
    att, = inject_slack.injection_attachments(row, "F1")
    assert att["color"] == color


def test_no_image_block_without_an_upload():
    att, = inject_slack.injection_attachments(RECOVERED_ROW, None)
    assert [b["type"] for b in att["blocks"]] == ["section"]


def test_a_failed_upload_still_posts_the_coloured_bar(tmp_path, monkeypatch,
                                                     caplog):
    poster = inject_slack.SlackPoster(enabled=True,
                                      mode=inject_slack.MODE_SINGLE)
    seen = _fake_transport(poster, monkeypatch, upload=None)
    with caplog.at_level("INFO"):
        ts = poster.post_injection(RECOVERED_ROW, tmp_path / "x.png")
    assert ts == "ts1"
    att, = seen["posts"][0]["attachments"]
    assert [b["type"] for b in att["blocks"]] == ["section"]   # no image
    assert att["color"] == inject_slack.COLOR_RECOVERED
    assert "form=bar" in caplog.text
    assert seen["shares"] == []


def test_rejected_blocks_fall_back_to_the_caption_form(tmp_path, monkeypatch,
                                                       caplog):
    """invalid_blocks: the app may not reference slack_file images."""
    poster = inject_slack.SlackPoster(enabled=True,
                                      mode=inject_slack.MODE_SINGLE)
    seen = _fake_transport(poster, monkeypatch, post_err="invalid_blocks",
                           shared="ts_shared")
    with caplog.at_level("INFO"):
        ts = poster.post_injection(RECOVERED_ROW, tmp_path / "x.png")
    assert ts == "ts_shared"
    assert seen["shares"][0]["comment"] == inject_slack.injection_text(
        RECOVERED_ROW, poster.web_base, None)
    assert "invalid_blocks" in caplog.text
    assert "form=caption" in caplog.text


def test_everything_failing_still_posts_the_two_lines(tmp_path, monkeypatch,
                                                     caplog):
    poster = inject_slack.SlackPoster(enabled=True,
                                      mode=inject_slack.MODE_SINGLE)
    seen = _fake_transport(poster, monkeypatch, post_err="invalid_blocks",
                           shared=None)
    with caplog.at_level("INFO"):
        ts = poster.post_injection(RECOVERED_ROW, tmp_path / "x.png")
    # the last post carries no attachments, so the fake gives it a ts
    assert ts is not None
    assert seen["posts"][-1]["attachments"] is None
    assert seen["posts"][-1]["text"] == inject_slack.injection_text(
        RECOVERED_ROW, poster.web_base, None)
    assert "form=text" in caplog.text


def test_no_png_at_all_posts_the_bar_then_text(monkeypatch, caplog):
    poster = inject_slack.SlackPoster(enabled=True,
                                      mode=inject_slack.MODE_SINGLE)
    seen = _fake_transport(poster, monkeypatch)
    ts = poster.post_injection(RECOVERED_ROW, None)
    assert ts == "ts1"
    assert seen["uploads"] == []                     # nothing to upload
    att, = seen["posts"][0]["attachments"]
    assert [b["type"] for b in att["blocks"]] == ["section"]


def test_single_mode_dry_run_writes_the_payload(tmp_path):
    import json
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path,
                                      mode=inject_slack.MODE_SINGLE)
    poster.post_injection(RECOVERED_ROW, "/events/inj661/inj661.png")
    written, = list(tmp_path.glob("*_inject_661.txt"))
    body = written.read_text()
    payload = json.loads(body.split("\n\n[uploaded")[0])
    assert payload["text"].startswith("injection 661 sent:")
    att, = payload["attachments"]
    assert att["color"] == inject_slack.COLOR_RECOVERED
    assert [b["type"] for b in att["blocks"]] == ["section", "image"]
    assert "/events/inj661/inj661.png" in body


def test_summary_figures_are_off_by_default(tmp_path, monkeypatch, no_network):
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path,
                                      mode=inject_slack.MODE_SINGLE)
    monkeypatch.setattr(
        inject_slack, "render_summary_figures",
        lambda *a, **k: pytest.fail("rendered a figure with the flag off"))
    poster.post_summary([RECOVERED_ROW], "2026-09-10", tmp_path)
    assert list(tmp_path.glob("*summary*.txt"))


# --- sent_then_update: the card is completed in place ------------------------

SENT_ROW = dict(RECOVERED_ROW, slack_ts="1757000000.661")


def _fake_update_transport(poster, monkeypatch, *, upload="F123",
                           update_err=None, second_err=None, post_ts="tsnew"):
    seen = {"updates": [], "posts": [], "shares": []}

    def fake_update(ts, text, attachments=None, want_error=False):
        seen["updates"].append({"ts": ts, "text": text,
                                "attachments": attachments})
        err = update_err if len(seen["updates"]) == 1 else second_err
        got = None if err else ts
        return (got, err) if want_error else got

    def fake_post(text, attachments=None, thread_ts=None, want_error=False):
        seen["posts"].append({"text": text, "attachments": attachments})
        return (post_ts, None) if want_error else post_ts

    def fake_post_file(png, title, thread_ts=None, comment=None,
                       want_ts=False):
        seen["shares"].append({"thread_ts": thread_ts, "comment": comment})
        return "shared"

    monkeypatch.setattr(poster, "_upload_unshared", lambda p, t: upload)
    monkeypatch.setattr(poster, "_update", fake_update)
    monkeypatch.setattr(poster, "_post", fake_post)
    monkeypatch.setattr(poster, "_post_file", fake_post_file)
    return seen


def test_the_card_is_completed_in_place(tmp_path, monkeypatch, caplog):
    poster = inject_slack.SlackPoster(enabled=True)
    seen = _fake_update_transport(poster, monkeypatch)
    with caplog.at_level("INFO"):
        ts = poster.post_injection(SENT_ROW, tmp_path / "inj661.png")

    assert ts == "1757000000.661"          # the SAME message, not a new one
    assert seen["posts"] == []
    upd, = seen["updates"]
    assert upd["ts"] == "1757000000.661"
    # the awaiting tail is gone once there is an outcome
    assert "awaiting recovery" not in upd["text"]
    att, = upd["attachments"]
    assert att["color"] == inject_slack.COLOR_RECOVERED
    assert [b["type"] for b in att["blocks"]] == ["section", "image"]
    assert att["blocks"][1]["slack_file"] == {"id": "F123"}
    assert "completed in place (form=inline)" in caplog.text


def test_a_failed_upload_completes_the_card_without_the_image(tmp_path,
                                                              monkeypatch,
                                                              caplog):
    poster = inject_slack.SlackPoster(enabled=True)
    seen = _fake_update_transport(poster, monkeypatch, upload=None)
    with caplog.at_level("INFO"):
        ts = poster.post_injection(SENT_ROW, tmp_path / "x.png")
    assert ts == "1757000000.661"
    att, = seen["updates"][0]["attachments"]
    assert [b["type"] for b in att["blocks"]] == ["section"]
    assert "form=bar" in caplog.text
    assert seen["shares"] == []


def test_rejected_blocks_complete_the_bar_and_thread_the_plot(tmp_path,
                                                              monkeypatch,
                                                              caplog):
    """invalid_blocks: keep the coloured bar, hang the plot underneath."""
    poster = inject_slack.SlackPoster(enabled=True)
    seen = _fake_update_transport(poster, monkeypatch,
                                  update_err="invalid_blocks")
    with caplog.at_level("INFO"):
        ts = poster.post_injection(SENT_ROW, tmp_path / "x.png")
    assert ts == "1757000000.661"
    assert len(seen["updates"]) == 2
    first, second = seen["updates"]
    assert [b["type"] for b in first["attachments"][0]["blocks"]] == [
        "section", "image"]
    assert [b["type"] for b in second["attachments"][0]["blocks"]] == ["section"]
    # the plot arrives as a reply under the very same message
    assert seen["shares"][0]["thread_ts"] == "1757000000.661"
    assert seen["shares"][0]["comment"] == inject_slack.CAPTION_REPLY
    assert "form=bar+thread" in caplog.text


def test_an_unusable_ts_falls_back_to_a_new_message(tmp_path, monkeypatch,
                                                    caplog):
    """A message that cannot be edited at all still gets its card posted."""
    poster = inject_slack.SlackPoster(enabled=True)
    seen = _fake_update_transport(poster, monkeypatch,
                                  update_err="message_not_found",
                                  second_err="message_not_found")
    with caplog.at_level("INFO"):
        ts = poster.post_injection(SENT_ROW, tmp_path / "x.png")
    assert ts == "tsnew"
    assert seen["posts"], "no new message was posted"
    assert "posting a new message instead" in caplog.text


def test_without_a_stored_ts_it_posts_fresh(tmp_path, monkeypatch):
    """Nothing to update - the fire-time post never landed."""
    poster = inject_slack.SlackPoster(enabled=True)
    seen = _fake_update_transport(poster, monkeypatch)
    ts = poster.post_injection(dict(SENT_ROW, slack_ts=None),
                               tmp_path / "x.png")
    assert ts == "tsnew"
    assert seen["updates"] == []
    assert seen["posts"]


def test_sent_then_update_dry_run_writes_both_payloads(tmp_path):
    import json
    poster = inject_slack.SlackPoster(enabled=True, dry_run_dir=tmp_path)
    poster.post_sent(SENT_ROW)
    poster.post_injection(SENT_ROW, "/events/inj661/inj661.png")

    sent, = list(tmp_path.glob("*_inject_661_sent.txt"))
    upd, = list(tmp_path.glob("*_inject_661_update.txt"))
    sent_payload = json.loads(sent.read_text())
    assert sent_payload["text"].endswith("_awaiting recovery..._")

    body = upd.read_text()
    upd_payload = json.loads(body.split("\n\n[uploaded")[0])
    assert upd_payload["ts"] == "1757000000.661"
    assert "awaiting recovery" not in upd_payload["text"]
    att, = upd_payload["attachments"]
    assert [b["type"] for b in att["blocks"]] == ["section", "image"]
    assert "/events/inj661/inj661.png" in body
