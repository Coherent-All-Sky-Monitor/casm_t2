"""Width veto: drop whole boxcar-width indices at parse time.

Width index 6 (the 67 ms boxcar) is 97-98.6% of stored rows on a quiet day
— red-noise junk at DM >= 200 that pays full DBSCAN cost and triggers
nothing.

The filter sits in _handle rather than _flush_later so that the fast path,
which fires dumps per batch before clustering, never sees vetoed trials
either. Accounting is still per gulp.
"""

from casm_t2.apps.t2d import apply_width_veto


def trigger_rows(d):
    return d.conn.execute("SELECT candname, action, detail FROM triggers").fetchall()


def gulp_stats(d):
    return d.conn.execute(
        "SELECT n_cands, n_vetoed, n_shed, n_stored FROM gulp_stats").fetchall()


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


# --------------------------------------------------------- fast-path coverage
#
# The veto lives at parse time precisely so these hold. A width-6 candidate
# well above tier B used to reach _fast_path and could fire a dump before
# clustering ever ran.

BRIGHT_W6 = dict(snr=45.0, width=6, dm=520.0, beam=300, samp=3000)


def test_vetoed_candidate_cannot_fire_the_fast_path(daemon, ingest, make_cand):
    d = daemon(veto_widths=[6], trigger={"fast_path": True})
    ingest(d, [make_cand(**BRIGHT_W6)])

    assert trigger_rows(d) == []
    assert d.pending_fast == {}


def test_same_candidate_does_fire_when_the_veto_is_empty(daemon, ingest, make_cand):
    """Control: the payload really is fast-path-worthy."""
    d = daemon(veto_widths=[], trigger={"fast_path": True})
    ingest(d, [make_cand(**BRIGHT_W6)])

    rows = trigger_rows(d)
    assert len(rows) == 1
    assert rows[0][1] == "suppressed_commissioning"
    assert rows[0][2] == "fast:tier_A"


def test_vetoed_candidate_stays_out_of_the_context_deque(daemon, ingest, make_cand):
    d = daemon(veto_widths=[6], trigger={"fast_path": True})
    ingest(d, [make_cand(**BRIGHT_W6), make_cand(snr=20.0, width=2, beam=7)])

    assert [m[4] for m in d.context] == [2]   # width column, no 6


def test_vetoed_count_reaches_gulp_stats(daemon, ingest, make_cand):
    d = daemon(veto_widths=[6])
    cands = ([make_cand(width=6, samp=1000 + i, beam=i % 4) for i in range(30)]
             + [make_cand(width=2, samp=2000 + i, beam=7) for i in range(5)])
    ingest(d, cands)

    (n_cands, n_vetoed, n_shed, _), = gulp_stats(d)
    assert n_cands == 35        # raw arrivals, reconstructed
    assert n_vetoed == 30
    assert n_shed == 0


def test_veto_count_aggregates_across_the_jobs_of_one_gulp(daemon, ingest, make_cand):
    """Filtering is per batch; the number reported is per gulp."""
    d = daemon(veto_widths=[6])
    batches = [[make_cand(width=6, samp=1000 + j * 100 + i, beam=j) for i in range(10)]
               + [make_cand(width=1, samp=5000 + j * 100, beam=j)]
               for j in range(8)]
    ingest(d, *batches)

    (n_cands, n_vetoed, _, _), = gulp_stats(d)
    assert n_cands == 8 * 11
    assert n_vetoed == 8 * 10


def test_fully_vetoed_gulp_still_records_the_count(daemon, ingest, make_cand):
    """Every trial vetoed: the gulp goes down the quiet path, not silently."""
    d = daemon(veto_widths=[6])
    ingest(d, [make_cand(width=6, samp=1000 + i) for i in range(12)])

    (n_cands, n_vetoed, n_shed, n_stored), = gulp_stats(d)
    assert (n_cands, n_vetoed, n_shed, n_stored) == (12, 12, 0, 0)


def test_disabled_veto_leaves_accounting_at_zero(daemon, ingest, make_cand):
    d = daemon(veto_widths=[])
    ingest(d, [make_cand(width=6, samp=1000 + i) for i in range(12)])

    (n_cands, n_vetoed, _, _), = gulp_stats(d)
    assert n_cands == 12
    assert n_vetoed == 0
