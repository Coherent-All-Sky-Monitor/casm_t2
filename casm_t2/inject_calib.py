"""Width and amplitude calibration for live injections.

Pure functions, no I/O. They live outside `apps.inject_daemon` because both
the solver (which picks the amplitude before firing) and the Slack text
(which quotes the S/N the shot is expected to be reported at) need the same
numbers, and the daemon already imports the Slack module.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger("t2.inject")

#: Widths are quoted as FWHM everywhere a human sees them. The generator
#: takes a Gaussian sigma, so this is the only place the two meet.
FWHM_PER_SIGMA = 2.355

#: make_noise_fil_with_frb_snr.gen_filterbank floors the rendered pulse at
#: `sigma_samp = max(1.0, ...)` and exposes no argument to lower it, so a
#: request narrower than one sample of sigma silently renders at one sample.
#: That puts a hard floor of 2.355 * 1.048576 ms on any injected FWHM.
MIN_RENDERABLE_FWHM_MS = FWHM_PER_SIGMA * 1.048576  # 2.469 ms

#: Used when no config is at hand (the preview CLI reads a database, not a
#: t2d.yaml). Keep in step with `injection.rec_per_true_table` in the config;
#: measured 2026-09-09 with T1 subtraction off.
DEFAULT_REC_PER_TRUE_TABLE = [[4.7, 2.24], [11.8, 2.08], [23.5, 1.22]]

#: Reported-S/N ceiling as a function of injected width, in the shape the
#: config uses. Above the ceiling the injected pulse fills hella's 10000-peak
#: per-gulp candidate buffer and the gulp stops being searched across the
#: whole beam set. The footprint of a candidate grows with width, so a wide
#: pulse saturates at a lower reported S/N than a narrow one.
DEFAULT_REPORTED_SNR_CAP = {
    "narrow": 50.0,        # FWHM below narrow_max_ms
    "narrow_max_ms": 6.0,
    "mid": 40.0,           # FWHM from narrow_max_ms to mid_max_ms
    "mid_max_ms": 15.0,
    "wide_base": 40.0,     # above mid_max_ms: wide_base * sqrt(wide_ref_ms/FWHM)
    "wide_ref_ms": 12.0,
}


def reported_snr_cap(fwhm_ms: float, icfg: dict | None = None) -> float:
    """Highest reported S/N this width may be injected at.

    Extrapolated from 2026-09-09: reported 43 at FWHM 4.7 ms and 35.5 at
    11.8 ms left the gulp intact; reported 78 at 11.8 ms filled the buffer
    (31/64 beams searched) and 132 filled it harder (43/64). Flat below
    15 ms, then falling as 1/sqrt(FWHM) because a wider candidate occupies
    more of the buffer per detection: 25 at FWHM 30 ms.

    A config carrying only the legacy flat `target_rec_snr_max` is honoured
    as a width-independent cap.
    """
    icfg = icfg or {}
    cfg = icfg.get("reported_snr_cap")
    if not cfg:
        legacy = icfg.get("target_rec_snr_max")
        if legacy is not None:
            return float(legacy)
        cfg = DEFAULT_REPORTED_SNR_CAP
    d = DEFAULT_REPORTED_SNR_CAP
    fwhm = max(float(fwhm_ms), 1e-6)
    if fwhm < float(cfg.get("narrow_max_ms", d["narrow_max_ms"])):
        return float(cfg.get("narrow", d["narrow"]))
    if fwhm <= float(cfg.get("mid_max_ms", d["mid_max_ms"])):
        return float(cfg.get("mid", d["mid"]))
    base = float(cfg.get("wide_base", d["wide_base"]))
    ref = float(cfg.get("wide_ref_ms", d["wide_ref_ms"]))
    return base * math.sqrt(ref / fwhm)


def sample_inject_snr(rng, lo: float, hi: float) -> float:
    """Draw an injected (true, analytic) S/N log-uniformly."""
    lo, hi = float(lo), max(float(hi), float(lo))
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def clamp_inject_snr(inject_snr: float, fwhm_ms: float,
                     icfg: dict | None = None) -> tuple[float, float, bool]:
    """Hold the PREDICTED reported S/N under the width's cap.

    The sampled quantity is the injected (true) S/N, which is what the
    amplitude solver needs. What saturates hella is the *reported* S/N, so
    predict it with the rec_per_true table and, if it is over the ceiling,
    scale the injected S/N down to sit exactly on it.

    Returns (injected S/N, predicted reported S/N, whether it was clamped).
    """
    k = rec_per_true(fwhm_ms, icfg)
    cap = reported_snr_cap(fwhm_ms, icfg)
    predicted = inject_snr * k
    if predicted <= cap:
        return float(inject_snr), float(predicted), False
    scaled = cap / k
    logger.warning("injected S/N %.1f at FWHM %.1f ms would be reported at "
                   "%.1f, over the %.1f cap for that width; scaled to %.1f "
                   "(reported %.1f)", inject_snr, fwhm_ms, predicted, cap,
                   scaled, cap)
    return float(scaled), float(cap), True


def sample_fwhm_ms(rng, lo_ms: float, hi_ms: float) -> float:
    """Draw an injected FWHM log-uniformly, clamped to what the generator can render.

    Log-uniform, not uniform: the trials are spaced in powers of two, so a
    uniform draw over 2.5-30 ms would put two thirds of the shots on the two
    widest trials and almost never exercise the narrow ones.
    """
    lo = max(float(lo_ms), MIN_RENDERABLE_FWHM_MS)
    hi = max(float(hi_ms), lo)
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def rec_per_true(fwhm_ms: float, icfg: dict | None = None) -> float:
    """hella-reported S/N divided by the analytical matched-filter S/N.

    hella's matched trial is a smoothing kernel of FWHM about 0.67 * 2**ibox
    samples over rows normalised to unit variance, not the unit-area boxcar
    the analytical formula assumes, so the ratio is a function of width and
    not a constant. `injection.rec_per_true_table` holds measured
    [fwhm_ms, ratio] pairs and this interpolates linearly in log FWHM,
    holding the end values flat outside the measured range.

    Falls back to the scalar `injection.rec_per_true` when a config is given
    with no table (what the deployed config did before), and to
    DEFAULT_REC_PER_TRUE_TABLE when no config is given at all.
    """
    if icfg is None:
        table = DEFAULT_REC_PER_TRUE_TABLE
    else:
        table = icfg.get("rec_per_true_table")
        if not table:
            return float(icfg.get("rec_per_true", 1.5))
    pts = sorted((float(w), float(r)) for w, r in table)
    if len(pts) == 1:
        return pts[0][1]
    x = math.log(max(float(fwhm_ms), 1e-6))
    xs = [math.log(w) for w, _ in pts]
    ys = [r for _, r in pts]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + f * (ys[i] - ys[i - 1])
    return ys[-1]
