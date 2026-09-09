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
