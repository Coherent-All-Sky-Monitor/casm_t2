"""DM-floor veto: wide clusters whose lowest DM sits at hella's first trial.

A bright zero-DM impulse of 10-20 ms dedisperses at DM 20 with only a factor
of ~3 loss, so it clusters at dm_lo = 20.5 (the first reported trial above
DM_MIN 20) with a wide boxcar. Hella never searches DM 0, so t2d has to
recognise the leak from the cluster shape: dm_lo at the floor and width
index >= min_width. Tagged dm_floor, stored, never dumped.
"""

from casm_t2.cluster import Cluster
from casm_t2.wire import Candidate

FILT = {"beam_veto": [], "max_nbeam": 32, "dm_floor": 20.0,
        "dm_floor_veto": {"max_dm_lo": 20.6, "min_width": 4}}


def _cluster(dm_lo, width, dm=22.1, snr=21.0, beam=449):
    c = Candidate(snr=snr, samp=1000, time_days=0.0, width=width,
                  dm_idx=1, dm=dm, beam=beam)
    return Cluster(peak=c, n_members=20, n_beams=1, beam_lo=beam, beam_hi=beam,
                   dm_lo=dm_lo, dm_hi=dm + 30, samp_lo=990, samp_hi=1010)


def test_floor_and_wide_is_tagged_and_never_triggers(daemon):
    d = daemon(filters=FILT)
    tier, tags = d._classify(_cluster(dm_lo=20.5, width=5), None)
    assert tier == "B"
    assert "dm_floor" in tags
    assert d._wants_trigger(_cluster(dm_lo=20.5, width=5), tier, tags) is None


def test_floor_but_narrow_is_not_tagged(daemon):
    d = daemon(filters=FILT)
    tier, tags = d._classify(_cluster(dm_lo=20.5, width=3), None)
    assert "dm_floor" not in tags
    assert d._wants_trigger(_cluster(dm_lo=20.5, width=3), tier, tags) == "tier_B"


def test_wide_but_off_the_floor_is_not_tagged(daemon):
    d = daemon(filters=FILT)
    tier, tags = d._classify(_cluster(dm_lo=27.3, width=5, dm=28.3), None)
    assert "dm_floor" not in tags
    assert d._wants_trigger(_cluster(dm_lo=27.3, width=5, dm=28.3), tier, tags) == "tier_B"


def test_disabled_when_max_dm_lo_is_zero(daemon):
    d = daemon(filters={**FILT, "dm_floor_veto": {"max_dm_lo": 0}})
    _, tags = d._classify(_cluster(dm_lo=20.5, width=5), None)
    assert "dm_floor" not in tags


def test_absent_config_block_disables_it(daemon):
    d = daemon(filters={"beam_veto": [], "max_nbeam": 32, "dm_floor": 20.0})
    _, tags = d._classify(_cluster(dm_lo=20.5, width=5), None)
    assert "dm_floor" not in tags


def test_shipped_config_enables_it():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config/t2d.yaml").read_text())
    assert cfg["filters"]["dm_floor_veto"] == {"max_dm_lo": 20.6, "min_width": 4}
    assert cfg["trigger"]["storm_lockout_s"] == 300


def test_coincident_fragment_off_the_floor_is_tagged_too(daemon, ingest, make_cand):
    """The bowtie fragments: the DM 26 cluster 0.1 s from a floor cluster is
    the same impulse and must not trigger either."""
    d = daemon(filters=FILT, occupancy={"min_beams": 0, "window_samp": 256})
    floor = [make_cand(snr=21.0, beam=449, width=5, samp=1000 + i, dm=20.5 + 0.5 * i, dm_idx=1 + i)
             for i in range(6)]
    upper = [make_cand(snr=20.0, beam=241, width=5, samp=1100 + i, dm=26.2, dm_idx=8)
             for i in range(6)]
    ingest(d, floor + upper)
    tags = [r[0] for r in d.conn.execute("SELECT tags FROM clusters")]
    assert tags and all("dm_floor" in t for t in tags)
    assert d.conn.execute("SELECT count(*) FROM triggers WHERE action='dumped'").fetchone()[0] == 0
