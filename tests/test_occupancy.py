"""Beam-occupancy veto: multi-beam coincidence on raw candidates.

The discriminant is distinct beams within +-window_samp of a cluster peak,
counted on RAW candidates (DBSCAN fragments broadband junk into single-beam
clusters, so cluster n_beams cannot substitute). Real sources occupy 0-2
beams, broadband zero-DM junk 40-64 (iteration-2 closure).
"""

from casm_t2.apps.t2d import build_beam_footprint, occupancy_beams


def _footprint(make_cand, spec):
    """spec: list of (samp, beam) pairs."""
    return build_beam_footprint(
        [make_cand(samp=s, beam=b) for s, b in spec])


def test_footprint_is_sorted_by_samp(make_cand):
    samps, beams = _footprint(make_cand, [(500, 3), (100, 1), (300, 2)])
    assert samps == [100, 300, 500]
    assert beams == [1, 2, 3]


def test_single_beam_event_counts_one(make_cand):
    samps, beams = _footprint(make_cand, [(1000, 7)] * 20)
    assert occupancy_beams(samps, beams, 1000, 256) == 1


def test_junk_footprint_counts_all_beams_in_window(make_cand):
    spec = [(1000 + i, i) for i in range(40)]  # 40 beams within 40 samples
    samps, beams = _footprint(make_cand, spec)
    assert occupancy_beams(samps, beams, 1020, 256) == 40


def test_candidates_outside_window_do_not_count(make_cand):
    spec = [(1000, 1), (1000, 2), (5000, 3), (5000, 4), (5000, 5)]
    samps, beams = _footprint(make_cand, spec)
    assert occupancy_beams(samps, beams, 1000, 256) == 2
    assert occupancy_beams(samps, beams, 5000, 256) == 3


def test_window_boundaries_inclusive(make_cand):
    spec = [(744, 1), (745, 2), (1255, 3), (1256, 4)]
    samps, beams = _footprint(make_cand, spec)
    # peak 1000, window 255: [745, 1255] inclusive
    assert occupancy_beams(samps, beams, 1000, 255) == 2


def test_duplicate_beams_counted_once(make_cand):
    spec = [(1000, 9), (1001, 9), (1002, 9), (1003, 12)]
    samps, beams = _footprint(make_cand, spec)
    assert occupancy_beams(samps, beams, 1001, 256) == 2


def test_empty_footprint(make_cand):
    samps, beams = build_beam_footprint([])
    assert occupancy_beams(samps, beams, 1000, 256) == 0


def test_real_event_far_from_junk_survives(make_cand):
    """A single-beam event elsewhere in the gulp must not inherit the junk
    footprint: this is why the veto is time-windowed, not per-gulp."""
    junk = [(2000 + i, i % 50) for i in range(200)]
    real = [(7000, 300)]
    samps, beams = _footprint(make_cand, junk + real)
    assert occupancy_beams(samps, beams, 2100, 256) >= 8   # junk vetoed
    assert occupancy_beams(samps, beams, 7000, 256) == 1   # real one passes
