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

# The sub-OFF sweep, kept as the fixture for the interpolation tests: it has
# three clearly distinct points, which is what those tests exercise.
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


def test_the_sampled_range_never_clamps_at_any_width_or_dm():
    """`inject_snr` is drawn so the cap is a safety net, not a ceiling the
    scheduled shots keep running into. A clamp during normal running means
    the sample block and the cap have drifted apart, so it is worth failing.

    DM is swept too, to pin that nothing in the solver reads it: the
    amplitude and the cap are functions of width and S/N only. DM matters
    downstream (the dump window, the search grid), not here.
    """
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    hi = icfg["sample"]["inject_snr"]["hi"]
    lo_f, hi_f = icfg["sample"]["fwhm_ms"]["lo"], icfg["sample"]["fwhm_ms"]["hi"]
    lo_dm, hi_dm = icfg["sample"]["dm"]["lo"], icfg["sample"]["dm"]["hi"]
    worst = None
    fwhm = lo_f
    while fwhm <= hi_f + 1e-9:
        _inj, predicted, clamped = d.clamp_inject_snr(hi, fwhm, icfg)
        assert not clamped, f"top of the range clamps at FWHM {fwhm:.3f} ms"
        margin = d.reported_snr_cap(fwhm, icfg) - predicted
        worst = margin if worst is None else min(worst, margin)
        # the same width at every DM in the range must give the same answer
        for dm in (lo_dm, (lo_dm + hi_dm) / 2, hi_dm):
            amp_a = d.rec_per_true(fwhm, icfg)
            assert d.reported_snr_cap(fwhm, icfg) == d.reported_snr_cap(
                fwhm, icfg), dm
            assert amp_a == d.rec_per_true(fwhm, icfg)
        fwhm += 0.01
    assert worst > 0.0


def test_the_solver_does_not_read_dm():
    """The amplitude is a function of S/N, width and the live std only."""
    import inspect
    src = inspect.getsource(d.amp_for_target_snr)
    assert "dm" not in src.lower().replace("nchan", "")


def test_a_manual_overbright_shot_is_still_clamped():
    """The safety net catches a manual --inject-snr well over the cap.

    Under the sub-ON table the ratio at 11.8 ms is 1.30, so the 40 cap is
    reached at an injected S/N of 30.8 - a manual 40 is over it.
    """
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    assert d.clamp_inject_snr(30.0, 11.8, icfg)[2] is False   # just under
    inj, predicted, clamped = d.clamp_inject_snr(40.0, 11.8, icfg)
    assert clamped is True
    assert predicted == pytest.approx(40.0)
    assert inj == pytest.approx(40.0 / 1.30, rel=1e-3)
    assert inj < 40.0


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
        fwhm = d.clamp_fwhm_ms(d.sample_spec(icfg, "fwhm_ms", rng))
        raw = d.sample_spec(icfg, "inject_snr", rng)
        dm = d.clamp_dm(d.sample_spec(icfg, "dm", rng))
        assert 0.0 <= dm <= 1000.0
        _inj, predicted, _c = d.clamp_inject_snr(raw, fwhm, icfg)
        assert predicted <= d.reported_snr_cap(fwhm, icfg) + 1e-9


def test_config_uses_the_new_keys():
    import yaml
    with open("config/t2d.yaml") as fh:
        icfg = yaml.safe_load(fh)["injection"]
    assert icfg["sample"]["dm"] == {"dist": "uniform", "lo": 100.0, "hi": 900.0}
    assert icfg["sample"]["fwhm_ms"] == {"dist": "loguniform", "lo": 2.5,
                                         "hi": 30.0}
    assert icfg["sample"]["inject_snr"] == {"dist": "loguniform", "lo": 12.0,
                                            "hi": 18.0}
    assert icfg["summary_dm_bins"] == [100.0, 300.0, 500.0, 700.0, 900.0]
    assert icfg["reported_snr_cap"]["narrow"] == 50.0
    # seeded for IB subtraction ON: measured 1.30 at the mid width
    assert icfg["rec_per_true_table"] == [[4.7, 1.40], [11.8, 1.30],
                                          [23.5, 0.77]]
    assert icfg["reported_snr_cap"]["mid"] == 40.0
    for gone in ("sigma_ms_range", "target_rec_snr_range", "target_rec_snr_max",
                 "dm_range", "fwhm_ms_range", "inject_snr_range"):
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
    got = d.count_t1_trials(path, beams={198, 199, 200, 201, 202},
                            dm=500.0, samp_lo=900, samp_hi=1100)
    assert got.n == 3
    assert got.best_snr == pytest.approx(9.2)
    assert got.gulp == 1000 // 8192          # samp // GULP_SAMPS
    assert got.all_veto_width is False       # the fixture writes width 4


def test_no_matching_trials(tmp_path):
    path = _cands(tmp_path, [(30.0, 1005, 900.0, 210)])
    got = d.count_t1_trials(path, beams={200}, dm=500.0,
                            samp_lo=900, samp_hi=1100)
    assert (got.n, got.best_snr) == (0, None)


def test_a_missing_file_is_not_the_same_as_no_trials(tmp_path):
    """None means 'cannot tell', which must never read as 'hella saw nothing'."""
    assert d.count_t1_trials(tmp_path / "nope.dat.3", {200}, 500.0, 0, 1) is None


def test_the_header_line_is_not_a_trial(tmp_path):
    path = _cands(tmp_path, [])
    got = d.count_t1_trials(path, beams={200}, dm=500.0,
                            samp_lo=0, samp_hi=1e9)
    assert (got.n, got.best_snr) == (0, None)


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
    # no gulp_stats row for that gulp in this fixture DB
    assert reason == ("lost at T2: gulp 34 never reached t2d "
                      "(no gulp_stats row)")


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


# --- IB subtraction state ---------------------------------------------------

BFC = ("{n} [{ts}] START casm_bfcorr -a 64 -f 512 -i a00a -m corr"
       " --corr_out a022{sub} -t 2048 -d 5 -m bf --bf_out a016\n")


def _bfcorr_log(tmp_path, entries):
    path = tmp_path / "antenna_bfcorr.log"
    with path.open("w") as fh:
        for n, ts, sub in entries:
            fh.write(BFC.format(n=n, ts=ts, sub=" --sub_incoh" if sub else ""))
    return path


def test_sub_incoh_on(tmp_path):
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-22:26:46.220", True)])
    assert d.current_sub_incoh(path) == 1


def test_sub_incoh_off(tmp_path):
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-22:26:46.220", False)])
    assert d.current_sub_incoh(path) == 0


def test_sub_incoh_takes_the_LATEST_start_not_the_last_line(tmp_path):
    """The log interleaves nodes: node 1 can log after node 5 restarted."""
    path = _bfcorr_log(tmp_path, [
        (5, "2026-09-09-22:27:58.266", False),   # latest, subtraction OFF
        (1, "2026-09-09-22:26:46.220", True),    # last LINE, but older
    ])
    assert d.current_sub_incoh(path) == 0
    assert d.last_bfcorr_start(path).minute == 27


def test_sub_incoh_unknown_when_unreadable(tmp_path):
    assert d.current_sub_incoh(tmp_path / "nope.log") is None
    assert d.last_bfcorr_start(tmp_path / "nope.log") is None
    empty = tmp_path / "empty.log"
    empty.write_text("nothing to see\n")
    assert d.current_sub_incoh(empty) is None


# --- live-std staleness guard -----------------------------------------------

def _clock(start):
    from datetime import timedelta
    box = {"t": start}

    def now():
        return box["t"]

    def sleep(dt):
        box["t"] += timedelta(seconds=dt)
    return now, sleep, box


def test_std_is_trusted_when_no_recent_restart(tmp_path):
    from datetime import datetime, timezone
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-20:00:00.000", True)])
    now, sleep, _ = _clock(datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc))
    reads = iter([36.2])
    std, age = d.wait_for_fresh_std(
        150, {}, reader=lambda: next(reads), now=now, sleep=sleep,
        log_path=path)
    assert std == 36.2
    assert age > 7000                  # two hours since the restart


def test_a_recent_restart_waits_for_the_value_to_move(tmp_path):
    from datetime import datetime, timezone
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-22:00:00.000", True)])
    now, sleep, box = _clock(datetime(2026, 9, 9, 22, 0, 10,
                                      tzinfo=timezone.utc))
    # frozen for two polls, then republished
    reads = iter([77.3, 77.3, 77.3, 41.0])
    std, age = d.wait_for_fresh_std(
        150, {"std_wait_s": 60.0, "std_poll_s": 5.0},
        reader=lambda: next(reads), now=now, sleep=sleep, log_path=path)
    assert std == 41.0
    assert age == pytest.approx(25.0)   # 10 s since restart + 15 s waiting


def test_a_frozen_std_gives_up_and_raises(tmp_path):
    from datetime import datetime, timezone
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-22:00:00.000", True)])
    now, sleep, _ = _clock(datetime(2026, 9, 9, 22, 0, 10,
                                    tzinfo=timezone.utc))
    with pytest.raises(d.StaleStdError) as exc:
        d.wait_for_fresh_std(150, {"std_wait_s": 20.0, "std_poll_s": 5.0},
                             reader=lambda: 77.3, now=now, sleep=sleep,
                             log_path=path)
    assert "live std stale" in str(exc.value)
    assert exc.value.age_s == pytest.approx(30.0)


def test_no_bfcorr_log_means_no_guard(tmp_path):
    """Unknown restart time must not block injections forever."""
    std, age = d.wait_for_fresh_std(150, {}, reader=lambda: 36.2,
                                    log_path=tmp_path / "nope.log")
    assert std == 36.2 and age == 0.0


def test_the_666_scenario_is_caught(tmp_path):
    """Fired 2.5 min after a restart on a std that never moved."""
    from datetime import datetime, timezone
    path = _bfcorr_log(tmp_path, [(1, "2026-09-09-22:27:58.267", True)])
    now, sleep, _ = _clock(datetime(2026, 9, 9, 22, 30, 0,
                                    tzinfo=timezone.utc))
    # 122 s after the restart, inside a max_std_age_s of 180
    with pytest.raises(d.StaleStdError):
        d.wait_for_fresh_std(150, {"max_std_age_s": 180.0, "std_wait_s": 30.0,
                                   "std_poll_s": 5.0},
                             reader=lambda: 77.27, now=now, sleep=sleep,
                             log_path=path)


# --- the sample block -------------------------------------------------------

def test_draw_fixed():
    rng = random.Random(0)
    assert d.draw({"dist": "fixed", "value": 15}, rng) == 15.0
    assert d.draw({"dist": "fixed", "value": 15}, rng) == 15.0   # no variance


def test_draw_uniform_covers_the_range():
    rng = random.Random(1)
    vals = [d.draw({"dist": "uniform", "lo": 100, "hi": 900}, rng)
            for _ in range(4000)]
    assert min(vals) >= 100.0 and max(vals) <= 900.0
    # a uniform draw's median sits at the arithmetic midpoint
    assert sorted(vals)[2000] == pytest.approx(500.0, rel=0.05)


def test_draw_loguniform_is_not_uniform():
    rng = random.Random(2)
    vals = sorted(d.draw({"dist": "loguniform", "lo": 2.5, "hi": 30.0}, rng)
                  for _ in range(4000))
    assert vals[0] >= 2.5 and vals[-1] <= 30.0
    # median at the geometric mean, not the arithmetic one (16.25)
    assert vals[2000] == pytest.approx(math.sqrt(2.5 * 30.0), rel=0.06)


def test_draw_choice_only_returns_listed_values():
    rng = random.Random(3)
    spec = {"dist": "choice", "values": [3.0, 8.0, 20.0]}
    vals = {d.draw(spec, rng) for _ in range(300)}
    assert vals == {3.0, 8.0, 20.0}


def test_draw_choice_honours_weights():
    rng = random.Random(4)
    spec = {"dist": "choice", "values": [3.0, 8.0], "weights": [0, 1]}
    assert {d.draw(spec, rng) for _ in range(100)} == {8.0}


@pytest.mark.parametrize("spec,msg", [
    ({"dist": "uniform", "lo": 900, "hi": 100}, "lo < hi"),
    ({"dist": "uniform", "lo": 1}, "needs `lo` and `hi`"),
    ({"dist": "loguniform", "lo": 0, "hi": 30}, "lo > 0"),
    ({"dist": "loguniform", "lo": -5, "hi": 30}, "lo > 0"),
    ({"dist": "choice", "values": []}, "non-empty"),
    ({"dist": "choice", "values": [1, 2], "weights": [1]}, "match `values`"),
    ({"dist": "choice", "values": [1, 2], "weights": [0, 0]}, "sum > 0"),
    ({"dist": "fixed"}, "needs `value`"),
    ({"dist": "nonsense"}, "unknown dist"),
    ("not a mapping", "must be a mapping"),
])
def test_a_bad_spec_is_refused_loudly(spec, msg):
    """A config mistake must stop the daemon, not quietly mis-inject."""
    with pytest.raises(d.inject_calib.SpecError) as exc:
        d.draw(spec, random.Random(0))
    assert msg in str(exc.value)


def test_the_old_range_keys_still_work_and_warn(caplog):
    rng = random.Random(5)
    legacy = {"fwhm_ms_range": [2.5, 30.0]}
    with caplog.at_level("WARNING"):
        v = d.sample_spec(legacy, "fwhm_ms", rng, "fwhm_ms_range")
    assert 2.5 <= v <= 30.0
    assert "deprecated" in caplog.text
    assert "sample.fwhm_ms" in caplog.text


def test_the_sample_block_wins_over_a_legacy_key(caplog):
    both = {"sample": {"fwhm_ms": {"dist": "fixed", "value": 7.0}},
            "fwhm_ms_range": [2.5, 30.0]}
    with caplog.at_level("WARNING"):
        assert d.sample_spec(both, "fwhm_ms", random.Random(0),
                             "fwhm_ms_range") == 7.0
    assert "deprecated" not in caplog.text


def test_nothing_configured_at_all_is_an_error():
    with pytest.raises(d.inject_calib.SpecError):
        d.sample_spec({}, "fwhm_ms", random.Random(0), "fwhm_ms_range")


def test_fwhm_floor_is_enforced_after_the_draw(caplog):
    with caplog.at_level("WARNING"):
        assert d.clamp_fwhm_ms(1.0) == pytest.approx(d.MIN_RENDERABLE_FWHM_MS)
    assert "below" in caplog.text
    assert d.clamp_fwhm_ms(11.8) == 11.8       # no warning for a normal width


@pytest.mark.parametrize("dm,want", [
    (-5.0, 0.0), (0.0, 0.0), (450.0, 450.0), (1000.0, 1000.0), (1500.0, 1000.0),
])
def test_dm_is_clamped_to_hellas_grid(dm, want):
    assert d.clamp_dm(dm) == pytest.approx(want)


def test_a_calibration_grid_config_draws_only_the_grid():
    """The shape a calibration run will be expressed in."""
    icfg = {"sample": {"fwhm_ms": {"dist": "choice", "values": [3.0, 8.0, 20.0]},
                       "inject_snr": {"dist": "fixed", "value": 15.0},
                       "dm": {"dist": "fixed", "value": 300.0}}}
    rng = random.Random(6)
    widths = {d.sample_spec(icfg, "fwhm_ms", rng) for _ in range(200)}
    assert widths == {3.0, 8.0, 20.0}
    assert d.sample_spec(icfg, "inject_snr", rng) == 15.0
    assert d.sample_spec(icfg, "dm", rng) == 300.0


# --- why a gulp with trials produced no cluster ------------------------------

OBS = "2026-07-31-00:00:00"


def _gulp_stats(conn, gulp, n_cands=54, n_clusters=7, n_stored=7, n_vetoed=0,
                n_shed=0, skipped=0, n_jobs=8):
    conn.execute(
        "INSERT INTO gulp_stats (obs_utc_start, gulp, gulp_utc, n_jobs,"
        " n_cands, n_clusters, n_stored, n_would, clustering_ms, n_vetoed,"
        " n_shed, skipped, created_utc)"
        " VALUES (?,?,'2026-07-31T00:00:00.000+00:00',?,?,?,?,0,1.0,?,?,?,'x')",
        (OBS, gulp, n_jobs, n_cands, n_clusters, n_stored, n_vetoed, n_shed,
         skipped))
    conn.commit()


class _Match:
    def __init__(self, gulp=432, all_veto_width=False):
        self.gulp = gulp
        self.all_veto_width = all_veto_width


def test_t2_miss_incomplete_gulp(conn):
    _gulp_stats(conn, 432, skipped=1, n_jobs=5)
    assert d.t2_miss_reason(conn, OBS, 432, _Match()) == (
        "lost at T2: gulp 432 skipped incomplete (5/8 jobs)")


def test_t2_miss_storm_cap(conn):
    _gulp_stats(conn, 432, n_cands=10000, n_shed=10000)
    assert d.t2_miss_reason(conn, OBS, 432, _Match()) == (
        "lost at T2: gulp 432 dropped by the storm cap (10000 trials > max)")


def test_t2_miss_partial_shed(conn):
    _gulp_stats(conn, 432, n_cands=8000, n_shed=3000)
    assert d.t2_miss_reason(conn, OBS, 432, _Match()) == (
        "lost at T2: gulp 432 shed 3000 of 8000 trials")


def test_t2_miss_width_vetoed(conn):
    _gulp_stats(conn, 432, n_cands=54, n_vetoed=54)
    assert d.t2_miss_reason(conn, OBS, 432, _Match(all_veto_width=True)) == (
        "lost at T2: gulp 432 width-vetoed")


def test_t2_miss_on_an_intact_gulp_is_flagged(conn, caplog):
    """Should be impossible: T2 clusters every surviving trial."""
    _gulp_stats(conn, 432)
    with caplog.at_level("WARNING"):
        reason = d.t2_miss_reason(conn, OBS, 432, _Match())
    assert reason == (
        "lost at T2: gulp 432 intact but no cluster (unexpected, investigate)")
    assert "investigate" in caplog.text


def test_t2_miss_with_no_gulp_stats_row(conn, caplog):
    with caplog.at_level("WARNING"):
        reason = d.t2_miss_reason(conn, OBS, 999, _Match(gulp=999))
    assert reason == "lost at T2: gulp 999 never reached t2d (no gulp_stats row)"
    assert "never processed" in caplog.text


def test_t2_miss_with_no_gulp_at_all(conn, caplog):
    with caplog.at_level("WARNING"):
        reason = d.t2_miss_reason(conn, None, None, None)
    assert reason == "lost at T2: the gulp could not be identified"


def test_skipped_wins_over_shed(conn):
    """t2d skips the gulp before it ever sheds, so say the earlier cause."""
    _gulp_stats(conn, 432, skipped=1, n_jobs=3, n_shed=100, n_cands=200)
    assert "skipped incomplete" in d.t2_miss_reason(conn, OBS, 432, _Match())


def test_gulp_index_is_samp_over_8192():
    """Checked against shot 660: cluster samp 3543044 is stored as gulp 432."""
    assert d.GULP_SAMPS == 8192
    assert 3543044 // d.GULP_SAMPS == 432
