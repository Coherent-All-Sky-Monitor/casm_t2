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

DEFAULT_TARGET_SNR_MAX = 40.0


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


def capped_target_snr(target: float, icfg: dict) -> float:
    """Clamp the requested reported S/N to the saturation cap.

    A bright enough injection fills hella's per-gulp candidate buffer: the
    2026-09-09 shots at reported 78 (id 663) and 132 (id 664) each hit the
    10000-peak cap for that gulp, with 31/64 and 43/64 beams searched, so
    the injected gulp stopped being a fair sample of the sky. Reported 43
    and below did not. The cap applies to CLI --target-snr too: a test shot
    is not a reason to blind the search for a gulp.
    """
    cap = float((icfg or {}).get("target_rec_snr_max", DEFAULT_TARGET_SNR_MAX))
    if target > cap:
        logger.warning("target reported S/N %.1f exceeds target_rec_snr_max "
                       "%.1f (hella saturates its gulp above ~45); clamped to %.1f",
                       target, cap, cap)
        return cap
    return float(target)
