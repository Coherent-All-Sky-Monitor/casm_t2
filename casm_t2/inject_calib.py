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


#: hella's DM grid. A shot outside it cannot be recovered at its own DM, so
#: a draw is clamped here rather than silently wasted.
DM_MIN, DM_MAX = 0.0, 1000.0


class SpecError(ValueError):
    """A `sample:` entry that cannot be drawn from."""


def draw(spec, rng) -> float:
    """One value from a distribution spec.

    Specs are the `injection.sample` block: a dict with `dist` plus the
    arguments that distribution needs.

        {dist: uniform,    lo: 100, hi: 900}
        {dist: loguniform, lo: 2.5, hi: 30.0}
        {dist: choice,     values: [3, 8, 20], weights: [1, 2, 1]}
        {dist: fixed,      value: 15}

    `loguniform` is the right default for anything spanning a decade and
    sampled against powers-of-two search trials; `choice` and `fixed`
    express a calibration grid, where the point is to repeat the same few
    settings rather than to cover a range.

    Raises SpecError on anything malformed: a bad spec is a config mistake
    that should stop the daemon at startup, not quietly produce injections
    at the wrong brightness for a week.
    """
    if not isinstance(spec, dict):
        raise SpecError(f"sample spec must be a mapping, got {spec!r}")
    dist = str(spec.get("dist", "")).lower()

    if dist == "fixed":
        if "value" not in spec:
            raise SpecError("fixed needs `value`")
        return float(spec["value"])

    if dist == "choice":
        values = spec.get("values")
        if not values:
            raise SpecError("choice needs a non-empty `values`")
        weights = spec.get("weights")
        if weights is not None:
            if len(weights) != len(values):
                raise SpecError("choice `weights` must match `values` in length")
            if any(w < 0 for w in weights) or sum(weights) <= 0:
                raise SpecError("choice `weights` must be non-negative and sum > 0")
            return float(rng.choices(list(values), weights=list(weights))[0])
        return float(rng.choice(list(values)))

    if dist in ("uniform", "loguniform"):
        if "lo" not in spec or "hi" not in spec:
            raise SpecError(f"{dist} needs `lo` and `hi`")
        lo, hi = float(spec["lo"]), float(spec["hi"])
        if not lo < hi:
            raise SpecError(f"{dist} needs lo < hi, got lo={lo} hi={hi}")
        if dist == "uniform":
            return float(rng.uniform(lo, hi))
        if lo <= 0:
            raise SpecError(f"loguniform needs lo > 0, got lo={lo}")
        return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))

    raise SpecError(f"unknown dist {spec.get('dist')!r}")


def sample_spec(icfg: dict, name: str, rng, legacy_key: str | None = None,
                legacy_log=logger) -> float:
    """Draw one shot parameter from `injection.sample`, or an old range key.

    The old `dm_range` / `fwhm_ms_range` / `inject_snr_range` lists still
    work so a config from before the `sample:` block keeps running, but they
    warn: two ways to say the same thing is how a config drifts out of step
    with what is actually being injected.
    """
    spec = ((icfg or {}).get("sample") or {}).get(name)
    if spec is not None:
        return draw(spec, rng)
    if legacy_key and (icfg or {}).get(legacy_key):
        lo, hi = (icfg or {})[legacy_key][:2]
        legacy_log.warning(
            "injection.%s is deprecated; use injection.sample.%s "
            "{dist: %s, lo: %s, hi: %s}", legacy_key, name,
            "uniform" if name == "dm" else "loguniform", lo, hi)
        dist = "uniform" if name == "dm" else "loguniform"
        return draw({"dist": dist, "lo": lo, "hi": hi}, rng)
    raise SpecError(f"no injection.sample.{name} and no {legacy_key}")


def clamp_fwhm_ms(fwhm_ms: float, log=logger) -> float:
    """Hold a drawn FWHM at what the generator can actually render."""
    if fwhm_ms < MIN_RENDERABLE_FWHM_MS:
        log.warning("injected FWHM %.2f ms is below the %.2f ms the generator "
                    "can render (it floors sigma at one sample); using the "
                    "floor", fwhm_ms, MIN_RENDERABLE_FWHM_MS)
        return MIN_RENDERABLE_FWHM_MS
    return float(fwhm_ms)


def clamp_dm(dm: float, log=logger) -> float:
    """Hold a drawn DM on hella's search grid."""
    if not DM_MIN <= dm <= DM_MAX:
        clamped = min(max(dm, DM_MIN), DM_MAX)
        log.warning("injected DM %.1f is off hella's grid [%.0f, %.0f]; "
                    "using %.1f", dm, DM_MIN, DM_MAX, clamped)
        return clamped
    return float(dm)


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
