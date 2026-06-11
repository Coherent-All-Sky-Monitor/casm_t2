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
import random
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from casm_t2 import db, inject_plot, logsetup

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


def reconcile(conn, inj_id: int, cfg: dict) -> None:
    """Fill the gate columns for one injection from the clusters table."""
    row = conn.execute("SELECT inject_utc, beam, dm FROM injections WHERE id = ?",
                       (inj_id,)).fetchone()
    if row is None:
        return
    inj_utc, beam, dm = row
    t0 = datetime.fromisoformat(inj_utc)
    lo = (t0 - timedelta(seconds=10)).isoformat(timespec="milliseconds")
    hi = (t0 + timedelta(seconds=90)).isoformat(timespec="milliseconds")
    dm_tol = max(0.15 * dm, 5.0)
    # Prefer the fast-triggered cluster: that is the one with the dump,
    # the plot, and the trigger audit attached.
    cand = conn.execute(
        "SELECT id, snr, dm, tier, tags, n_beams, beam FROM clusters"
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
    else:
        cid, snr, rdm, tier, tags, n_beams, cbeam = cand
        would = (tier in ("A", "B") and rdm >= filt.get("dm_floor", 20.0)
                 and n_beams <= filt.get("max_nbeam", 32)
                 and cbeam not in set(filt.get("beam_veto", [])))
        gates = dict(gate_t1=1, gate_t2=1, gate_trigger=int(would),
                     fail_reason=None if would else
                     f"trigger_filters(tier={tier},nbeam={n_beams})")
        rec = (cid, snr, rdm)
    with conn:
        conn.execute(
            "UPDATE injections SET gate_t1=?, gate_t2=?, gate_trigger=?,"
            " fail_reason=?, matched_cluster=?, rec_snr=?, rec_dm=? WHERE id=?",
            (gates["gate_t1"], gates["gate_t2"], gates["gate_trigger"],
             gates["fail_reason"], *rec, inj_id))
    logger.info("reconciled injection %d: t1=%s t2=%s trigger=%s (%s)",
                inj_id, gates["gate_t1"], gates["gate_t2"], gates["gate_trigger"],
                gates["fail_reason"] or "recovered")


async def run(cfg: dict, once: bool) -> None:  # noqa: C901
    icfg = cfg.get("injection", {})
    scratch = Path(icfg.get("scratch_dir", "/mnt/nvme5/casm_pipeline/injections"))
    scratch.mkdir(parents=True, exist_ok=True)
    streams = icfg.get("streams", [0, 1, 2, 3])
    cadence_s = icfg.get("cadence_min", 30) * 60.0
    conn = db.connect(cfg.get("db", db.DEFAULT_PATH))
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
        beam = stream * 64 + local_beam
        dm = random.uniform(*icfg.get("dm_range", [100.0, 1000.0]))
        amp = random.uniform(*icfg.get("amp_range", [25.0, 45.0]))
        sigma_ms = random.uniform(*icfg.get("sigma_ms_range", [1.0, 10.0]))
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
                " est_snr, file_id, created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (now.isoformat(timespec="milliseconds"), stream, beam, dm, amp,
                 sigma_ms, est_snr, file_id, now.isoformat(timespec="milliseconds")))
            inj_id = cur.lastrowid
        fifo = f"/tmp/beaminj.fifo.{stream}"
        try:
            data = dada.read_bytes()
            await asyncio.to_thread(Path(fifo).write_bytes, data)
        except OSError as exc:
            logger.error("FIFO write to %s failed: %s", fifo, exc)
            with conn:
                conn.execute("UPDATE injections SET fail_reason=? WHERE id=?",
                             (f"fifo_write_failed:{exc}", inj_id))
            if once:
                return
            await asyncio.sleep(cadence_s)
            continue
        logger.info("injection %d: beam %d (stream %d) dm=%.1f amp=%.1f "
                    "sigma=%.1fms est_snr=%s", inj_id, beam, stream, dm, amp,
                    sigma_ms, f"{est_snr:.1f}" if est_snr else "?")

        # truth plot for the web gallery, rendered from the injected .fil
        # (dumps tap upstream of the injection merge and cannot show it)
        try:
            png_dir = Path(icfg.get("plot_dir",
                                    "/mnt/nvme5/casm_pipeline/candidates/injections"))
            await asyncio.to_thread(
                inject_plot.render, scratch / f"{file_id}.fil", dm, est_snr,
                png_dir / f"{file_id}.png",
                f"injection {inj_id}   beam {beam}   DM={dm:.1f}   "
                f"sigma={sigma_ms:.1f} ms   est S/N={est_snr or float('nan'):.0f}")
        except Exception:
            logger.exception("truth plot for injection %d failed", inj_id)

        # T1 reports 20-30 s late; give it 3 minutes, then attribute gates.
        await asyncio.sleep(180)
        try:
            reconcile(conn, inj_id, cfg)
        except Exception:
            logger.exception("reconcile of injection %d failed", inj_id)

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
    args = p.parse_args()
    logsetup.setup(args.log_file)
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    try:
        asyncio.run(run(cfg, args.once))
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
