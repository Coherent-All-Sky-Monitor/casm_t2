"""Width veto: drop whole boxcar-width indices before clustering.

Width index 6 (the 67 ms boxcar) is 97-98.6% of stored rows on a quiet day
— red-noise junk at DM >= 200 that pays full DBSCAN cost and triggers
nothing.
"""

from casm_t2.apps.t2d import apply_width_veto


def test_width_six_is_filtered(make_cand):
    cands = [make_cand(width=w, beam=w) for w in range(8)]
    keep, n_vetoed = apply_width_veto(cands, {6})

    assert n_vetoed == 1
    assert [c.width for c in keep] == [0, 1, 2, 3, 4, 5, 7]


def test_count_is_right_with_many_vetoed(make_cand):
    cands = ([make_cand(width=6) for _ in range(97)]
             + [make_cand(width=2) for _ in range(3)])
    keep, n_vetoed = apply_width_veto(cands, {6})

    assert n_vetoed == 97
    assert len(keep) == 3
    assert all(c.width == 2 for c in keep)


def test_multiple_vetoed_widths(make_cand):
    cands = [make_cand(width=w) for w in range(8)]
    keep, n_vetoed = apply_width_veto(cands, {6, 7})

    assert n_vetoed == 2
    assert [c.width for c in keep] == [0, 1, 2, 3, 4, 5]


def test_empty_veto_list_is_a_no_op(make_cand):
    cands = [make_cand(width=w) for w in range(8)]
    keep, n_vetoed = apply_width_veto(cands, [])

    assert n_vetoed == 0
    assert keep is cands


def test_a_list_works_as_well_as_a_set(make_cand):
    cands = [make_cand(width=w) for w in range(8)]
    keep, n_vetoed = apply_width_veto(cands, [6])
    assert n_vetoed == 1
    assert all(c.width != 6 for c in keep)


def test_everything_vetoed_yields_an_empty_gulp(make_cand):
    cands = [make_cand(width=6) for _ in range(10)]
    keep, n_vetoed = apply_width_veto(cands, {6})
    assert keep == []
    assert n_vetoed == 10


def test_no_candidates(make_cand):
    assert apply_width_veto([], {6}) == ([], 0)


def test_order_and_identity_are_preserved(make_cand):
    """Survivors come through untouched — no reordering, no copies of values."""
    cands = [make_cand(width=1, snr=s) for s in (30.0, 10.0, 20.0)]
    keep, _ = apply_width_veto(cands, {6})
    assert [c.snr for c in keep] == [30.0, 10.0, 20.0]
