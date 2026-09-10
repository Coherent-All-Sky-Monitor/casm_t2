"""DBSCAN clustering of T1 candidates.

A single astrophysical pulse fires many T1 trials: neighbouring DM steps,
several boxcar widths, a few adjacent sky beams, and a spread of arrival
samples comparable to the boxcar width. Broadband RFI does the same but
across *many* beams at once. Clustering collapses both into one object per
physical event, so triggering logic reasons about events, not trials, and
the sky extent of a cluster becomes the primary RFI discriminator.

Distance is Euclidean over five scaled axes::

    sqrt( (d_samp / samp_scale)^2 + (d_dm_idx / dm_idx_scale)^2
          + (d_log2(width) / width_scale)^2
          + (dx / beam_fwhm_x_deg)^2 + (dy / beam_fwhm_y_deg)^2 )  <=  eps

where (x, y) is the beam's position on a tangent plane about the zenith,
in degrees, x East-West and y North-South. Each scale keeps its meaning:
an offset on one axis alone of exactly one scale is a distance of exactly
1, so eps 1.0 still reads as "one scale on any single axis". The
time/DM/width scales were tuned with t2-replay on live data (2026-06-10,
see casm_t2 MEMORY.md).

The sky link on each axis is the beam's own FWHM on that axis
(2026-09-09), so two trials are linked when they fall within one beam width
of each other. The beam is treated as a standard ellipse aligned with
alt/az, from ``bf_weights_generator.compute_beam_fwhm``, and it follows the
deployed weights: t2d takes the pair off the weights-registry product that
was live for the gulp (about 19.3 deg E-W by 3.9 deg N-S for the 17-antenna
products of September 2026) and falls back to the configured values below
only for a product that carries no ellipse. Pointing independent. It is very much wider E-W than N-S, so
one source lights up a row of beams rather than a circle of them, and an
isotropic link either splits that row or merges unrelated sky.

The metric was cityblock (L1) until 2026-09-09. Two things forced the
change, both about the sky pair. The sky axes are two coordinates of ONE
physical quantity — an angle on the sky — so under L1 the cost of a
cross-beam link was `|dx| + |dy|`, which is up to sqrt(2) times the real
separation and depends on how the beam pair happens to lie relative to the
projection axes. Measured on the deployed grid, the median great-circle
nearest-neighbour distance is 3.13 deg but the median L1 cost of reaching
that same neighbour was 3.58 deg, so at a 4 deg scale only 61% of beams
could reach their own nearest neighbour even for two otherwise identical
trials. Under Euclidean the sky contribution is exactly the tangent-plane
separation over the scale, which is what "one beam spacing" was always
meant to mean.

The other axes now also add in quadrature rather than linearly, which is
more permissive for a trial that is offset a little on several axes at
once — the usual shape of a real pulse's trial cloud — and unchanged for a
trial offset on one axis alone.

Why sky and not beam index (2026-09-09). The deployed 512-beam grid is not
sky-ordered: consecutive beam indices are a median 16 deg apart while true
sky neighbours are 3.1 deg, and only 11% of a beam's six sky-nearest
neighbours lie within +-4 index. Clustering on ``beam / beam_scale`` was
therefore clustering on nothing: a point source spanning two adjacent sky
beams fragmented into two clusters, ``n_beams`` understated real
footprints, and the ``rfi_wide`` cut (n_beams > 32) never fired on
broadband RFI that was in fact lit up across the whole sky.

The beam-index axis survives only as a fallback for when no pointing table
is available (an unregistered weights product, a partial deploy); it is
worse than useless as a similarity measure, so a run on the fallback logs
a warning and should be treated as degraded.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN

from casm_t2.wire import Candidate

logger = logging.getLogger(__name__)

DEG = 180.0 / math.pi

_warned_no_pointings = False


@dataclass(frozen=True, slots=True)
class ClusterParams:
    """DBSCAN axis scales (units per eps) and density requirements."""

    eps: float = 1.0
    min_samples: int = 5
    samp_scale: float = 64.0     # samples, ~67 ms at 1.048576 ms/sample
    dm_idx_scale: float = 32.0   # DM trial steps
    width_scale: float = 2.0     # steps of the width column (already log2 samples)
    # Beam FWHM per axis, degrees. This IS the sky link scale: two trials
    # within one beam width of each other on both axes are one event.
    # Standard alt/az-aligned ellipse from compute_beam_fwhm. t2d overrides
    # these per weights product from the registry; the defaults here are the
    # fallback for a product with no ellipse.
    beam_fwhm_x_deg: float = 18.1   # E-W
    beam_fwhm_y_deg: float = 3.9    # N-S
    beam_scale: float = 4.0      # FALLBACK ONLY: beam-index axis, no pointings


class SkyTable:
    """Per-beam sky positions for one weights product.

    Holds the 512-beam alt/az table plus its tangent-plane projection about
    the zenith, which is the pair of axes DBSCAN clusters on. Beams sit
    above alt ~22 deg on the deployed grid, so a zenith-centred projection
    is well behaved everywhere on it.

    The projection is the sine (orthographic) one the task specifies::

        x = cos(alt) * sin(az) * 180/pi
        y = cos(alt) * cos(az) * 180/pi

    It is exact at the zenith and compresses radial separations by
    ``(zenith_angle) / sin(zenith_angle)`` away from it — about 8% at
    alt 45 and 22% at alt 22, i.e. the sky axis is slightly *more*
    permissive low down. That is the safe direction (it merges rather than
    fragments) and is well inside the factor the 4 deg scale carries.

    ``separation_deg`` is a true great-circle separation, not a
    tangent-plane distance, so ``Cluster.sky_extent_deg`` is a real angle.
    Clustering itself uses the tangent-plane distance, which is the same
    thing to well under a percent at the separations that matter.
    """

    __slots__ = ("weights_id", "alt_deg", "az_deg", "x", "y", "_unit", "n")

    def __init__(self, alt_deg, az_deg, weights_id: str | None = None):
        alt = np.asarray(alt_deg, dtype=float)
        az = np.asarray(az_deg, dtype=float)
        if alt.ndim != 1 or alt.shape != az.shape or alt.size == 0:
            raise ValueError("alt_deg and az_deg must be equal-length 1-D tables")
        self.weights_id = weights_id
        self.alt_deg = alt
        self.az_deg = az
        self.n = int(alt.size)
        a, z = np.radians(alt), np.radians(az)
        ca = np.cos(a)
        self.x = ca * np.sin(z) * DEG
        self.y = ca * np.cos(z) * DEG
        # unit vectors in the horizontal frame, for exact separations
        self._unit = np.stack([ca * np.sin(z), ca * np.cos(z), np.sin(a)], axis=1)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_pointings(cls, pointings: dict | None) -> "SkyTable | None":
        """Build from ``weights_registry.pointings_for`` output, or None."""
        if not pointings:
            return None
        alt, az = pointings.get("alt_deg"), pointings.get("az_deg")
        if not alt or not az or len(alt) != len(az):
            return None
        try:
            return cls(alt, az, weights_id=pointings.get("weights_id"))
        except ValueError:
            return None

    # -- lookups ---------------------------------------------------------
    def has(self, beam: int) -> bool:
        return 0 <= beam < self.n

    def xy(self, beam: int) -> tuple[float, float]:
        """Tangent-plane (x, y) of a beam, in degrees."""
        return float(self.x[beam]), float(self.y[beam])

    def altaz(self, beam: int) -> tuple[float, float]:
        return float(self.alt_deg[beam]), float(self.az_deg[beam])

    def separation_deg(self, b1: int, b2: int) -> float:
        """Great-circle separation between two beams, in degrees."""
        d = float(np.dot(self._unit[b1], self._unit[b2]))
        return float(np.degrees(math.acos(max(-1.0, min(1.0, d)))))

    def max_separation_deg(self, beams) -> float:
        """Largest pairwise separation across a set of beams (0 for one beam)."""
        idx = [b for b in sorted(set(beams)) if self.has(b)]
        if len(idx) < 2:
            return 0.0
        u = self._unit[idx]
        dots = np.clip(u @ u.T, -1.0, 1.0)
        return float(np.degrees(np.arccos(dots.min())))


#: neighbour_beams results, keyed by (weights_id, beam, fwhm_x, fwhm_y, scale).
#: One weights product is live for hours and the same handful of injection
#: beams is asked about repeatedly, so this is a few entries in practice.
_NEIGHBOUR_CACHE: dict[tuple, frozenset] = {}


def neighbour_beams(sky: "SkyTable | None", beam: int, fwhm_x_deg: float,
                    fwhm_y_deg: float, scale: float = 1.0) -> set[int]:
    """Beams within one beam ellipse of `beam` on the SKY, plus `beam` itself.

    Beam *indices* are not sky-ordered: consecutive indices on the deployed
    grid are a median 16 degrees apart, so "beam +-2" as an index window is
    not a statement about the sky at all. It can miss the beam a pulse
    actually landed in and admit beams most of a horizon away.

    The test is the same ellipse the clustering uses::

        (dx / fwhm_x)^2 + (dy / fwhm_y)^2 <= scale^2

    on the tangent-plane axes, with the FWHMs coming from the registry
    product (config fallback). Returns ``{beam}`` when there is no pointing
    table or the beam is off the table: callers that need an index window
    must ask for one explicitly, so a missing table can never silently look
    like a sky answer.
    """
    if sky is None or not sky.has(beam) or fwhm_x_deg <= 0 or fwhm_y_deg <= 0:
        return {int(beam)}
    key = (sky.weights_id, int(beam), float(fwhm_x_deg), float(fwhm_y_deg),
           float(scale))
    hit = _NEIGHBOUR_CACHE.get(key)
    if hit is None:
        x0, y0 = sky.xy(beam)
        dx = (sky.x - x0) / float(fwhm_x_deg)
        dy = (sky.y - y0) / float(fwhm_y_deg)
        inside = np.nonzero(dx * dx + dy * dy <= float(scale) ** 2)[0]
        hit = frozenset(int(b) for b in inside) | {int(beam)}
        _NEIGHBOUR_CACHE[key] = hit
    return set(hit)


def unproject(x_deg: float, y_deg: float) -> tuple[float, float]:
    """Inverse of the SkyTable projection: (x, y) degrees -> (alt, az) degrees.

    Exists so the projection can be round-tripped in tests and by anyone
    reading a tangent-plane figure back into pointing space.
    """
    r = math.hypot(x_deg, y_deg) / DEG        # = cos(alt)
    alt = math.degrees(math.acos(max(-1.0, min(1.0, r))))
    az = math.degrees(math.atan2(x_deg, y_deg)) % 360.0
    return alt, az


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
    # largest pairwise sky separation of the member beams, degrees; 0.0 for a
    # single-beam cluster and for clusters made without a pointing table
    sky_extent_deg: float = 0.0

    @property
    def is_noise(self) -> bool:
        return self.n_members == 1


def _features(cands: list[Candidate], p: ClusterParams,
              sky: SkyTable | None) -> np.ndarray:
    """Scaled feature matrix: 5 axes with a pointing table, 4 without."""
    if sky is None:
        x = np.empty((len(cands), 4))
        for i, c in enumerate(cands):
            x[i] = (c.samp / p.samp_scale,
                    c.dm_idx / p.dm_idx_scale,
                    c.width / p.width_scale,
                    c.beam / p.beam_scale)
        return x
    x = np.empty((len(cands), 5))
    for i, c in enumerate(cands):
        bx, by = (sky.xy(c.beam) if sky.has(c.beam) else (0.0, 0.0))
        x[i] = (c.samp / p.samp_scale,
                c.dm_idx / p.dm_idx_scale,
                c.width / p.width_scale,
                bx / p.beam_fwhm_x_deg,
                by / p.beam_fwhm_y_deg)
    return x


def _dedup_grid(cands: list[Candidate], p: ClusterParams,
                sky: SkyTable | None) -> tuple[list[Candidate], list[int]]:
    """Collapse trials onto a half-scale grid, keeping the best per cell.

    Persistent RFI can emit 10k+ trials per gulp in one beam, which makes
    DBSCAN's neighbour search the latency bottleneck. Cells of half the
    cluster scale on each axis are finer than anything DBSCAN would
    separate, so this only removes redundancy. Returns the cell
    representatives (highest SNR) and each cell's raw trial count.

    The spatial part of the key uses the same axes DBSCAN sees — half-scale
    cells of the tangent plane, one cell size per axis — plus the beam index
    itself. Cells alone would merge distinct beams (a half-scale x cell is
    far wider than the 3.1 deg beam spacing), and ``n_beams`` /
    ``sky_extent_deg`` would then understate the real footprint. Keeping the
    beam in the key makes the grid strictly finer than the sky axes, which
    is the invariant that matters.
    """
    half_x = max(p.beam_fwhm_x_deg / 2, 1e-6)
    half_y = max(p.beam_fwhm_y_deg / 2, 1e-6)
    cells: dict[tuple, list] = {}
    for c in cands:
        if sky is not None:
            bx, by = (sky.xy(c.beam) if sky.has(c.beam) else (0.0, 0.0))
            spatial = (math.floor(bx / half_x), math.floor(by / half_y), c.beam)
        else:
            spatial = (c.beam,)
        key = (c.samp // max(int(p.samp_scale / 2), 1),
               c.dm_idx // max(int(p.dm_idx_scale / 2), 1),
               int(c.width)) + spatial
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


def _envelope(members: list[Candidate], n_raw: int,
              sky: SkyTable | None = None) -> Cluster:
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
        sky_extent_deg=(round(sky.max_separation_deg(beams), 3)
                        if sky is not None else 0.0),
    )


def cluster_candidates(cands: list[Candidate],
                       params: ClusterParams = ClusterParams(),
                       sky: SkyTable | None = None) -> list[Cluster]:
    """Cluster one coalesced window of candidates.

    ``sky`` is the beam pointing table live at the time of these
    candidates (see :class:`SkyTable`). With it, beams enter as two
    tangent-plane degree axes scaled by ``beam_fwhm_x_deg`` and
    ``beam_fwhm_y_deg`` — one beam width per axis — and every cluster
    carries a real ``sky_extent_deg``. Without it, clustering falls back to
    the beam-index axis and ``beam_scale``, which is a poor similarity
    measure on the deployed non-sky-ordered grid — the fallback warns once
    per process and leaves ``sky_extent_deg`` at 0.0, so nothing downstream
    can be tagged on a number that was never measured.

    Trials are first collapsed onto a half-scale grid (see _dedup_grid),
    then DBSCAN groups the cell representatives. Returns one Cluster per
    DBSCAN cluster plus one singleton per noise point: a bright narrow
    event that fired few trials must still be able to reach the trigger
    logic, so density alone never discards anything — downstream filters
    decide using n_members/n_beams/sky_extent_deg/SNR. Cluster.n_members
    counts raw T1 trials, not deduplicated cells.
    """
    global _warned_no_pointings
    if sky is None and not _warned_no_pointings:
        _warned_no_pointings = True
        logger.warning(
            "no beam pointing table available: clustering on the beam-index "
            "axis (beam_scale=%.1f). Beam index is not sky-ordered, so sky "
            "neighbours will fragment and sky_extent_deg stays 0. Check the "
            "weights registry.", params.beam_scale)

    if not cands:
        return []
    if len(cands) == 1:
        return [_envelope(cands, 1, sky)]

    reps, counts = _dedup_grid(cands, params, sky)
    if len(reps) == 1:
        return [_envelope(reps, sum(counts), sky)]

    labels = DBSCAN(eps=params.eps, min_samples=params.min_samples,
                    metric="euclidean").fit_predict(_features(reps, params, sky))

    groups: dict[int, list[int]] = {}
    noise: list[int] = []
    for i, lab in enumerate(labels):
        if lab == -1:
            noise.append(i)
        else:
            groups.setdefault(int(lab), []).append(i)

    out = [_envelope([reps[i] for i in g], sum(counts[i] for i in g), sky)
           for g in groups.values()]
    out.extend(_envelope([reps[i]], counts[i], sky) for i in noise)
    out.sort(key=lambda cl: -cl.peak.snr)
    return out
