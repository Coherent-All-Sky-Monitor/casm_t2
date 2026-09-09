"""Online injection scheduler with a per-gate ledger.

Injects synthetic FRBs into the live beamformer stream on a fixed cadence
(via the existing casm_beam_inj FIFOs — no Fourier Space code involved) and
records every injection in the T2 database BEFORE it happens, so t2d can
exclude it from triggering and the daily report can attribute every miss to
a pipeline gate.

Recipe (productionized from meilin's mei_realtime_inj.ipynb):
  1. make_noise_fil_with_frb_snr.py renders one 8192-sample, single-beam
     filterbank with the pulse (no noise — it adds onto the live stream)
     and estimates the injected S/N from the live beam noise statistics.
  2. convert_fil_to_dada.py wraps it in a DADA header for the inject beam.
  3. The .dada bytes are written to /tmp/beaminj.fifo.<stream>, where the
     casm_beam_inj daemon merges them into the next gulps.

A few minutes after each injection the daemon reconciles it against the
clusters table and fills the gate columns (gate_t1/gate_t2/gate_trigger)
plus the recovered snr/dm, so /injections in the web UI and the daily
report are near-real-time. Gates: did T1 report it -> did T2 cluster it ->
would T2's trigger filters have passed it (injections are tagged and never
actually dump).

corr1 streams (0-3) only for now; corr2 needs a local runner.
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
from pathlib import Path

import yaml

from casm_t2 import (db, inject_calib, inject_outcome, inject_plot,
                     inject_slack, logsetup)

logger = logging.getLogger("t2.inject")

MAKE_NOISE = "/home/casm/software/dev/make_noise_fil_with_frb_snr.py"
CONVERT_DIR = "/home/casm/software/meilin/code/casm-hella/scripts"
PYTHON = "/home/casm/software/dev/casm_venvs/casm_offline_env/bin/python"

_SNR_RE = re.compile(r"INJECTED_SNR_ESTIMATE\s+([-+0-9.eE]+)")


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


def amp_for_target_snr(target_snr: float, sigma_ms: float, beam: int,
                       nchan_usable: int = 2880) -> tuple[float, float]:
    """Pulse amplitude in stream counts for a target matched-filter S/N.

    Analytical Gaussian matched filter over nchan independent channels
    (make_noise_fil_with_frb_snr.matched_filter_snr, "analytical" branch):
    S/N = (A / sigma_n) * sqrt(nchan) * sqrt(sigma_t * sqrt(pi)), sigma_t in
    samples. sigma_n is the live per-channel std of this beam from Redis
    (bf_proc_stat). The pulse is rendered as integer u8 counts, so the result
    is rounded and floored at 1 count. Returns (amp_counts, sigma_n).
    """
    import math
    import sys
    sys.path.insert(0, str(Path(MAKE_NOISE).parent))
    import make_noise_fil_with_frb_snr as mn  # noqa: E402
    _, sigma_n, _, _, _ = mn.query_live_noise_std(beam, "bf_proc_stat",
                                                   force_refresh=True)
    sigma_t = max(sigma_ms / 1.048576, 1.0)
    amp = target_snr * sigma_n / (math.sqrt(nchan_usable) * math.sqrt(sigma_t * math.sqrt(math.pi)))
    return float(max(1, round(amp))), float(sigma_n)


# The width/amplitude calibration lives in casm_t2.inject_calib so the Slack
# text can quote the same expected S/N the solver aimed at. Re-exported here
# because this module is where they are used.
FWHM_PER_SIGMA = inject_calib.FWHM_PER_SIGMA
MIN_RENDERABLE_FWHM_MS = inject_calib.MIN_RENDERABLE_FWHM_MS
sample_fwhm_ms = inject_calib.sample_fwhm_ms
rec_per_true = inject_calib.rec_per_true
capped_target_snr = inject_calib.capped_target_snr


def ledger_row(conn, inj_id: int) -> dict | None:
    """One injections row as a plain dict, for the Slack text builders."""
    cur = conn.execute("SELECT * FROM injections WHERE id = ?", (inj_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row))


def reconcile(conn, inj_id: int, cfg: dict) -> None:
    """Fill the gate columns for one injection from the clusters table."""
    row = conn.execute(
        "SELECT inject_utc, beam, dm, fail_reason FROM injections WHERE id = ?",
        (inj_id,)).fetchone()
    if row is None:
        return
    inj_utc, beam, dm, prior_fail = row
    t0 = datetime.fromisoformat(inj_utc)
    # The pulse lands in the data stream BEFORE inject_utc: the sidecar is
    # added to the next assembled gulp, whose samples are already 5-18 s old
    # (measured 2026-08-15 to 09-01, drifting later; casm-wiki
    # injection-saturation.md). The old [-10, +90] window declared most
    # late-August shots t1_no_detection although hella had found them.
    lo = (t0 - timedelta(seconds=40)).isoformat(timespec="milliseconds")
    hi = (t0 + timedelta(seconds=90)).isoformat(timespec="milliseconds")
    dm_tol = max(0.15 * dm, 5.0)
    # Prefer the fast-triggered cluster: that is the one with the dump,
    # the plot, and the trigger audit attached.
    cand = conn.execute(
        "SELECT id, snr, dm, tier, tags, n_beams, beam, width, samp, event_utc"
        " FROM clusters"
        " WHERE event_utc BETWEEN ? AND ? AND beam_lo <= ? AND beam_hi >= ?"
        " AND dm_lo <= ? AND dm_hi >= ?"
        " ORDER BY (tags LIKE '%fast_triggered%') DESC, snr DESC LIMIT 1",
        (lo, hi, beam + 2, beam - 2, dm + dm_tol, dm - dm_tol)).fetchone()

    filt = cfg.get("filters", {})
    tiers = cfg.get("tiers", {})
    if cand is None:
        # No cluster at all. T2 stores every injection-tagged cluster, so a
        # missing row means T1 never reported trials worth clustering.
        gates = dict(gate_t1=0, gate_t2=0, gate_trigger=0,
                     fail_reason="t1_no_detection")
        rec = (None, None, None)
        detail = (None, None, None, None)
    else:
        cid, snr, rdm, tier, tags, n_beams, cbeam, cwidth, csamp, cutc = cand
        would = (tier in ("A", "B") and rdm >= filt.get("dm_floor", 20.0)
                 and n_beams <= filt.get("max_nbeam", 32)
                 and cbeam not in set(filt.get("beam_veto", [])))
        gates = dict(gate_t1=1, gate_t2=1, gate_trigger=int(would),
                     fail_reason=None if would else
                     f"trigger_filters(tier={tier},nbeam={n_beams})")
        rec = (cid, snr, rdm)
        # Negative lead is the normal case: the sidecar joins a gulp whose
        # samples are already seconds old, so the pulse arrives in the search
        # stream BEFORE the FIFO write that scheduled it.
        try:
            lead = (datetime.fromisoformat(cutc) - t0).total_seconds()
        except (TypeError, ValueError):
            lead = None
        detail = (cwidth, cbeam, csamp, lead)
    outcome = inject_outcome.classify(gates["gate_t1"], gates["gate_t2"],
                                      gates["gate_trigger"], prior_fail)
    with conn:
        conn.execute(
            "UPDATE injections SET gate_t1=?, gate_t2=?, gate_trigger=?,"
            " fail_reason=?, matched_cluster=?, rec_snr=?, rec_dm=?,"
            " rec_width=?, rec_beam=?, rec_samp=?, rec_lead_s=?, outcome=?"
            " WHERE id=?",
            (gates["gate_t1"], gates["gate_t2"], gates["gate_trigger"],
             gates["fail_reason"], *rec, *detail, outcome, inj_id))
    logger.info("reconciled injection %d: t1=%s t2=%s trigger=%s (%s)",
                inj_id, gates["gate_t1"], gates["gate_t2"], gates["gate_trigger"],
                gates["fail_reason"] or "recovered")


async def run(cfg: dict, once: bool, force: dict | None = None) -> None:  # noqa: C901
    force = force or {}
    icfg = cfg.get("injection", {})
    scratch = Path(icfg.get("scratch_dir", "/mnt/nvme5/casm_pipeline/injections"))
    scratch.mkdir(parents=True, exist_ok=True)
    streams = icfg.get("streams", [0, 1, 2, 3])
    cadence_s = icfg.get("cadence_min", 30) * 60.0
    conn = db.connect(cfg.get("db", db.DEFAULT_PATH))
    # Ships disabled: with injection.slack.enabled false every poster call is
    # a no-op, so the deployed daemon behaves exactly as it did before.
    poster = inject_slack.poster_from_cfg(icfg)
    i = 0
    first = True
    while True:
        # Hold the cadence on startup too: otherwise every daemon restart
        # fires a surprise injection (and a dump) immediately.
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
        dm = force.get("dm") or random.uniform(*icfg.get("dm_range", [100.0, 1000.0]))
        # Widths are FWHM everywhere except the generator call and the
        # sigma_ms ledger column, both of which want the Gaussian sigma.
        fwhm_ms = force.get("fwhm_ms") or sample_fwhm_ms(
            random, *icfg.get("fwhm_ms_range", [2.5, 30.0]))
        sigma_ms = fwhm_ms / FWHM_PER_SIGMA
        if force.get("target_snr") is not None or "target_rec_snr_range" in icfg:
            # Amplitude from a target hella-REPORTED S/N and the live beam std.
            # The fixed count range assumed the pre-Route-Z beam scale (u8 rail,
            # casm-wiki injection-saturation.md); counts are now fp16 units of
            # a stream whose std is ~36-46. The reported/analytical-true ratio
            # is width-dependent (rec_per_true above), so the amplitude scales
            # with the injected width and the target reported S/N does not.
            target_rec = capped_target_snr(
                force.get("target_snr") or random.uniform(
                    *icfg.get("target_rec_snr_range", [18.0, 30.0])), icfg)
            k = rec_per_true(fwhm_ms, icfg)
            nchan_usable = int(icfg.get("nchan_usable", 2880))
            amp, sigma_n = amp_for_target_snr(
                target_rec / k, sigma_ms, beam, nchan_usable)
            logger.info("target reported S/N %.1f (true %.1f at rec_per_true %.2f "
                        "for FWHM %.1f ms), live std %.2f -> amp %.0f counts",
                        target_rec, target_rec / k, k, fwhm_ms, sigma_n, amp)
        else:
            amp = random.uniform(*icfg.get("amp_range", [25.0, 45.0]))
            target_rec = sigma_n = nchan_usable = None
        file_id = f"inj_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_b{beam:03d}"

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
                " est_snr, file_id, created_utc, target_snr, sigma_n, nchan_usable)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(timespec="milliseconds"), stream, beam, dm, amp,
                 sigma_ms, est_snr, file_id, now.isoformat(timespec="milliseconds"),
                 target_rec, sigma_n, nchan_usable))
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

        try:
            ts = poster.post_sent(ledger_row(conn, inj_id))
            if ts:
                with conn:
                    conn.execute("UPDATE injections SET slack_ts=? WHERE id=?",
                                 (ts, inj_id))
        except Exception:
            logger.exception("slack sent-post for injection %d failed", inj_id)

        # truth plot for the web gallery, rendered from the injected .fil
        # (dumps tap upstream of the injection merge and cannot show it)
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

        # T1 reports 20-30 s late; give it 3 minutes, then attribute gates.
        await asyncio.sleep(180)
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

        # rolling scratch cleanup: keep the last ~20 injections of work files
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
    p.add_argument("--target-snr", type=float,
                   help="force the target hella-reported S/N; amplitude solved from "
                        "the live std and the per-width rec_per_true table. Still "
                        "clamped to injection.target_rec_snr_max")
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
             "target_snr": args.target_snr}
    try:
        asyncio.run(run(cfg, args.once, force))
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
