"""Storm cap: a per-beam quota, then a bounded global truncation.

Stage 1 (per-beam quota) keeps the shed fair across the sky but is not a
bound — its allowance is quota x populated beams. Stage 2 makes the cap a
real bound, and its per-beam floor is what stops a bright storm elsewhere
from evicting a real single-beam FRB.
"""

import math

from casm_t2.apps.t2d import BEAM_FLOOR, BEAM_QUOTA_DIVISOR, apply_storm_cap


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


def test_uniformly_spread_storm_is_bounded_by_stage_two(make_cand):
    """Stage 1 alone would pass this straight through.

    80k trials over all 512 beams is ~156 per beam, under the 313 quota, so
    the per-beam stage sheds nothing and its allowance (313 x 512 = 160k) is
    no bound at all. Stage 2 has to bring it down to the cap plus the floor.
    """
    cands = [make_cand(snr=float(i % 100), beam=i % 512) for i in range(80_000)]
    keep, n_shed = apply_storm_cap(cands, 20_000)

    assert math.ceil(20_000 / BEAM_QUOTA_DIVISOR) == 313   # stage-1 quota
    assert len(keep) <= 20_000 + BEAM_FLOOR * 512
    assert len(keep) + n_shed == 80_000
    assert n_shed > 0


def test_jul_31_storm_width_zero_across_every_beam(make_cand):
    """The gulp that wedged t2d: 80k width-0 spikes in all 512 beams.

    `veto_widths: [6]` does not touch width 0, so the cap is the only thing
    standing between this gulp and a 100+ second DBSCAN call.
    """
    cands = [make_cand(snr=10.0 + (i % 400) * 0.05, width=0, beam=i % 512)
             for i in range(80_000)]
    keep, n_shed = apply_storm_cap(cands, 20_000)

    assert len(keep) <= 20_000 + BEAM_FLOOR * 512   # ~22k, the real bound
    assert len(keep) + n_shed == 80_000
    # every beam is still represented — the sky is not silently narrowed
    assert len({c.beam for c in keep}) == 512


def test_stage_two_floor_keeps_a_faint_beams_top_four(make_cand):
    """A beam entirely below the global cutoff still keeps its best four.

    Beams 0-98 are bright (S/N 100+) and fill the global top-640 on their
    own; beam 99 is faint (S/N 10-29) and would be wiped out by a plain
    global truncation. That is the single-beam-FRB-during-a-storm case.
    """
    cands = ([make_cand(snr=100.0 + i, beam=b) for b in range(99) for i in range(20)]
             + [make_cand(snr=10.0 + i, beam=99) for i in range(20)])
    keep, n_shed = apply_storm_cap(cands, 640, beam_floor=4)

    assert math.ceil(640 / BEAM_QUOTA_DIVISOR) == 10   # stage-1 quota
    faint = sorted((c.snr for c in keep if c.beam == 99), reverse=True)
    # stage 1 left beam 99 its top 10 (S/N 19..10); stage 2's floor keeps 4
    assert faint == [29.0, 28.0, 27.0, 26.0]
    assert len(keep) == 640 + 4
    assert len(keep) + n_shed == len(cands)


def test_stage_two_keeps_the_globally_brightest(make_cand):
    """The truncation is by S/N, not arbitrary."""
    cands = ([make_cand(snr=float(i), beam=b) for b in range(99) for i in range(20)]
             + [make_cand(snr=1000.0, beam=99)])
    keep, _ = apply_storm_cap(cands, 640, beam_floor=4)

    assert max(c.snr for c in keep) == 1000.0
    assert any(c.snr == 1000.0 and c.beam == 99 for c in keep)


def test_survivors_come_out_in_input_order(make_cand):
    # samp makes every candidate distinct so index() is meaningful
    cands = [make_cand(snr=float(i % 50), samp=i, beam=i % 100)
             for i in range(3000)]
    keep, _ = apply_storm_cap(cands, 640, beam_floor=4)

    positions = [cands.index(c) for c in keep]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(keep)   # no duplicates from the union
