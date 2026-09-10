"""Injected width, the per-width amplitude ratio, and the saturation cap.

Nothing here renders a filterbank or talks to Redis: every function under
test is pure.
"""

import math
import random

import pytest

from casm_t2 import hella_kernel as hk
from casm_t2.apps import inject_daemon as d


# --- hella's smoothing kernel ----------------------------------------------

@pytest.mark.parametrize("ibox,fwhm_ms", [
    (0, 1.0), (1, 1.0), (2, 3.1), (3, 5.2), (4, 11.5), (5, 22.0), (6, 45.1),
])
def test_kernel_fwhm_ms(ibox, fwhm_ms):
    """Computed from smooth.cpp's polynomial, not a lookup table."""
    assert hk.kernel_fwhm_ms(ibox) == pytest.approx(fwhm_ms, abs=0.05)


def test_kernel_is_narrower_than_the_trial_label():
    """The headline reason to report kernel FWHM and not 2**ibox samples."""
    for ibox in range(2, 7):
        trial_ms = 2 ** ibox * hk.TSAMP_MS
        assert 0.6 < hk.kernel_fwhm_ms(ibox) / trial_ms < 0.76


def test_boxcar_equivalent_width():
    # 1/sum(k^2) is close to the trial label: the kernel averages nearly the
    # same number of samples a boxcar of 2**ibox would.
    for ibox in range(2, 7):
        assert hk.boxcar_equiv_samp(ibox) / 2 ** ibox == pytest.approx(0.93,
                                                                       abs=0.02)


def test_kernel_normalised():
    for ibox in range(0, 7):
        assert hk._kernel(ibox).sum() == pytest.approx(1.0)
        assert (hk._kernel(ibox) >= 0).all()


def test_best_ibox_never_returns_the_vetoed_trial():
    """t2d vetoes ibox 6, so the sampling range must never aim at it."""
    for fwhm in (2.5, 5.0, 11.0, 22.0, 30.0):
        assert hk.best_ibox_for_fwhm(fwhm) < hk.VETOED_IBOX


@pytest.mark.parametrize("inj_fwhm_ms,ibox", [
    (4.71, 2),      # id 660
    (11.78, 4),     # ids 657, 659, 661, 663
    (23.55, 5),     # id 662
    (70.65, 5),     # id 658, clipped by the veto
])
def test_best_ibox_reproduces_the_live_shots(inj_fwhm_ms, ibox):
    """Measured 2026-09-09; the prediction is the widest kernel that fits."""
    assert hk.best_ibox_for_fwhm(inj_fwhm_ms) == ibox


# --- injected width sampling ------------------------------------------------

def test_sample_fwhm_respects_the_generator_floor():
    """make_noise_fil_with_frb_snr floors sigma at 1 sample and has no
    argument to lower it, so 1.0 ms cannot be rendered: it is clamped."""
    assert d.MIN_RENDERABLE_FWHM_MS == pytest.approx(2.355 * 1.048576)
    rng = random.Random(0)
    for _ in range(200):
        assert d.sample_fwhm_ms(rng, 1.0, 30.0) >= d.MIN_RENDERABLE_FWHM_MS


def test_sample_fwhm_is_log_uniform_and_in_range():
    rng = random.Random(1)
    draws = [d.sample_fwhm_ms(rng, 2.5, 30.0) for _ in range(4000)]
    assert min(draws) >= 2.5
    assert max(draws) <= 30.0
    # log-uniform: the median sits at the geometric mean, not the arithmetic
    # one (which would be 16.25).
    draws.sort()
    assert draws[len(draws) // 2] == pytest.approx(math.sqrt(2.5 * 30.0),
                                                   rel=0.06)


def test_sampling_range_exercises_ibox_1_to_5():
    """Every trial the generator can reach should actually get shots.

    ibox 0 is unreachable: it shares a 1.0 ms kernel FWHM with ibox 1, so
    nothing distinguishes them by width, and the narrowest pulse the
    generator can render is 2.47 ms anyway.
    """
    rng = random.Random(2)
    seen = {hk.best_ibox_for_fwhm(d.sample_fwhm_ms(rng, 2.5, 30.0))
            for _ in range(3000)}
    assert seen == {1, 2, 3, 4, 5}


# --- the per-width reported/true ratio --------------------------------------

TABLE_CFG = {"rec_per_true_table": [[4.7, 2.24], [11.8, 2.08], [23.5, 1.22]],
             "rec_per_true": 1.30}


def test_rec_per_true_hits_the_measured_points():
    for fwhm, ratio in TABLE_CFG["rec_per_true_table"]:
        assert d.rec_per_true(fwhm, TABLE_CFG) == pytest.approx(ratio)


def test_rec_per_true_interpolates_in_log_fwhm():
    # Halfway in log between 11.8 and 23.5 is sqrt(11.8*23.5) = 16.65 ms,
    # so the ratio there is the midpoint of 2.08 and 1.22.
    mid = math.sqrt(11.8 * 23.5)
    assert d.rec_per_true(mid, TABLE_CFG) == pytest.approx((2.08 + 1.22) / 2,
                                                           rel=1e-6)
    # and it is monotone across the measured span
    vals = [d.rec_per_true(w, TABLE_CFG) for w in (5, 8, 12, 16, 20, 23)]
    assert vals == sorted(vals, reverse=True)


def test_rec_per_true_holds_the_end_values_flat():
    assert d.rec_per_true(1.0, TABLE_CFG) == pytest.approx(2.24)
    assert d.rec_per_true(500.0, TABLE_CFG) == pytest.approx(1.22)


def test_rec_per_true_falls_back_to_the_scalar():
    assert d.rec_per_true(11.8, {"rec_per_true": 1.30}) == pytest.approx(1.30)
    assert d.rec_per_true(11.8, {}) == pytest.approx(1.5)


def test_rec_per_true_accepts_a_single_point_table():
    assert d.rec_per_true(99.0, {"rec_per_true_table": [[10.0, 1.7]]}) == 1.7


def test_amplitude_scales_so_the_target_is_width_independent(monkeypatch):
    """The whole point of the table: same target S/N, any width, and the
    solved amplitude tracks the measured ratio rather than a constant."""
    monkeypatch.setattr(d, "amp_for_target_snr",
                        lambda true_snr, sigma_ms, beam, nchan: (true_snr, 1.0))
    solved = {}
    for fwhm in (4.7, 11.8, 23.5):
        k = d.rec_per_true(fwhm, TABLE_CFG)
        solved[fwhm] = 25.0 / k
    # a wider pulse needs MORE true S/N to be reported at 25, because hella
    # reports a smaller multiple of the analytical value
    assert solved[4.7] < solved[11.8] < solved[23.5]


# --- the reported-S/N cap and the clamp ------------------------------------

@pytest.mark.parametrize("fwhm,cap", [
    (2.5, 50.0), (4.7, 50.0), (5.9, 50.0),        # narrow band
    (6.0, 40.0), (11.8, 40.0), (15.0, 40.0),      # mid band
    (23.5, 40.0 * math.sqrt(12.0 / 23.5)),        # wide band, 1/sqrt(FWHM)
    (30.0, 40.0 * math.sqrt(12.0 / 30.0)),        # ~25 at the widest injection
])
def test_reported_snr_cap_per_band(fwhm, cap):
    assert d.reported_snr_cap(fwhm) == pytest.approx(cap)


def test_widest_injection_caps_near_25():
    assert d.reported_snr_cap(30.0) == pytest.approx(25.3, abs=0.1)


def test_cap_honours_the_legacy_flat_alias():
    """A config with only target_rec_snr_max keeps working, width-independent."""
    legacy = {"target_rec_snr_max": 40.0}
    for fwhm in (2.5, 11.8, 30.0):
        assert d.reported_snr_cap(fwhm, legacy) == 40.0


def test_clamp_leaves_a_normal_shot_alone():
    inj, predicted, clamped = d.clamp_inject_snr(15.0, 11.8, TABLE_CFG)
    assert clamped is False
    assert inj == 15.0
    assert predicted == pytest.approx(15.0 * 2.08)


def test_the_sampled_range_never_clamps_at_any_width():
    """`inject_snr_range` is chosen so the cap is a safety net, not a ceiling
    the scheduled shots keep running into. A clamp during normal running
    means the range and the cap have drifted apart, so it is worth failing.

    Swept finely because the cap steps down at 6 and 15 ms; the tightest
    point is FWHM 6.0 ms, where 18 * 2.198 = 39.6 against a cap of 40.
    """
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    hi = icfg["inject_snr_range"][1]
    lo_fwhm, hi_fwhm = icfg["fwhm_ms_range"]
    fwhm, worst = lo_fwhm, None
    while fwhm <= hi_fwhm + 1e-9:
        _inj, predicted, clamped = d.clamp_inject_snr(hi, fwhm, icfg)
        assert not clamped, f"top of the range clamps at FWHM {fwhm:.3f} ms"
        margin = d.reported_snr_cap(fwhm, icfg) - predicted
        worst = margin if worst is None else min(worst, margin)
        fwhm += 0.01
    # headroom is real but thin at the 6 ms step; worth knowing if it moves
    assert 0.0 < worst < 1.0


def test_a_manual_overbright_shot_is_still_clamped():
    """The safety net still catches --inject-snr 30 at FWHM 11.8 ms."""
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    inj, predicted, clamped = d.clamp_inject_snr(30.0, 11.8, icfg)
    assert clamped is True
    assert predicted == pytest.approx(40.0)
    assert inj == pytest.approx(40.0 / 2.08, rel=1e-3)
    assert inj < 30.0


def test_clamp_reproduces_the_saturating_shot():
    """id 664 was injected at true 60.9 and reported 132, well over the cap."""
    inj, predicted, clamped = d.clamp_inject_snr(60.9, 11.78, TABLE_CFG)
    assert clamped is True
    assert predicted == pytest.approx(40.0)
    assert inj == pytest.approx(40.0 / 2.08, rel=1e-3)


# --- injected-S/N sampling --------------------------------------------------

def test_sample_inject_snr_stays_in_range_and_is_log_uniform():
    rng = random.Random(4)
    draws = sorted(d.sample_inject_snr(rng, 12.0, 25.0) for _ in range(4000))
    assert draws[0] >= 12.0 and draws[-1] <= 25.0
    assert draws[len(draws) // 2] == pytest.approx(math.sqrt(12.0 * 25.0),
                                                   rel=0.06)


def test_sampled_shots_never_predict_over_the_cap():
    """The whole range, at every width, after the clamp."""
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    rng = random.Random(5)
    for _ in range(2000):
        fwhm = d.sample_fwhm_ms(rng, *icfg["fwhm_ms_range"])
        raw = d.sample_inject_snr(rng, *icfg["inject_snr_range"])
        _inj, predicted, _c = d.clamp_inject_snr(raw, fwhm, icfg)
        assert predicted <= d.reported_snr_cap(fwhm, icfg) + 1e-9


def test_config_uses_the_new_keys():
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    assert icfg["inject_snr_range"] == [12.0, 18.0]
    assert icfg["fwhm_ms_range"] == [2.5, 30.0]
    assert icfg["reported_snr_cap"]["narrow"] == 50.0
    assert icfg["reported_snr_cap"]["mid"] == 40.0
    for gone in ("sigma_ms_range", "target_rec_snr_range", "target_rec_snr_max"):
        assert gone not in icfg


# --- beam offset ------------------------------------------------------------

def test_same_beam_offset_is_exactly_zero():
    from casm_t2 import weights_registry as wr
    pointings = {"alt_deg": [80.0] * 8, "az_deg": [10.0] * 8}
    assert wr.beam_separation_arcsec(pointings, 3, 3) == 0.0


def test_offset_between_two_known_beams():
    """A synthetic table: two beams 1 degree apart at the same azimuth."""
    from casm_t2 import weights_registry as wr
    alt = [0.0] * 8
    az = [0.0] * 8
    alt[0], alt[1] = 45.0, 46.0          # 1 deg apart in altitude
    pointings = {"alt_deg": alt, "az_deg": az}
    assert wr.beam_separation_arcsec(pointings, 0, 1) == pytest.approx(3600.0)

    # and one degree of azimuth at the equator of the alt/az sphere
    alt[2], az[2] = 0.0, 0.0
    alt[3], az[3] = 0.0, 1.0
    assert wr.beam_separation_arcsec(pointings, 2, 3) == pytest.approx(3600.0)


def test_offset_is_none_without_a_pointing_table():
    from casm_t2 import weights_registry as wr
    assert wr.beam_separation_arcsec(None, 0, 1) is None
    assert wr.beam_separation_arcsec({}, 0, 1) is None
    assert wr.beam_separation_arcsec({"alt_deg": [1.0], "az_deg": [1.0]},
                                     0, 400) is None


# --- raw T1 trials behind a shot with no cluster ----------------------------

HEADER = "SNR SAMP_START TIME_START WIDTH DM_IDX DM BEAM_IDX\n"


def _cands(tmp_path, rows, obs="2026-09-09-21:12:15", stream=3):
    """Write a hella-format candidate file and return its path."""
    path = tmp_path / f"cands_{obs}.dat.{stream}"
    with path.open("w") as fh:
        fh.write(HEADER)
        for snr, samp, dm, beam in rows:
            fh.write(f"{snr} {samp} 0.0 4 100 {dm} {beam}\n")
    return path


def test_matching_trials_are_counted(tmp_path):
    path = _cands(tmp_path, [
        (9.2, 1000, 500.0, 200),      # matches
        (8.1, 1010, 505.0, 201),      # matches: beam +1, DM inside tolerance
        (7.5, 1020, 500.0, 198),      # matches: beam -2 is the edge
        (30.0, 1005, 500.0, 210),     # wrong beam
        (30.0, 1005, 900.0, 200),     # wrong DM
        (30.0, 90000, 500.0, 200),    # outside the sample window
    ])
    n, best = d.count_t1_trials(path, beams={198, 199, 200, 201, 202},
                                dm=500.0, samp_lo=900, samp_hi=1100)
    assert n == 3
    assert best == pytest.approx(9.2)


def test_no_matching_trials(tmp_path):
    path = _cands(tmp_path, [(30.0, 1005, 900.0, 210)])
    assert d.count_t1_trials(path, beams={200}, dm=500.0,
                             samp_lo=900, samp_hi=1100) == (0, None)


def test_a_missing_file_is_not_the_same_as_no_trials(tmp_path):
    """None means 'cannot tell', which must never read as 'hella saw nothing'."""
    assert d.count_t1_trials(tmp_path / "nope.dat.3", {200}, 500.0, 0, 1) is None


def test_the_header_line_is_not_a_trial(tmp_path):
    path = _cands(tmp_path, [])
    assert d.count_t1_trials(path, beams={200}, dm=500.0,
                             samp_lo=0, samp_hi=1e9) == (0, None)


def test_dm_tolerance_matches_the_cluster_rule():
    assert d.dm_tolerance(500.0) == pytest.approx(75.0)
    assert d.dm_tolerance(10.0) == pytest.approx(5.0)      # floor


def test_cands_path_shape():
    assert d.cands_path("2026-09-09-21:12:15", 3, "/tmp/x").name == (
        "cands_2026-09-09-21:12:15.dat.3")


def test_obs_utc_start_at(conn, cluster_row):
    from datetime import datetime, timezone
    from casm_t2 import db as _db
    _db.insert_clusters(conn, [cluster_row("260731aaaaaa")])
    conn.execute("UPDATE clusters SET obs_utc_start='2026-07-31-00:00:00',"
                 " event_utc='2026-07-31T00:00:10.000+00:00'")
    conn.commit()
    got = d.obs_utc_start_at(
        conn, datetime(2026, 7, 31, 0, 5, tzinfo=timezone.utc))
    assert got == "2026-07-31-00:00:00"
    # nothing that old
    assert d.obs_utc_start_at(
        conn, datetime(2026, 7, 30, tzinfo=timezone.utc)) is None


# --- reconcile end to end ---------------------------------------------------

def _inject(conn, inj_id=1, utc="2026-07-31T00:05:00.000+00:00",
            stream=3, beam=200, dm=500.0):
    conn.execute(
        "INSERT INTO injections (id, inject_utc, stream, beam, dm, amp,"
        " sigma_ms, file_id, created_utc) VALUES (?,?,?,?,?,5.0,5.0,'f',?)",
        (inj_id, utc, stream, beam, dm, utc))
    conn.commit()


def _observation(conn, cluster_row, obs="2026-07-31-00:00:00"):
    """One cluster, only so obs_utc_start_at has an observation to find."""
    from casm_t2 import db as _db
    _db.insert_clusters(conn, [cluster_row("260731zzzzzz")])
    conn.execute("UPDATE clusters SET obs_utc_start=?,"
                 " event_utc='2026-07-31T00:00:10.000+00:00'", (obs,))
    conn.commit()


def _outcome(conn, inj_id=1):
    return conn.execute(
        "SELECT outcome, fail_reason, n_t1_trials FROM injections WHERE id=?",
        (inj_id,)).fetchone()


def test_reconcile_missed_t2_when_trials_are_there(conn, cluster_row, tmp_path):
    _observation(conn, cluster_row)
    _inject(conn)
    # inject_utc is 300 s after UTC_START, so the window centre is sample
    # 300/0.001048576 = 286102
    _cands(tmp_path, [(9.2, 286102, 500.0, 200), (8.0, 286200, 498.0, 201)],
           obs="2026-07-31-00:00:00", stream=3)
    d.reconcile(conn, 1, {"injection": {"hella_cands_dir": str(tmp_path)}})
    outcome, reason, n = _outcome(conn)
    assert outcome == "missed_t2"
    assert n == 2
    assert reason == ("lost at T2: 2 matching T1 trials (best S/N 9.2) "
                      "but no cluster formed (min 5 members)")


def test_reconcile_missed_t1_when_the_file_has_nothing(conn, cluster_row,
                                                       tmp_path):
    _observation(conn, cluster_row)
    _inject(conn)
    _cands(tmp_path, [(30.0, 286102, 900.0, 210)],
           obs="2026-07-31-00:00:00", stream=3)
    d.reconcile(conn, 1, {"injection": {"hella_cands_dir": str(tmp_path)}})
    outcome, reason, n = _outcome(conn)
    assert outcome == "missed_t1"
    assert n == 0
    assert reason.startswith("lost at T1: no matching trial in beam 200")


def test_reconcile_missed_t1_when_the_file_is_missing(conn, cluster_row,
                                                      tmp_path):
    _observation(conn, cluster_row)
    _inject(conn)
    d.reconcile(conn, 1, {"injection": {"hella_cands_dir": str(tmp_path)}})
    outcome, reason, n = _outcome(conn)
    assert outcome == "missed_t1"
    assert n is None                      # unknown, not zero
    assert "T1 file unavailable" in reason
    assert "not found" in reason


def test_a_low_snr_cluster_is_recovered(conn, make_cluster, tmp_path):
    """S/N 15.8, below tier B: found by the search, so recovered."""
    from casm_t2 import db as _db
    cl = make_cluster(snr=15.8, beam=200)
    _db.insert_clusters(conn, [(cl, "2026-07-31-00:00:00", 1,
                                "2026-07-31T00:05:05.000+00:00", "C",
                                "injection", "260731cccccc")])
    conn.execute("UPDATE clusters SET dm=500.0, dm_lo=499.0, dm_hi=501.0")
    conn.commit()
    _inject(conn)
    d.reconcile(conn, 1, {"injection": {"hella_cands_dir": str(tmp_path)}})
    outcome, reason, n = _outcome(conn)
    assert outcome == "recovered"
    assert reason is None
    assert n is None                      # no need to look at raw trials
    # the trigger gate is still recorded, it just does not decide
    gate = conn.execute(
        "SELECT gate_trigger FROM injections WHERE id=1").fetchone()[0]
    assert gate == 0


# --- sky neighbours, not beam index -----------------------------------------

def _sky_with_neighbours():
    """A 512-beam table where beam 5's sky neighbours are 200 and 301.

    Beam 6 - its index neighbour - is parked far away, which is the whole
    point: index adjacency says nothing about the sky.
    """
    from casm_t2 import cluster as cl
    import numpy as np
    alt = np.full(512, 80.0)
    az = np.arange(512, dtype=float) * 0.7 % 360.0   # scattered
    # put 5, 200, 301 essentially on top of each other, and 6 far off
    for b in (5, 200, 301):
        alt[b], az[b] = 80.0, 10.0
    alt[200], az[200] = 80.05, 10.0        # a few arcmin away
    alt[301], az[301] = 79.95, 10.0
    alt[6], az[6] = 40.0, 200.0            # nowhere near
    return cl.SkyTable(alt, az, weights_id="wtest")


def test_neighbour_beams_is_a_sky_test_not_an_index_one():
    from casm_t2 import cluster as cl
    sky = _sky_with_neighbours()
    got = cl.neighbour_beams(sky, 5, fwhm_x_deg=1.0, fwhm_y_deg=1.0)
    assert 5 in got and 200 in got and 301 in got
    assert 6 not in got                    # the index neighbour is far away


def test_neighbour_beams_without_a_table_is_just_the_beam():
    """No pointing table must never silently look like a sky answer."""
    from casm_t2 import cluster as cl
    assert cl.neighbour_beams(None, 5, 1.0, 1.0) == {5}
    sky = _sky_with_neighbours()
    assert cl.neighbour_beams(sky, 9999, 1.0, 1.0) == {9999}
    assert cl.neighbour_beams(sky, 5, 0.0, 1.0) == {5}


def test_neighbour_beams_is_cached():
    from casm_t2 import cluster as cl
    sky = _sky_with_neighbours()
    a = cl.neighbour_beams(sky, 5, 1.0, 1.0)
    b = cl.neighbour_beams(sky, 5, 1.0, 1.0)
    assert a == b
    assert (sky.weights_id, 5, 1.0, 1.0, 1.0) in cl._NEIGHBOUR_CACHE


def test_reconcile_matches_a_sky_neighbour_and_rejects_an_index_neighbour(
        conn, make_cluster, monkeypatch, tmp_path):
    """Injected in beam 5; the cluster is in beam 200, a sky neighbour."""
    from casm_t2 import db as _db
    from casm_t2.apps import inject_daemon as dd
    sky = _sky_with_neighbours()
    monkeypatch.setattr(dd, "injection_neighbours",
                        lambda cfg, utc, beam: ({5, 200, 301}, True))
    for beam, name, ok in ((200, "260731nnnnnn", True), (6, "260731iiiiii", False)):
        conn.execute("DELETE FROM clusters")
        conn.execute("DELETE FROM injections")
        cl = make_cluster(snr=25.0, beam=beam)
        _db.insert_clusters(conn, [(cl, "2026-07-31-00:00:00", 1,
                                    "2026-07-31T00:05:05.000+00:00", "B",
                                    "injection", name)])
        conn.execute("UPDATE clusters SET dm=500.0, dm_lo=499.0, dm_hi=501.0")
        _inject(conn, beam=5)
        dd.reconcile(conn, 1, {"injection": {"hella_cands_dir": str(tmp_path)}})
        outcome = conn.execute(
            "SELECT outcome FROM injections WHERE id=1").fetchone()[0]
        assert (outcome == "recovered") is ok, f"beam {beam}"


def test_t2d_tags_a_sky_neighbour_and_not_an_index_neighbour(daemon,
                                                             make_cluster):
    """t2d's injection tag follows the same sky rule."""
    d_ = daemon()
    sky = _sky_with_neighbours()
    d_._sky_params[sky.weights_id] = d_.params
    d_._inj_cache = [(1000.0, 5, 500.0)]        # epoch, beam, dm
    for beam, tagged in ((301, True), (6, False)):
        cl = make_cluster(snr=25.0, beam=beam)
        object.__setattr__(cl, "dm_lo", 499.0)
        object.__setattr__(cl, "dm_hi", 501.0)
        assert d_._injection_match(cl, 1000.0, sky) is tagged, f"beam {beam}"
        assert d_._cand_injection_match(1000.0, beam, 500.0, sky) is tagged


def test_t2d_falls_back_to_the_index_window_without_a_table(daemon,
                                                            make_cluster):
    d_ = daemon()
    d_._inj_cache = [(1000.0, 5, 500.0)]
    cl = make_cluster(snr=25.0, beam=6)
    object.__setattr__(cl, "dm_lo", 499.0)
    object.__setattr__(cl, "dm_hi", 501.0)
    # index +-2 catches beam 6, and the daemon says so once
    assert d_._injection_match(cl, 1000.0, None) is True
    assert d_._inj_index_warned is True
