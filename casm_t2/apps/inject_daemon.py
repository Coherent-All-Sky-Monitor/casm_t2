"""Online injection scheduler with a per-gate ledger.

Injects synthetic FRBs into the live beamformer stream on a fixed cadence
through the casm_beam_inj FIFOs, and records every injection in the T2 database
before it happens so t2d can exclude it from triggering and every miss can be
attributed to a gate.

Per shot:
  1. make_noise_fil_with_frb_snr.py renders one 8192-sample, single-beam
     filterbank holding the pulse alone, no noise, since it adds onto the live
     stream, and estimates the injected S/N from the live beam statistics.
  2. convert_fil_to_dada.py wraps it in a DADA header for the inject beam.
  3. The .dada bytes go to /tmp/beaminj.fifo.<stream>, where casm_beam_inj
     merges them into the next gulps.

Reconciliation runs a few minutes later against the clusters table and fills
gate_t1/gate_t2/gate_trigger plus the recovered snr/dm. Gates: did T1 report it,
did T2 cluster it, would T2's trigger filters have passed it (injections are
tagged and never dump).

corr1 streams (0-3) only; corr2 needs a local runner.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from pathlib import Path

import yaml

from casm_t2 import (beams, cluster, db, dump_client, inject_calib,
                     inject_outcome, inject_plot, inject_replay,
                     inject_slack, logsetup, timing, weights_registry)

logger = logging.getLogger("t2.inject")

MAKE_NOISE = "/home/casm/software/dev/make_noise_fil_with_frb_snr.py"
CONVERT_DIR = "/home/casm/software/meilin/code/casm-hella/scripts"
PYTHON = "/home/casm/software/dev/casm_venvs/casm_offline_env/bin/python"

_SNR_RE = re.compile(r"INJECTED_SNR_ESTIMATE\s+([-+0-9.eE]+)")

#: Samples per gulp. The gulp index is exactly samp // GULP_SAMPS.
GULP_SAMPS = 8192

#: The display-name format, and nothing else: `inj_YYYYMMDD_NNNN`.
_FILE_ID_RE = re.compile(r"inj_(\d{8})_(\d{4})")


def make_injection_files(dm: float, amp: float, sigma_ms: float, local_beam: int,
                         scratch: Path, file_id: str) -> tuple[Path, float | None]:
    """Render the .fil and .dada for one injection; returns (dada, est_snr)."""
    fil = scratch / f"{file_id}.fil"
    dada = scratch / f"{file_id}.dada"
    r1 = subprocess.run(
        [PYTHON, MAKE_NOISE, "--DM", f"{dm:.3f}", "--pulse_amp", f"{amp:.2f}",
         "--pulse_sigma_ms", f"{sigma_ms:.2f}", "--no_noise", "--no_config",
         "--output", str(fil), "--dada_nbeam", str(local_beam),
         "--snr_beam", str(local_beam), "--snr_source", "bf_proc_stat"],
        capture_output=True, text=True, timeout=300, check=True)
    m = _SNR_RE.search(r1.stdout)
    est_snr = float(m.group(1)) if m else None
    subprocess.run(
        [PYTHON, "convert_fil_to_dada.py", "--input", str(fil), "--output", str(dada),
         "--nchan", "3072", "--nbeam", "1", "--ibeam", "0",
         "--gulp_samps", "8192", "--hdr_injbeam", str(local_beam)],
        cwd=CONVERT_DIR, capture_output=True, text=True, timeout=300, check=True)
    return dada, est_snr


def read_live_std(beam: int) -> float:
    """The live per-channel std of one beam, straight from Redis."""
    import sys
    sys.path.insert(0, str(Path(MAKE_NOISE).parent))
    import make_noise_fil_with_frb_snr as mn  # noqa: E402
    _, sigma_n, _, _, _ = mn.query_live_noise_std(beam, "bf_proc_stat",
                                                  force_refresh=True)
    return float(sigma_n)


def amp_for_target_snr(target_snr: float, sigma_ms: float, beam: int,
                       nchan_usable: int = 2880,
                       std: float | None = None) -> tuple[float, float]:
    """Pulse amplitude in stream counts for a target matched-filter S/N.

    Analytical Gaussian matched filter over nchan independent channels
    (make_noise_fil_with_frb_snr.matched_filter_snr, "analytical" branch):
    S/N = (A / sigma_n) * sqrt(nchan) * sqrt(sigma_t * sqrt(pi)), sigma_t in
    samples, sigma_n the live per-channel std of the beam from Redis. The pulse
    renders as integer u8 counts, so the result is rounded and floored at 1.
    Returns (amp_counts, sigma_n).
    """
    import math
    sigma_n = read_live_std(beam) if std is None else float(std)
    sigma_t = max(sigma_ms / 1.048576, 1.0)
    amp = target_snr * sigma_n / (math.sqrt(nchan_usable) * math.sqrt(sigma_t * math.sqrt(math.pi)))
    return float(max(1, round(amp))), float(sigma_n)


# The width/amplitude calibration lives in casm_t2.inject_calib so the Slack
# text uses the same numbers as the solver. Re-exported here, where it applies.
FWHM_PER_SIGMA = inject_calib.FWHM_PER_SIGMA
MIN_RENDERABLE_FWHM_MS = inject_calib.MIN_RENDERABLE_FWHM_MS
sample_fwhm_ms = inject_calib.sample_fwhm_ms
sample_inject_snr = inject_calib.sample_inject_snr
draw = inject_calib.draw
sample_spec = inject_calib.sample_spec
clamp_fwhm_ms = inject_calib.clamp_fwhm_ms
clamp_dm = inject_calib.clamp_dm
rec_per_true = inject_calib.rec_per_true
reported_snr_cap = inject_calib.reported_snr_cap
clamp_inject_snr = inject_calib.clamp_inject_snr


LEDGER_SELECT = ("SELECT i.*, c.name AS rec_name FROM injections i"
                 " LEFT JOIN clusters c ON c.id = i.matched_cluster")


def ledger_row(conn, inj_id: int) -> dict | None:
    """One injections row as a plain dict, for the Slack text builders.

    Joined to the matched cluster's event name, so the Slack link points at that
    event page rather than the injection's own truth plot.
    """
    cur = conn.execute(LEDGER_SELECT + " WHERE i.id = ?", (inj_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row))


#: hella writes one candidate file per observation per stream. Injections go
#: to streams 0-3, which are corr1-local, so reconcile can always read them.
HELLA_CANDS_DIR = "/mnt/nvme4/data/casm/hella_cands"

#: Reconcile window around inject_utc, seconds. The pulse lands before
#: inject_utc: the sidecar joins the next assembled gulp, whose samples are
#: already 5-18 s old. Used for both the cluster match and the T1 trial scan.
#: See casm-wiki injection-saturation.md.
WINDOW_LO_S = -40.0
WINDOW_HI_S = 90.0


def dm_tolerance(dm: float) -> float:
    """DM half-width for a match, for clusters and raw trials alike."""
    return max(0.15 * dm, 5.0)


BFCORR_LOG = "/data/casm/logs/antenna_bfcorr.log"

_BFCORR_START_RE = re.compile(r"START casm_bfcorr\b")
_BFCORR_STAMP_RE = re.compile(r"\[([0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9:.]+)\]")


def _bfcorr_starts(log_path) -> list[tuple[datetime | None, str]]:
    """Every ``START casm_bfcorr`` line as (timestamp, line), file order.

    The log interleaves the antenna nodes, so the last LINE is not the
    latest start - node 1 can log after node 5 has already restarted.
    Callers must sort on the timestamp, not take the tail.
    """
    out: list[tuple[datetime | None, str]] = []
    try:
        with open(log_path, errors="replace") as fh:
            for line in fh:
                if not _BFCORR_START_RE.search(line):
                    continue
                m = _BFCORR_STAMP_RE.search(line)
                stamp = None
                if m:
                    try:
                        stamp = timing.parse_dada_utc(m.group(1))
                    except ValueError:
                        stamp = None
                out.append((stamp, line))
    except OSError as exc:
        logger.debug("bfcorr log unreadable (%s): %s", log_path, exc)
    return out


class StaleStdError(RuntimeError):
    """The live beam std could not be trusted in time; skip the shot."""

    def __init__(self, age_s: float):
        self.age_s = float(age_s)
        super().__init__(f"live std stale (age {self.age_s:.0f} s)")


def wait_for_fresh_std(beam: int, icfg: dict, reader=None, now=None,
                       sleep=None, log_path=BFCORR_LOG) -> tuple[float, float]:
    """The live std of `beam`, once it can be trusted. Returns (std, age_s).

    A beamformer restart leaves the last published value in Redis, where it can
    sit unchanged for minutes. Redis carries no timestamp for these keys, and
    the `age_s` query_live_noise_std returns is the local cache age, always 0.0
    on the force_refresh path. Freshness is decided two ways:

      * no restart within `max_std_age_s`: trusted at once, one Redis read;
      * a recent restart: poll every `std_poll_s` until the value changes, which
        is proof the publisher is back, giving up after `std_wait_s`.

    Raises StaleStdError when the wait runs out.
    """
    import time as _time
    now = now or (lambda: datetime.now(timezone.utc))
    sleep = sleep or _time.sleep
    reader = reader or (lambda: read_live_std(beam))
    max_age = float(icfg.get("max_std_age_s", 30.0))
    wait_s = float(icfg.get("std_wait_s", 120.0))
    poll_s = float(icfg.get("std_poll_s", 5.0))

    first = reader()
    started = last_bfcorr_start(log_path)
    if started is None:
        return float(first), 0.0
    age = (now() - started).total_seconds()
    if age > max_age:
        return float(first), float(age)

    logger.warning("beam %d: beamformer restarted %.0f s ago (max_std_age_s "
                   "%.0f); waiting for the live std to be republished",
                   beam, age, max_age)
    waited = 0.0
    while waited < wait_s:
        sleep(poll_s)
        waited += poll_s
        value = reader()
        if value != first:
            logger.info("beam %d: live std republished after %.0f s "
                        "(%.2f -> %.2f)", beam, waited, first, value)
            return float(value), float(age + waited)
    raise StaleStdError(age + waited)


def current_sub_incoh(log_path=BFCORR_LOG) -> int | None:
    """Incoherent-beam subtraction state now: 1, 0, or None when unknown.

    Read from the most recent ``START casm_bfcorr`` line in the beamformer log,
    the flag being a command-line argument. None when the log cannot be read or
    holds no start line; the S/N calibration differs between the two states, so
    a wrong label is worse than none.
    """
    starts = _bfcorr_starts(log_path)
    if not starts:
        return None
    dated = [(t, ln) for t, ln in starts if t is not None]
    line = max(dated, key=lambda tl: tl[0])[1] if dated else starts[-1][1]
    return int("--sub_incoh" in line)


def last_bfcorr_start(log_path=BFCORR_LOG) -> datetime | None:
    """UTC of the most recent beamformer start, or None.

    A restart leaves a stale per-beam std in Redis, so this is the clock the
    staleness guard runs on.
    """
    stamps = [t for t, _ in _bfcorr_starts(log_path) if t is not None]
    return max(stamps) if stamps else None


def injection_neighbours(cfg: dict, utc: datetime, beam: int) -> tuple[set[int], bool]:
    """Beams counting as the injected beam, and whether that is a sky answer.

    Returns (beams, from_sky). from_sky False means no pointing table was
    available and the caller falls back to an index window, which is not a sky
    test and is logged.
    """
    try:
        reg = (weights_registry.Registry(cfg["weights_registry"])
               if cfg.get("weights_registry") else weights_registry.Registry())
        pointings = reg.pointings_for(utc)
        sky = cluster.SkyTable.from_pointings(pointings)
    except Exception as exc:  # noqa: BLE001 - never break reconcile on this
        logger.debug("pointing table unavailable for neighbours: %s", exc)
        sky, pointings = None, None
    if sky is None:
        return {int(beam)}, False
    cc = cfg.get("clustering", {}) or {}
    fx = (pointings or {}).get("beam_fwhm_x_deg") or cc.get("beam_fwhm_x_deg", 18.1)
    fy = (pointings or {}).get("beam_fwhm_y_deg") or cc.get("beam_fwhm_y_deg", 3.9)
    scale = float(cc.get("injection_match_scale", 1.0))
    return cluster.neighbour_beams(sky, int(beam), float(fx), float(fy),
                                   scale), True


def next_file_id(conn, when: datetime | None = None) -> str:
    """The shot's display name: ``inj_YYYYMMDD_NNNN``, NNNN per UTC day.

    Counts from 0001 each UTC day, continuing from the highest number already
    recorded, so a restart does not reuse a name. The ledger's integer `id`
    stays the primary key; this is the handle a person reads.
    """
    when = when or datetime.now(timezone.utc)
    day = f"{when:%Y%m%d}"
    prefix = f"inj_{day}_"
    # Strictly this format. A LIKE prefix also matches the old
    # `inj_YYYYMMDD_HHMMSS_bNNN` names, whose time field parses as a number.
    rows = conn.execute(
        "SELECT file_id FROM injections WHERE file_id LIKE ?",
        (prefix + "%",)).fetchall()
    n = 0
    for (value,) in rows:
        m = _FILE_ID_RE.fullmatch(str(value or ""))
        if m and m.group(1) == day:
            n = max(n, int(m.group(2)))
    return f"{prefix}{n + 1:04d}"


def obs_utc_start_at(conn, utc: datetime) -> str | None:
    """UTC_START of the observation live at `utc`, as a PSRDADA string.

    Taken from the most recent cluster at or before that time; clusters carry
    their obs and one is written every few seconds in a normal sky. None when
    the database has no cluster that old. An observation that restarted and has
    not yet produced a cluster still reads as the previous one, which the caller
    sees as a candidate file missing the injection window.
    """
    row = conn.execute(
        "SELECT obs_utc_start FROM clusters WHERE event_utc <= ?"
        " ORDER BY event_utc DESC LIMIT 1",
        (utc.isoformat(timespec="milliseconds"),)).fetchone()
    return row[0] if row else None


def cands_path(obs_utc_start: str, stream: int, cands_dir: str | Path = HELLA_CANDS_DIR) -> Path:
    """Path of hella's raw candidate file for one observation and stream."""
    return Path(cands_dir) / f"cands_{obs_utc_start}.dat.{stream}"


class T1Match(NamedTuple):
    """What hella's raw candidate file holds for one injection."""

    n: int
    best_snr: float | None
    gulp: int | None            # samp // GULP_SAMPS of the best trial
    all_veto_width: bool        # every match was a width t2d drops at parse


def count_t1_trials(path, beams, dm: float, samp_lo: float, samp_hi: float,
                    veto_widths=(6,)) -> T1Match | None:
    """Raw T1 trials matching an injection: (count, best S/N).

    Returns None when the file does not exist, meaning the question could not be
    asked, which is not the same as hella seeing nothing. `beams` is the set
    counting as the injected beam, its sky neighbours from `neighbour_beams`.
    The file is hella's own output, a header line then
    ``snr samp time_days width dm_idx dm beam`` with beam global and samp
    absolute from UTC_START. Streamed, a long observation running to hundreds
    of MB.
    """
    path = Path(path)
    if not path.is_file():
        return None
    tol = dm_tolerance(dm)
    dm_lo, dm_hi = dm - tol, dm + tol
    beams = {int(b) for b in beams}
    veto = {int(w) for w in (veto_widths or ())}
    n, best, best_samp = 0, None, None
    widths: set[int] = set()
    with path.open() as fh:
        for line in fh:
            fields = line.split()
            if len(fields) != 7:
                continue
            try:
                snr = float(fields[0])
                samp = float(fields[1])
                width = int(fields[3])
                cdm = float(fields[5])
                cbeam = int(fields[6])
            except ValueError:
                continue          # the header line, or a torn write
            if not (samp_lo <= samp <= samp_hi):
                continue
            if cbeam not in beams:
                continue
            if not (dm_lo <= cdm <= dm_hi):
                continue
            n += 1
            widths.add(width)
            if best is None or snr > best:
                best, best_samp = snr, samp
    gulp = int(best_samp // GULP_SAMPS) if best_samp is not None else None
    return T1Match(n, best, gulp, bool(widths) and widths <= veto)


def scan_t1_trials(conn, cfg: dict, t0: datetime, stream: int, beams,
                   dm: float):
    """Look for raw T1 trials behind a shot that produced no cluster.

    Returns (n_trials, best_snr, note, match). `n_trials` is None when the
    question could not be asked, no observation known or no file, and `note`
    says which for the ledger. `match` is the full T1Match when a file was read.
    """
    obs = obs_utc_start_at(conn, t0)
    if obs is None:
        return (None, None,
                "T1 file unavailable (no observation known at that time)", None)
    try:
        utc_start = timing.parse_dada_utc(obs)
    except ValueError:
        return (None, None,
                f"T1 file unavailable (unparseable obs_utc_start {obs!r})", None)
    path = cands_path(obs, stream, cfg.get("injection", {}).get(
        "hella_cands_dir", HELLA_CANDS_DIR))
    samp_lo = (t0 - utc_start).total_seconds() + WINDOW_LO_S
    samp_hi = (t0 - utc_start).total_seconds() + WINDOW_HI_S
    got = count_t1_trials(path, beams, dm,
                          samp_lo / timing.TSAMP_S, samp_hi / timing.TSAMP_S,
                          veto_widths=cfg.get("veto_widths", [6]))
    if got is None:
        return None, None, f"T1 file unavailable ({path.name} not found)", None
    return got.n, got.best_snr, None, got



def t2_miss_reason(conn, obs_utc_start: str | None, gulp: int | None,
                   match=None, log=logger) -> str:
    """Why a gulp with matching T1 trials produced no cluster.

    T2 clusters every surviving trial, DBSCAN noise points becoming singletons,
    and stores everything at S/N >= 12, so trials arriving with nothing
    clustered means the gulp or the trials were dropped before clustering.
    `gulp_stats` records which, one row per coalesced gulp. The final branch
    fires only if that assumption is wrong, and logs so.
    """
    where = f"gulp {gulp}" if gulp is not None else "the gulp"
    if obs_utc_start is None or gulp is None:
        log.warning("t2 miss with no gulp to look up (obs=%r gulp=%r)",
                    obs_utc_start, gulp)
        return f"lost at T2: {where} could not be identified"
    row = conn.execute(
        "SELECT n_jobs, n_cands, n_clusters, n_stored, n_vetoed, n_shed, skipped"
        " FROM gulp_stats WHERE obs_utc_start = ? AND gulp = ?",
        (obs_utc_start, gulp)).fetchone()
    if row is None:
        log.warning("t2 miss: no gulp_stats row for %s gulp %s - t2d never "
                    "processed that gulp", obs_utc_start, gulp)
        return (f"lost at T2: {where} never reached t2d (no gulp_stats row)")
    n_jobs, n_cands, n_clusters, n_stored, n_vetoed, n_shed, skipped = row
    if skipped:
        return f"lost at T2: {where} skipped incomplete ({n_jobs}/8 jobs)"
    if n_shed and n_cands and n_shed >= n_cands:
        return (f"lost at T2: {where} dropped by the storm cap "
                f"({n_cands} trials > max)")
    if n_shed:
        return f"lost at T2: {where} shed {n_shed} of {n_cands} trials"
    if match is not None and getattr(match, "all_veto_width", False):
        return f"lost at T2: {where} width-vetoed"
    log.warning("t2 miss on an intact gulp: %s gulp %s had %d cands, "
                "%d clusters, %d stored, %d vetoed - investigate",
                obs_utc_start, gulp, n_cands, n_clusters, n_stored, n_vetoed)
    return f"lost at T2: {where} intact but no cluster (unexpected, investigate)"


def beam_offset_arcsec(cfg: dict, utc, inj_beam: int, rec_beam: int):
    """Sky separation between the injected and recovered beams, or None.

    Uses the pointing table live at the injection time, so it answers "how
    far from where we put it did it come back", not "how far apart are those
    beams today".
    """
    if rec_beam is None or inj_beam is None:
        return None
    if int(rec_beam) == int(inj_beam):
        return 0.0
    try:
        reg = weights_registry.Registry(cfg.get("weights_registry")) \
            if cfg.get("weights_registry") else weights_registry.Registry()
        return weights_registry.beam_separation_arcsec(
            reg.pointings_for(utc), int(inj_beam), int(rec_beam))
    except Exception as exc:  # noqa: BLE001 - a message must never break reconcile
        logger.debug("beam offset unavailable: %s", exc)
        return None


def reconcile(conn, inj_id: int, cfg: dict) -> None:
    """Decide whether the search saw this injection, and record the evidence.

    Recovered means a matching cluster at any S/N. The gates stay recorded, a
    dump being trigger policy rather than detection. With no cluster, hella's
    raw candidate file separates "no cluster formed" from "hella never saw it".
    """
    row = conn.execute(
        "SELECT inject_utc, stream, beam, dm, fail_reason FROM injections"
        " WHERE id = ?", (inj_id,)).fetchone()
    if row is None:
        return
    inj_utc, stream, beam, dm, prior_fail = row
    t0 = datetime.fromisoformat(inj_utc)
    lo = (t0 + timedelta(seconds=WINDOW_LO_S)).isoformat(timespec="milliseconds")
    hi = (t0 + timedelta(seconds=WINDOW_HI_S)).isoformat(timespec="milliseconds")
    dm_tol = dm_tolerance(dm)
    # The injected beam means its neighbours on the sky. Beam indices are not
    # sky-ordered, so an index window is not a sky test.
    beams, from_sky = injection_neighbours(cfg, t0, beam)
    if not from_sky:
        beams = set(range(beam - 2, beam + 3))
        logger.warning("injection %d: no pointing table at %s, falling back to "
                       "the beam-INDEX window %d+-2, which is not a sky match",
                       inj_id, inj_utc, beam)
    # Prefer the fast-triggered cluster, which carries the dump, the plot and
    # the trigger audit.
    marks = ",".join("?" for _ in beams)
    cand = conn.execute(
        "SELECT id, snr, dm, tier, tags, n_beams, beam, width, samp, event_utc"
        " FROM clusters"
        f" WHERE event_utc BETWEEN ? AND ? AND beam IN ({marks})"
        " AND dm_lo <= ? AND dm_hi >= ?"
        " ORDER BY (tags LIKE '%fast_triggered%') DESC, snr DESC LIMIT 1",
        (lo, hi, *sorted(beams), dm + dm_tol, dm - dm_tol)).fetchone()

    filt = cfg.get("filters", {})
    tiers = cfg.get("tiers", {})
    n_trials = None
    if cand is None:
        # No cluster. Ask hella's candidate file whether the trials were there,
        # the only way to tell a clustering loss from a detection loss, T2
        # storing clusters and never raw trials.
        n_trials, best_snr, note, match = scan_t1_trials(conn, cfg, t0, stream,
                                                         beams, dm)
        if n_trials is None:
            reason = (f"lost at T1: no cluster in the window in beam {beam} "
                      f"or its {len(beams) - 1} sky neighbours at DM "
                      f"{dm:.0f} (+-{dm_tol:.0f}) ({note})")
        elif n_trials > 0:
            # Trials were there, so the loss is upstream of clustering.
            # gulp_stats names the gulp-level drop.
            reason = t2_miss_reason(conn, obs_utc_start_at(conn, t0),
                                    match.gulp if match else None, match)
        else:
            reason = (f"lost at T1: no matching trial in beam {beam} or its "
                      f"{len(beams) - 1} sky neighbours within the window "
                      f"at DM {dm:.0f} (+-{dm_tol:.0f})")
        gates = dict(gate_t1=0, gate_t2=0, gate_trigger=0, fail_reason=reason)
        rec = (None, None, None)
        detail = (None, None, None, None, None)
    else:
        cid, snr, rdm, tier, tags, n_beams, cbeam, cwidth, csamp, cutc = cand
        # Recorded for information; it does not decide the outcome.
        would = (tier in ("A", "B") and rdm >= filt.get("dm_floor", 20.0)
                 and n_beams <= filt.get("max_nbeam", 32)
                 and cbeam not in set(filt.get("beam_veto", [])))
        gates = dict(gate_t1=1, gate_t2=1, gate_trigger=int(would),
                     fail_reason=None)
        rec = (cid, snr, rdm)
        # Negative lead is normal: the sidecar joins a gulp whose samples are
        # already seconds old, so the pulse arrives before the FIFO write.
        try:
            lead = (datetime.fromisoformat(cutc) - t0).total_seconds()
        except (TypeError, ValueError):
            lead = None
        offset = beam_offset_arcsec(cfg, t0, beam, cbeam)
        detail = (cwidth, cbeam, csamp, lead, offset)
    outcome = inject_outcome.classify(gates["gate_t1"], gates["gate_t2"],
                                      gates["gate_trigger"], prior_fail,
                                      n_trials)
    with conn:
        conn.execute(
            "UPDATE injections SET gate_t1=?, gate_t2=?, gate_trigger=?,"
            " fail_reason=?, matched_cluster=?, rec_snr=?, rec_dm=?,"
            " rec_width=?, rec_beam=?, rec_samp=?, rec_lead_s=?,"
            " rec_offset_arcsec=?, n_t1_trials=?, outcome=? WHERE id=?",
            (gates["gate_t1"], gates["gate_t2"], gates["gate_trigger"],
             gates["fail_reason"], *rec, *detail, n_trials, outcome, inj_id))
    logger.info("reconciled injection %d: %s (t1=%s t2=%s trigger=%s%s)",
                inj_id, outcome, gates["gate_t1"], gates["gate_t2"],
                gates["gate_trigger"],
                "" if n_trials is None else f" t1_trials={n_trials}")


def _parse_utc(value):
    """An ISO timestamp from the ledger, or None."""
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def do_replay(conn, cfg: dict, poster, inj_id: int, dump_dir) -> Path | None:
    """Render the shot's replay plot and thread it under its Slack message.

    Runs after reconcile, so the outcome is known and a miss can be rendered at
    the time the pulse should have arrived. Returns the PNG path, or None when
    nothing was due or anything failed.
    """
    icfg = cfg.get("injection", {}) or {}
    rcfg = inject_replay.replay_cfg(icfg)
    row = ledger_row(conn, inj_id)
    outcome = (row or {}).get("outcome")
    # The dump directory is shared with T2's triggered dumps, so cleanup needs
    # this shot's own window to tell which files are its.
    d_start = _parse_utc((row or {}).get("dump_utc_start"))
    d_stop = _parse_utc((row or {}).get("dump_utc_stop"))
    completes = poster.mode in (inject_slack.MODE_SINGLE,
                                inject_slack.MODE_SENT_THEN_UPDATE)
    due = (dump_dir is not None and rcfg.get("post") != "never"
           and inject_replay.post_due(rcfg, outcome,
                                      inject_replay.last_replay_day(conn)))
    if not due:
        if dump_dir is not None:
            logger.info("injection %d: replay not due (post=%s, outcome=%s)",
                        inj_id, rcfg.get("post"), outcome)
            inject_replay.cleanup_dump(dump_dir, rcfg, d_start, d_stop,
                                       outcome, (row or {}).get('file_id'))
        if completes:
            # These modes owe the shot a finished card either way; without a
            # plot that is the outcome bar alone.
            ts = poster.post_injection(row, None)
            if ts:
                # Never clear a ts already held: the fire-time message exists
                # even when completing it failed.
                with conn:
                    conn.execute("UPDATE injections SET slack_ts=? WHERE id=?",
                                 (ts, inj_id))
        return None

    if (outcome in inject_outcome.MISSES
            and rcfg.get("on_miss", "none") == "none"):
        # A miss gets the red bar and the reason, no rendered pulse. The raw
        # dump is kept instead (keep_dump_on_miss).
        logger.info("injection %d missed (%s): no replay rendered "
                    "(replay.on_miss=none)", inj_id, outcome)
        if completes:
            ts = poster.post_injection(row, None)
            if ts:
                with conn:
                    conn.execute("UPDATE injections SET slack_ts=? WHERE id=?",
                                 (ts, inj_id))
        inject_replay.cleanup_dump(dump_dir, rcfg, d_start, d_stop, outcome,
                                   (row or {}).get("file_id"))
        return None

    event_utc = None
    if outcome in inject_outcome.MISSES:
        # Nothing was found, so the tool is told where to put the pulse; the
        # image is the pulse version either way.
        event_utc = inject_replay.expected_event_utc(
            conn, datetime.fromisoformat(row["inject_utc"]))
    png = inject_replay.run_replay(inj_id, dump_dir, rcfg,
                                   db_path=cfg.get("db", db.DEFAULT_PATH),
                                   event_utc=event_utc,
                                   label=(row or {}).get("file_id"),
                                   card_only=outcome in inject_outcome.MISSES)
    posted = False
    if png is not None:
        with conn:
            conn.execute("UPDATE injections SET replay_png=? WHERE id=?",
                         (str(png), inj_id))
        row = ledger_row(conn, inj_id)
    if completes:
        # The shot's card: completed in place in sent_then_update, posted fresh
        # in single.
        ts = poster.post_injection(row, png)
        posted = ts is not None
        with conn:
            if ts:
                conn.execute("UPDATE injections SET slack_ts=? WHERE id=?",
                             (ts, inj_id))
            conn.execute("UPDATE injections SET replay_posted=? WHERE id=?",
                         (int(posted), inj_id))
    elif png is not None:
        posted = bool(poster.post_replay(
            row, png, inject_slack.display_id_md(row)))
        with conn:
            conn.execute("UPDATE injections SET replay_posted=? WHERE id=?",
                         (int(posted), inj_id))
    inject_replay.cleanup_dump(dump_dir, rcfg, d_start, d_stop, outcome,
                               (row or {}).get('file_id'))
    return png


async def run(cfg: dict, once: bool, force: dict | None = None) -> None:  # noqa: C901
    force = force or {}
    icfg = cfg.get("injection", {})
    scratch = Path(icfg.get("scratch_dir", "/mnt/nvme5/casm_pipeline/injections"))
    scratch.mkdir(parents=True, exist_ok=True)
    streams = icfg.get("streams", [0, 1, 2, 3])
    cadence_s = icfg.get("cadence_min", 30) * 60.0
    conn = db.connect(cfg.get("db", db.DEFAULT_PATH))
    # With injection.slack.enabled false every poster call is a no-op.
    poster = inject_slack.poster_from_cfg(icfg)
    rcfg = inject_replay.replay_cfg(icfg)
    i = 0
    first = True
    while True:
        # Hold the cadence on startup too, so a restart does not fire an
        # immediate injection and dump.
        if first and not once:
            first = False
            last = conn.execute("SELECT max(inject_utc) FROM injections").fetchone()[0]
            if last:
                from datetime import datetime as _dt
                elapsed = (_dt.now(timezone.utc) - _dt.fromisoformat(last)).total_seconds()
                wait = max(cadence_s - elapsed, 0)
                if wait:
                    logger.info("startup: %.0f s until next scheduled injection", wait)
                    await asyncio.sleep(wait)
        stream = streams[i % len(streams)]
        local_beam = random.randint(4, 59)
        if force.get("beam") is not None:
            local_beam = int(force["beam"]) % 64
            stream = int(force["beam"]) // 64
        beam = stream * 64 + local_beam
        # Shot parameters come from the `sample:` block; a CLI flag overrides.
        dm = clamp_dm(force.get("dm")
                      or sample_spec(icfg, "dm", random, "dm_range"))
        # Widths are FWHM everywhere except the generator call and the sigma_ms
        # ledger column, both of which want the Gaussian sigma.
        fwhm_ms = clamp_fwhm_ms(
            force.get("fwhm_ms")
            or sample_spec(icfg, "fwhm_ms", random, "fwhm_ms_range"))
        sigma_ms = fwhm_ms / FWHM_PER_SIGMA
        solve = (force.get("inject_snr") is not None
                 or force.get("target_snr") is not None
                 or (icfg.get("sample") or {}).get("inject_snr") is not None
                 or "inject_snr_range" in icfg or "target_rec_snr_range" in icfg)
        if solve:
            # The sampled quantity is the injected (true, analytic) S/N, which
            # is what the amplitude solver needs, independent of how hella
            # reports it. The fixed count range assumed the pre-Route-Z u8 beam
            # scale; counts are now fp16 units of a stream with std ~36-46.
            # See casm-wiki injection-saturation.md.
            if force.get("inject_snr") is not None:
                inject_snr = float(force["inject_snr"])
            elif force.get("target_snr") is not None:
                # Manual shot given as a reported S/N: undo the table.
                inject_snr = float(force["target_snr"]) / rec_per_true(fwhm_ms, icfg)
            elif "target_rec_snr_range" in icfg and not (
                    (icfg.get("sample") or {}).get("inject_snr")):
                # Legacy config expressed as a reported-S/N range.
                inject_snr = random.uniform(
                    *icfg["target_rec_snr_range"]) / rec_per_true(fwhm_ms, icfg)
            else:
                inject_snr = sample_spec(icfg, "inject_snr", random,
                                         "inject_snr_range")
            # Only the reported S/N saturates hella: predict it from the
            # per-width table and scale down if over this width's ceiling.
            inject_snr, target_rec, clamped = clamp_inject_snr(
                inject_snr, fwhm_ms, icfg)
            nchan_usable = int(icfg.get("nchan_usable", 2880))
            try:
                std, std_age = await asyncio.to_thread(
                    wait_for_fresh_std, beam, icfg)
            except StaleStdError as exc:
                # Do not fire on an untrusted std: the amplitude would be off
                # by the drift and pollute the calibration.
                logger.error("injection skipped: %s", exc)
                now = datetime.now(timezone.utc)
                with conn:
                    conn.execute(
                        "INSERT INTO injections (inject_utc, stream, beam, dm,"
                        " amp, sigma_ms, file_id, created_utc, fail_reason,"
                        " outcome, sub_incoh) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (now.isoformat(timespec="milliseconds"), stream, beam,
                         dm, 0.0, sigma_ms, "", 
                         now.isoformat(timespec="milliseconds"),
                         f"live std stale (age {exc.age_s:.0f} s)",
                         inject_outcome.FIRE_FAILED, current_sub_incoh()))
                if once:
                    return
                i += 1
                await asyncio.sleep(cadence_s)
                continue
            amp, sigma_n = amp_for_target_snr(
                inject_snr, sigma_ms, beam, nchan_usable, std=std)
            logger.info("injected S/N %.1f at FWHM %.1f ms (predicted reported "
                        "%.1f at rec_per_true %.2f, cap %.1f%s), live std %.2f "
                        "-> amp %.0f counts",
                        inject_snr, fwhm_ms, target_rec,
                        rec_per_true(fwhm_ms, icfg),
                        reported_snr_cap(fwhm_ms, icfg),
                        ", CLAMPED" if clamped else "", sigma_n, amp)
            if std_age:
                logger.info("live std age %.0f s (max_std_age_s %.0f)",
                            std_age, float(icfg.get("max_std_age_s", 30.0)))
        else:
            amp = random.uniform(*icfg.get("amp_range", [25.0, 45.0]))
            inject_snr = target_rec = sigma_n = nchan_usable = None
        file_id = next_file_id(conn)

        try:
            dada, est_snr = await asyncio.to_thread(
                make_injection_files, dm, amp, sigma_ms, local_beam, scratch, file_id)
        except subprocess.SubprocessError as exc:
            logger.error("injection file generation failed: %s", exc)
            if once:
                return
            await asyncio.sleep(cadence_s)
            continue

        now = datetime.now(timezone.utc)
        with conn:
            cur = conn.execute(
                "INSERT INTO injections (inject_utc, stream, beam, dm, amp, sigma_ms,"
                " est_snr, file_id, created_utc, target_snr, inject_snr, sigma_n,"
                " nchan_usable, sub_incoh) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(timespec="milliseconds"), stream, beam, dm, amp,
                 sigma_ms, est_snr, file_id, now.isoformat(timespec="milliseconds"),
                 target_rec, inject_snr, sigma_n, nchan_usable,
                 current_sub_incoh()))
            inj_id = cur.lastrowid
        fifo = f"/tmp/beaminj.fifo.{stream}"
        try:
            data = dada.read_bytes()
            await asyncio.to_thread(Path(fifo).write_bytes, data)
        except OSError as exc:
            logger.error("FIFO write to %s failed: %s", fifo, exc)
            with conn:
                conn.execute(
                    "UPDATE injections SET fail_reason=?, outcome=? WHERE id=?",
                    (f"fifo_write_failed:{exc}", inject_outcome.FIRE_FAILED,
                     inj_id))
            if once:
                return
            await asyncio.sleep(cadence_s)
            continue
        logger.info("injection %d: beam %d (stream %d) dm=%.1f amp=%.1f "
                    "fwhm=%.1fms est_snr=%s", inj_id, beam, stream, dm, amp,
                    fwhm_ms, f"{est_snr:.1f}" if est_snr else "?")

        # Dump the injected stream around the shot for the replay plot. The
        # window sits before inject_utc, where the pulse is. Requested now
        # because it cannot be taken retrospectively; whether the plot is posted
        # is decided once the outcome is known.
        dump_dir = None
        if inject_replay.dump_due(rcfg):
            d_start, d_stop = inject_replay.dump_window(now, rcfg)
            try:
                loc = beams.stream_location(stream)
                reply = await asyncio.to_thread(
                    dump_client.request_dump, loc.host, loc.control_port,
                    d_start, d_stop, float(rcfg.get("dump_timeout_s", 60.0)))
                dump_dir = loc.dump_dir
                with conn:
                    conn.execute(
                        "UPDATE injections SET dump_dir=?, dump_utc_start=?,"
                        " dump_utc_stop=? WHERE id=?",
                        (dump_dir,
                         d_start.isoformat(timespec="milliseconds"),
                         d_stop.isoformat(timespec="milliseconds"), inj_id))
                logger.info("injection %d: dump [%s .. %s] on %s -> %r",
                            inj_id, d_start, d_stop, loc.host, reply)
            except Exception:
                logger.exception("injection %d: dump request failed", inj_id)
                dump_dir = None

        try:
            ts = poster.post_sent(ledger_row(conn, inj_id))
            if ts:
                with conn:
                    conn.execute("UPDATE injections SET slack_ts=? WHERE id=?",
                                 (ts, inj_id))
        except Exception:
            logger.exception("slack sent-post for injection %d failed", inj_id)

        # Truth plot for the web gallery, rendered from the injected .fil:
        # dumps tap upstream of the injection merge and cannot show it.
        try:
            png_dir = Path(icfg.get("plot_dir",
                                    "/mnt/nvme5/casm_pipeline/candidates/injections"))
            await asyncio.to_thread(
                inject_plot.render, scratch / f"{file_id}.fil", dm, est_snr,
                png_dir / f"{file_id}.png",
                f"injection {inj_id}   beam {beam}   DM={dm:.1f}   "
                f"FWHM={fwhm_ms:.1f} ms   est S/N={est_snr or float('nan'):.0f}")
        except Exception:
            logger.exception("truth plot for injection %d failed", inj_id)

        # T1 reports 20-30 s late and the sidecar joins a gulp already seconds
        # old, so the default 90 s clears both.
        await asyncio.sleep(float(icfg.get("reconcile_wait_s", 90)))
        try:
            reconcile(conn, inj_id, cfg)
        except Exception:
            logger.exception("reconcile of injection %d failed", inj_id)
        else:
            try:
                poster.post_outcome(ledger_row(conn, inj_id))
                inject_slack.check_streak(conn, poster, inj_id)
            except Exception:
                logger.exception("slack outcome-post for injection %d failed",
                                 inj_id)
            try:
                do_replay(conn, cfg, poster, inj_id, dump_dir)
            except Exception:
                logger.exception("replay of injection %d failed", inj_id)

        # Rolling scratch cleanup: keep the last ~20 injections of work files.
        work = sorted(scratch.glob("inj_*"), key=lambda p: p.stat().st_mtime)
        for f in work[:-40]:
            f.unlink(missing_ok=True)

        if once:
            return
        i += 1
        await asyncio.sleep(max(cadence_s - 180, 10))


def main() -> None:
    p = argparse.ArgumentParser(description="Scheduled live injections with gate ledger")
    p.add_argument("config", nargs="?",
                   default="/home/casm/software/dev/casm_t2/config/t2d.yaml")
    p.add_argument("--once", action="store_true", help="single injection, then exit")
    p.add_argument("--log-file", default="/mnt/nvme5/casm_pipeline/logs/t2_inject.log")
    p.add_argument("--beam", type=int, help="force the global beam (0-255) for a test shot")
    p.add_argument("--dm", type=float, help="force the DM for a test shot")
    p.add_argument("--fwhm-ms", type=float,
                   help="force the pulse FWHM in ms for a test shot "
                        f"(floor {MIN_RENDERABLE_FWHM_MS:.2f} ms, set by the generator)")
    p.add_argument("--sigma-ms", type=float,
                   help="deprecated spelling of --fwhm-ms, in Gaussian sigma "
                        "(FWHM = 2.355 sigma)")
    p.add_argument("--inject-snr", type=float,
                   help="force the injected (true, analytic) S/N; the amplitude "
                        "is solved from it and the live beam std")
    p.add_argument("--target-snr", type=float,
                   help="force the hella-REPORTED S/N instead; converted to an "
                        "injected S/N through the per-width rec_per_true table")
    args = p.parse_args()
    logsetup.setup(args.log_file)
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    fwhm_ms = args.fwhm_ms
    if fwhm_ms is None and args.sigma_ms is not None:
        fwhm_ms = args.sigma_ms * FWHM_PER_SIGMA
        logger.warning("--sigma-ms %.2f is deprecated; using FWHM %.2f ms",
                       args.sigma_ms, fwhm_ms)
    force = {"beam": args.beam, "dm": args.dm, "fwhm_ms": fwhm_ms,
             "inject_snr": args.inject_snr, "target_snr": args.target_snr}
    try:
        asyncio.run(run(cfg, args.once, force))
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
