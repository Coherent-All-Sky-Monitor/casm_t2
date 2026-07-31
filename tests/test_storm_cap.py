"""Storm cap: a per-beam S/N quota, not a global top-N.

A global top-N would let a bright RFI storm occupying a few beams crowd out
a real single-beam FRB. The quota is per global beam for exactly that
reason, which also means the kept total scales with populated beams rather
than being clamped to the cap.
"""

import math

from casm_t2.apps.t2d import BEAM_QUOTA_DIVISOR, apply_storm_cap


def test_under_cap_is_untouched(make_cand):
    cands = [make_cand(snr=10.0 + i, beam=i % 4) for i in range(50)]
    keep, n_shed = apply_storm_cap(cands, 100)

    assert n_shed == 0
    assert keep is cands


def test_exactly_at_cap_is_untouched(make_cand):
    cands = [make_cand(beam=i % 4) for i in range(64)]
    keep, n_shed = apply_storm_cap(cands, 64)
    assert n_shed == 0
    assert keep is cands


def test_quota_is_ceil_cap_over_64(make_cand):
    assert math.ceil(20 / BEAM_QUOTA_DIVISOR) == 1
    cands = [make_cand(snr=float(i), beam=b)
             for b in range(3) for i in range(10)]
    keep, n_shed = apply_storm_cap(cands, 20)   # 30 > 20, quota = 1 per beam

    assert len(keep) == 3
    assert n_shed == 27
    assert sorted(c.snr for c in keep) == [9.0, 9.0, 9.0]


def test_keeps_the_top_n_by_snr_within_each_beam(make_cand):
    cands = [make_cand(snr=float(i), beam=b)
             for b in range(4) for i in range(20)]
    keep, n_shed = apply_storm_cap(cands, 70)   # 80 > 70, quota = 2 per beam

    assert n_shed == 80 - 4 * 2
    by_beam = {}
    for c in keep:
        by_beam.setdefault(c.beam, []).append(c.snr)
    assert set(by_beam) == {0, 1, 2, 3}
    for beam, snrs in by_beam.items():
        assert sorted(snrs) == [18.0, 19.0], beam


def test_a_quiet_beam_survives_a_storm_in_another_beam(make_cand):
    """The whole point: one faint candidate alone in its beam is kept.

    A global top-N over this gulp would drop it — every one of the storm
    beam's candidates is brighter.
    """
    storm = [make_cand(snr=100.0 + i, beam=7) for i in range(500)]
    lone = make_cand(snr=12.5, beam=200)
    keep, n_shed = apply_storm_cap(storm + [lone], 128)   # quota = 2

    assert lone in keep
    assert n_shed == 500 - 2
    assert sorted(c.snr for c in keep if c.beam == 7) == [598.0, 599.0]


def test_shed_count_matches_what_was_removed(make_cand):
    cands = [make_cand(snr=float(i), beam=i % 9) for i in range(900)]
    keep, n_shed = apply_storm_cap(cands, 64)   # quota = 1

    assert len(keep) + n_shed == len(cands)
    assert len(keep) == 9   # nine populated beams, one each


def test_beams_below_quota_are_not_padded_or_dropped(make_cand):
    cands = ([make_cand(snr=float(i), beam=0) for i in range(200)]
             + [make_cand(snr=5.0, beam=1)])
    keep, _ = apply_storm_cap(cands, 192)   # 201 > 192, quota = 3 per beam

    assert sum(1 for c in keep if c.beam == 0) == 3
    assert sum(1 for c in keep if c.beam == 1) == 1


def test_cap_of_zero_disables_the_cap(make_cand):
    cands = [make_cand(beam=i % 4) for i in range(500)]
    keep, n_shed = apply_storm_cap(cands, 0)
    assert n_shed == 0
    assert keep is cands


def test_quota_never_falls_below_one(make_cand):
    """A tiny cap must not shed a beam down to nothing."""
    cands = [make_cand(snr=float(i), beam=0) for i in range(10)]
    keep, n_shed = apply_storm_cap(cands, 1)   # ceil(1/64) = 1
    assert len(keep) == 1
    assert keep[0].snr == 9.0
    assert n_shed == 9


def test_beam_concentrated_storm_is_cut_hard(make_cand):
    """The case the cap is for: 80k trials piled into 8 RFI beams."""
    cands = [make_cand(snr=float(i % 1000), beam=i % 8) for i in range(80_000)]
    keep, n_shed = apply_storm_cap(cands, 20_000)   # quota = 313

    assert len(keep) == 8 * 313
    assert n_shed == 80_000 - 8 * 313


def test_uniformly_spread_storm_passes_the_quota_untouched(make_cand):
    """Documented ceiling of a per-beam quota, not a bug.

    80k trials spread evenly over all 512 beams is ~156 per beam, under the
    313 quota, so the cap trips but sheds nothing: quota x populated beams
    (160,256) is well above any observed gulp. A storm that is both huge and
    uniform across every beam is handled by `veto_widths`, which removes the
    97-98.6% width-6 bulk before this function ever sees it.
    """
    cands = [make_cand(snr=float(i % 100), beam=i % 512) for i in range(80_000)]
    keep, n_shed = apply_storm_cap(cands, 20_000)

    assert math.ceil(20_000 / BEAM_QUOTA_DIVISOR) == 313
    assert n_shed == 0
    assert len(keep) == 80_000
