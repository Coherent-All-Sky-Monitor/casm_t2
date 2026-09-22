"""The trigger card's ``gulp`` block: every T1 trial t2d clustered in the gulp.

T3's candidate figure draws its bottom row (trials in time and beam, the beams
on the sky) from this block. Schema: ``docs/CARD_SCHEMA.md``. t2d builds it from
its own clustering of the coalesced gulp window, the same clusters and the same
per-cluster decisions it recorded in the DB, so it is a report of data t2d
already had and changes no trigger decision.

Everything here is a pure function of its arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from casm_t2 import timing
from casm_t2.cluster import Cluster
from casm_t2.wire import Candidate

#: Samples per hella gulp.
GULP_SAMP = 8192

#: How far a hella gulp's first sample sits before ``gulp * GULP_SAMP``.
#: hella numbers a gulp ``floor(block_start / 8192) + 1`` where its block starts
#: at ``-(NTIME - gulp) + maxWidth / 2`` (casm_hella.cu), so preamble gulp g
#: carries samples ``g*8192 - 960 .. g*8192 + 7231``. Measured 2026-09-22 over
#: the T2 DB: ``samp - gulp*8192`` spans exactly -960..7231 in every full
#: observation since 2026-09-02. It moves if hella's DM range or widest boxcar
#: changes, so the span is widened to cover the trials whenever they fall outside.
HELLA_GULP_LEAD_SAMP = 960

#: Post-width-drop counts of this many gulps, this one last, in ``storm.raw_recent``.
STORM_RECENT_GULPS = 5

#: Bounds on what one card carries. A gulp can reach ``max_cands_per_gulp``
#: (20000) trials, which is about 1 MB of trial rows alone. Over a cap the
#: triggered cluster's rows are kept first, then the rest by descending S/N,
#: and the block says so in ``trials_truncated`` / ``clusters_truncated``.
MAX_CARD_TRIALS = 10000
MAX_CARD_CLUSTERS = 1000


@dataclass(slots=True)
class GulpReport:
    """What t2d knew about one gulp when it decided, kept for the card.

    ``cands`` are the trials DBSCAN saw (after the width veto and any storm
    shed), ``labels[i]`` the index into ``clusters`` of ``cands[i]``'s cluster,
    ``outcomes[k]`` the (outcome, detail) t2d gave ``clusters[k]``.
    """

    gulp: int | None
    utc_start: str | None
    weights_id: str | None
    n_raw: int
    n_width_dropped: int
    n_shed: int
    cands: list[Candidate]
    labels: list[int]
    clusters: list[Cluster]
    outcomes: list[tuple[str, str]]
    rules: dict
    raw_recent: tuple = ()
    stormy: bool = False
    tsamp_s: float = timing.TSAMP_S

    def sample_span(self) -> tuple[int, int]:
        """(samp_lo, samp_hi) of the gulp, inclusive, hella samples."""
        samps = [c.samp for c in self.cands]
        if self.gulp is None:
            return (min(samps), max(samps)) if samps else (0, 0)
        lo = int(self.gulp) * GULP_SAMP - HELLA_GULP_LEAD_SAMP
        hi = lo + GULP_SAMP - 1
        if samps:
            lo, hi = min(lo, min(samps)), max(hi, max(samps))
        return lo, hi


def _cluster_entry(k: int, cl: Cluster, members: list[Candidate],
                   outcome: tuple[str, str], event_samp: int,
                   tsamp_s: float) -> dict:
    c = cl.peak
    best: dict[int, float] = {}
    for m in members:
        if m.snr > best.get(m.beam, float("-inf")):
            best[m.beam] = float(m.snr)
    return {
        "id": int(k),
        "peak": [round((c.samp - event_samp) * tsamp_s, 4), int(c.beam),
                 round(float(c.dm), 3), round(float(c.snr), 2), int(c.width)],
        "n_trials": len(members),
        "beams": [[int(b), round(s, 2)] for b, s in sorted(best.items())],
        "sky_extent_deg": round(float(cl.sky_extent_deg or 0.0), 3),
        "dm_lo": round(float(cl.dm_lo), 3),
        "dm_hi": round(float(cl.dm_hi), 3),
        "outcome": outcome[0],
        "detail": outcome[1],
    }


def gulp_block(report: GulpReport, triggered: int) -> dict:
    """The card's ``gulp`` block, with ``clusters[triggered]`` the dumped one.

    Every ``dt_s`` is relative to the triggered cluster's peak sample, which is
    the card's ``samp``, in seconds to 4 decimals. The triggered cluster is the
    only one with outcome ``triggered``, whatever the report said about it.
    """
    event_samp = report.clusters[triggered].peak.samp
    tsamp = float(report.tsamp_s)
    members: list[list[Candidate]] = [[] for _ in report.clusters]
    for c, k in zip(report.cands, report.labels):
        members[k].append(c)

    # Trials: all of them up to the cap; over it the triggered cluster's first,
    # then the brightest of the rest.
    idx = list(range(len(report.cands)))
    n_trials_total = len(idx)
    trials_truncated = n_trials_total > MAX_CARD_TRIALS
    if trials_truncated:
        idx.sort(key=lambda i: (report.labels[i] != triggered,
                                -report.cands[i].snr))
        idx = idx[:MAX_CARD_TRIALS]
    idx.sort(key=lambda i: report.cands[i].samp)

    # Clusters: already in descending peak S/N; the triggered one always stays.
    keep = list(range(len(report.clusters)))
    clusters_truncated = len(keep) > MAX_CARD_CLUSTERS
    if clusters_truncated:
        keep = sorted({triggered} | set(keep[:MAX_CARD_CLUSTERS - 1]))

    entries = []
    for k in keep:
        outcome = (report.outcomes[k] if k != triggered else
                   ("triggered", report.outcomes[k][1]))
        if k != triggered and outcome[0] == "triggered":
            outcome = ("also_triggerable", outcome[1])
        entries.append(_cluster_entry(k, report.clusters[k], members[k],
                                      outcome, event_samp, tsamp))

    samp_lo, samp_hi = report.sample_span()
    return {
        "gulp": int(report.gulp) if report.gulp is not None else None,
        "utc_start": report.utc_start,
        "samp_lo": int(samp_lo), "samp_hi": int(samp_hi),
        "tsamp_s": tsamp,
        "weights_id": report.weights_id,
        "n_raw": int(report.n_raw),
        "n_width_dropped": int(report.n_width_dropped),
        "n_shed": int(report.n_shed),
        "n_trials_total": n_trials_total,
        "trials_truncated": trials_truncated,
        "n_clusters_total": len(report.clusters),
        "clusters_truncated": clusters_truncated,
        "trials": [[round((report.cands[i].samp - event_samp) * tsamp, 4),
                    int(report.cands[i].beam), round(float(report.cands[i].dm), 3),
                    round(float(report.cands[i].snr), 2),
                    int(report.cands[i].width), int(report.labels[i])]
                   for i in idx],
        "clusters": entries,
        "rules": report.rules,
        "storm": {"raw_recent": [int(n) for n in report.raw_recent],
                  "stormy": bool(report.stormy)},
    }


def card_json(obj, indent: int = 2) -> str:
    """JSON text of a trigger card, indented, with every list of scalars on one line.

    ``json.dumps(indent=2)`` puts each number on its own line, which makes a
    20000-trial gulp block about 3 MB. Readers use ``json.load`` and see the
    same object either way.
    """
    def enc(o, level: int) -> str:
        pad, inner = " " * (indent * level), " " * (indent * (level + 1))
        if isinstance(o, dict):
            if not o:
                return "{}"
            body = ",\n".join(f"{inner}{json.dumps(str(k))}: {enc(v, level + 1)}"
                              for k, v in o.items())
            return "{\n" + body + "\n" + pad + "}"
        if isinstance(o, (list, tuple)):
            if not any(isinstance(v, (dict, list, tuple)) for v in o):
                return json.dumps(list(o))
            body = ",\n".join(inner + enc(v, level + 1) for v in o)
            return "[\n" + body + "\n" + pad + "]"
        return json.dumps(o)
    return enc(obj, 0) + "\n"
