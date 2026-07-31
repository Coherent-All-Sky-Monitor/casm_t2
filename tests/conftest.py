"""Shared fixtures: in-memory event DB and candidate/cluster builders.

Nothing here opens a socket, sleeps, or touches the real database.
"""

import pytest

from casm_t2 import db
from casm_t2.cluster import Cluster
from casm_t2.wire import Candidate


@pytest.fixture
def conn():
    """A fresh in-memory event DB with the current schema."""
    c = db.connect(":memory:")
    yield c
    c.close()


def _make_cand(snr=20.0, beam=5, width=3, samp=1000, dm=250.0, dm_idx=100):
    return Candidate(snr=snr, samp=samp, time_days=0.0, width=width,
                     dm_idx=dm_idx, dm=dm, beam=beam)


def _make_cluster(snr=20.0, beam=5):
    c = _make_cand(snr=snr, beam=beam)
    return Cluster(peak=c, n_members=4, n_beams=1, beam_lo=beam, beam_hi=beam,
                   dm_lo=c.dm - 1, dm_hi=c.dm + 1,
                   samp_lo=c.samp - 10, samp_hi=c.samp + 10)


def _cluster_row(name, snr=20.0, beam=5, gulp=1):
    """One row in the tuple shape db.insert_clusters expects."""
    return (_make_cluster(snr=snr, beam=beam), "2026-07-31-00:00:00", gulp,
            "2026-07-31T00:00:00.000+00:00", "B", "", name)


@pytest.fixture
def make_cand():
    return _make_cand


@pytest.fixture
def make_cluster():
    return _make_cluster


@pytest.fixture
def cluster_row():
    return _cluster_row
