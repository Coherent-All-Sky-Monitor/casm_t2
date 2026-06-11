"""DBSCAN clustering of T1 candidates.

A single astrophysical pulse fires many T1 trials: neighbouring DM steps,
several boxcar widths, a few adjacent sky beams, and a spread of arrival
samples comparable to the boxcar width. Broadband RFI does the same but
across *many* beams at once. Clustering collapses both into one object per
physical event, so triggering logic reasons about events, not trials, and
the beam extent of a cluster becomes the primary RFI discriminator.

Distance is cityblock (L1) over four scaled axes::

    |d_samp| / samp_scale + |d_dm_idx| / dm_idx_scale
        + |d_log2(width)| / width_scale + |d_beam| / beam_scale  <=  eps

L1 keeps the axes interpretable: each scale is "how far apart two trials
can be on this axis alone and still be the same event". Scales were tuned
with t2-replay on live data (2026-06-10, see casm_t2 MEMORY.md).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN

from casm_t2.wire import Candidate


@dataclass(frozen=True, slots=True)
class ClusterParams:
    """DBSCAN axis scales (units per eps) and density requirements."""

    eps: float = 1.0
    min_samples: int = 5
    samp_scale: float = 64.0     # samples, ~67 ms at 1.048576 ms/sample
    dm_idx_scale: float = 32.0   # DM trial steps
    width_scale: float = 2.0     # log2(boxcar width) steps
    beam_scale: float = 4.0      # adjacent sky beams


@dataclass(slots=True)
class Cluster:
    """One clustered event: its peak trial plus the membership envelope."""

    peak: Candidate              # highest-SNR member
    n_members: int
    n_beams: int                 # distinct beams spanned
    beam_lo: int
    beam_hi: int
    dm_lo: float
    dm_hi: float
    samp_lo: int
    samp_hi: int

    @property
    def is_noise(self) -> bool:
        return self.n_members == 1


def _features(cands: list[Candidate], p: ClusterParams) -> np.ndarray:
    x = np.empty((len(cands), 4))
    for i, c in enumerate(cands):
        x[i] = (c.samp / p.samp_scale,
                c.dm_idx / p.dm_idx_scale,
                np.log2(max(c.width, 1)) / p.width_scale,
                c.beam / p.beam_scale)
    return x


def _dedup_grid(cands: list[Candidate],
                p: ClusterParams) -> tuple[list[Candidate], list[int]]:
    """Collapse trials onto a half-scale grid, keeping the best per cell.

    Persistent RFI can emit 10k+ trials per gulp in one beam, which makes
    DBSCAN's neighbour search the latency bottleneck. Cells of half the
    cluster scale on each axis are finer than anything DBSCAN would
    separate, so this only removes redundancy. Returns the cell
    representatives (highest SNR) and each cell's raw trial count.
    """
    cells: dict[tuple, list] = {}
    for c in cands:
        key = (c.samp // max(int(p.samp_scale / 2), 1),
               c.dm_idx // max(int(p.dm_idx_scale / 2), 1),
               int(np.log2(max(c.width, 1))),
               c.beam)
        cur = cells.get(key)
        if cur is None:
            cells[key] = [c, 1]
        else:
            cur[1] += 1
            if c.snr > cur[0].snr:
                cur[0] = c
    reps = [v[0] for v in cells.values()]
    counts = [v[1] for v in cells.values()]
    return reps, counts


def _envelope(members: list[Candidate], n_raw: int) -> Cluster:
    peak = max(members, key=lambda c: c.snr)
    beams = {c.beam for c in members}
    return Cluster(
        peak=peak,
        n_members=n_raw,
        n_beams=len(beams),
        beam_lo=min(beams),
        beam_hi=max(beams),
        dm_lo=min(c.dm for c in members),
        dm_hi=max(c.dm for c in members),
        samp_lo=min(c.samp for c in members),
        samp_hi=max(c.samp for c in members),
    )


def cluster_candidates(cands: list[Candidate],
                       params: ClusterParams = ClusterParams()) -> list[Cluster]:
    """Cluster one coalesced window of candidates.

    Trials are first collapsed onto a half-scale grid (see _dedup_grid),
    then DBSCAN groups the cell representatives. Returns one Cluster per
    DBSCAN cluster plus one singleton per noise point: a bright narrow
    event that fired few trials must still be able to reach the trigger
    logic, so density alone never discards anything — downstream filters
    decide using n_members/n_beams/SNR. Cluster.n_members counts raw T1
    trials, not deduplicated cells.
    """
    if not cands:
        return []
    if len(cands) == 1:
        return [_envelope(cands, 1)]

    reps, counts = _dedup_grid(cands, params)
    if len(reps) == 1:
        return [_envelope(reps, sum(counts))]

    labels = DBSCAN(eps=params.eps, min_samples=params.min_samples,
                    metric="cityblock").fit_predict(_features(reps, params))

    groups: dict[int, list[int]] = {}
    noise: list[int] = []
    for i, lab in enumerate(labels):
        if lab == -1:
            noise.append(i)
        else:
            groups.setdefault(int(lab), []).append(i)

    out = [_envelope([reps[i] for i in g], sum(counts[i] for i in g))
           for g in groups.values()]
    out.extend(_envelope([reps[i]], counts[i]) for i in noise)
    out.sort(key=lambda cl: -cl.peak.snr)
    return out
