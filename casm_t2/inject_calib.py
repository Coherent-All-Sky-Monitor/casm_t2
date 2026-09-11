"""Width and amplitude calibration for live injections.

Kept outside `apps.inject_daemon` so the solver and the Slack text share one
copy of the numbers. No Redis and no database: the only I/O is the amplitude
solver rendering a short pulse through the generator to a temporary file, which
is how it stays bit-exact with what gets injected.
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

#: Used when no config is at hand, the preview CLI reading a database rather
#: than a t2d.yaml. Keep in step with `injection.rec_per_true_table`. Measured
#: with T1 subtraction off.
DEFAULT_REC_PER_TRUE_TABLE = [[4.7, 2.24], [11.8, 2.08], [23.5, 1.22]]

#: Reported-S/N ceiling against injected width, in the config's shape. Above it
#: the pulse fills hella's 10000-peak per-gulp buffer and the gulp stops being
#: searched across the whole beam set. A candidate's footprint grows with width,
#: so a wide pulse saturates at a lower reported S/N.
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

    Flat below mid_max_ms, then falling as 1/sqrt(FWHM), a wider candidate
    occupying more of hella's per-gulp buffer per detection. A config carrying
    only the legacy flat `target_rec_snr_max` is honoured as a width-independent
    cap.
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
    """Hold the predicted reported S/N under the width's cap.

    The sampled quantity is the injected (true) S/N, what the amplitude solver
    needs, while the reported S/N is what saturates hella. Predict it with the
    rec_per_true table and scale the injected S/N down onto the ceiling if it is
    over. Returns (injected S/N, predicted reported S/N, whether it was clamped).
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


#: hella's DM grid, pc/cc. A shot outside it cannot be recovered at its own DM,
#: so a draw is clamped.
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

    `loguniform` suits anything spanning a decade against powers-of-two search
    trials; `choice` and `fixed` express a calibration grid. Raises SpecError on
    a malformed spec, which stops the daemon at startup rather than injecting at
    the wrong brightness.
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

    The legacy `dm_range` / `fwhm_ms_range` / `inject_snr_range` lists still
    work, with a warning.
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
    """Draw an injected FWHM log-uniformly, clamped to what the generator renders.

    Log-uniform because the trials are spaced in powers of two: a uniform draw
    over 2.5-30 ms puts two thirds of the shots on the two widest trials.
    """
    lo = max(float(lo_ms), MIN_RENDERABLE_FWHM_MS)
    hi = max(float(hi_ms), lo)
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def rec_per_true(fwhm_ms: float, icfg: dict | None = None) -> float:
    """hella-reported S/N divided by the analytical matched-filter S/N.

    A function of width, not a constant: hella's matched trial is a smoothing
    kernel of FWHM about 0.67 * 2**ibox samples over unit-variance rows, not the
    unit-area boxcar the analytical formula assumes.
    `injection.rec_per_true_table` holds measured [fwhm_ms, ratio] pairs,
    interpolated linearly in log FWHM and held flat outside the measured range.
    Falls back to the scalar `injection.rec_per_true`, then to
    DEFAULT_REC_PER_TRUE_TABLE.
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


# --- amplitude solve on the rendered (u8-truncated) pulse -------------------

#: The generator the daemon shells out to. The solver imports it so the pulse
#: it scores is rendered by the same code that is injected, truncation and all.
GENERATOR_PATH = "/home/casm/software/dev/make_noise_fil_with_frb_snr.py"

#: Sample time of the beamformer stream, s.
TSAMP_S = 0.001048576

#: Lowest amplitude in stream counts the solver may return. u8 truncation eats
#: 23 percent of a 3-count pulse's matched-filter amplitude at 13 ms FWHM and
#: the loss grows fast below that, so shots under 4 counts are not calibratable.
DEFAULT_AMP_FLOOR = 4

#: Largest amplitude the u8 stream can carry.
AMP_MAX = 255

#: Redis std over corrected std. Casm_bf_proc_stat_<beam>_0_stddev is one
#: number over all 3072 channels about the beam mean, so bandpass structure
#: inflates it over the per-channel std in the searched band: measured 1.33
#: (beam 22), 1.41 (beam 6), 1.47 (beam 14).
DEFAULT_STD_BANDPASS_FACTOR = 1.4


def std_bandpass_factor(icfg: dict | None = None) -> float:
    """Divisor taking the Redis std to a per-channel std in the searched band."""
    factor = float((icfg or {}).get("std_bandpass_factor",
                                    DEFAULT_STD_BANDPASS_FACTOR))
    if factor <= 0:
        raise SpecError(f"injection.std_bandpass_factor must be > 0, got {factor}")
    return factor


def corrected_std(raw_std: float, icfg: dict | None = None) -> tuple[float, float]:
    """(per-channel std in the searched band, factor used)."""
    factor = std_bandpass_factor(icfg)
    return float(raw_std) / factor, factor


def _generator():
    """The generator module, imported from its script path."""
    import sys
    parent = str(__import__("pathlib").Path(GENERATOR_PATH).parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    import make_noise_fil_with_frb_snr as mn  # noqa: E402
    return mn


_RENDER_CACHE: dict = {}


def rendered_pulse_counts(amp_counts: int, sigma_samp: float):
    """One channel of the pulse as the generator renders it, u8 counts.

    Calls gen_filterbank at DM 0 with no noise and reads channel 0 back, so the
    `np.clip(...).astype(np.uint8)` truncation is the online one rather than a
    copy of it. Every channel carries the same profile at DM 0.
    """
    import contextlib
    import io
    import tempfile
    from pathlib import Path

    import numpy as np

    key = (int(amp_counts), round(float(sigma_samp), 6))
    hit = _RENDER_CACHE.get(key)
    if hit is not None:
        return hit
    mn = _generator()
    # gen_filterbank centres the pulse at nsamp // 2 and paints +-4 sigma.
    halfwin = int(math.ceil(4.0 * max(float(sigma_samp), 1.0)))
    nsamp = 2 * (halfwin + 8)
    sigma_ms_gen = float(sigma_samp) * 1000.0 * mn.tsamp_s
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "amp_solve.fil"
        with contextlib.redirect_stdout(io.StringIO()):
            mn.gen_filterbank(duration_samps=nsamp, DM=0.0,
                              pulse_amp_counts=float(amp_counts),
                              pulse_sigma_ms=sigma_ms_gen,
                              output_fname_base=str(out), with_noise=False)
        raw = out.read_bytes()
    body = raw[len(raw) - nsamp * mn.nchans:]
    prof = np.frombuffer(body, dtype=np.uint8).reshape(nsamp, mn.nchans)[:, 0]
    prof = prof.astype(np.float64)
    _RENDER_CACHE[key] = prof
    return prof


def truncated_snr(amp_counts: int, sigma_ms: float, std_per_channel: float,
                  nchan_usable: int, tsamp_s: float = TSAMP_S) -> float:
    """Matched-filter S/N of the rendered pulse over nchan_usable channels.

    S/N = sqrt(nchan) * ||p||_2 / sigma_n for a profile p in counts repeated
    over independent channels, which for an untruncated Gaussian is exactly the
    analytic (A / sigma_n) sqrt(N) sqrt(sigma_t sqrt(pi)). Rendering p through
    the generator is what puts the u8 truncation into the number.
    """
    sigma_samp = max(float(sigma_ms) / (1000.0 * float(tsamp_s)), 1.0)
    prof = rendered_pulse_counts(int(amp_counts), sigma_samp)
    return float(math.sqrt(int(nchan_usable)) * math.sqrt(float((prof ** 2).sum()))
                 / float(std_per_channel))


def fluence_retention(amp_counts: int, sigma_ms: float,
                      tsamp_s: float = TSAMP_S) -> float:
    """Counts surviving u8 truncation over the ideal Gaussian's counts.

    Diagnostic only: the S/N loss goes as the ||p||_2 ratio, which is milder.
    """
    sigma_samp = max(float(sigma_ms) / (1000.0 * float(tsamp_s)), 1.0)
    prof = rendered_pulse_counts(int(amp_counts), sigma_samp)
    return float(prof.sum() / (int(amp_counts) * sigma_samp * math.sqrt(2.0 * math.pi)))


def amp_for_injected_snr(target_snr: float, sigma_ms: float,
                         std_per_channel: float, nchan_usable: int,
                         tsamp_s: float = TSAMP_S,
                         amp_floor: int = DEFAULT_AMP_FLOOR,
                         log=logger) -> tuple[int, float]:
    """Smallest integer amplitude reaching `target_snr`, and its predicted S/N.

    The amplitude is quantised to u8 counts in the stream, so the solve is over
    integers and scores the truncated pulse, not the Gaussian that was asked
    for. Never returns below `amp_floor`, nor above AMP_MAX, where it warns that
    the target is out of reach. `std_per_channel` is the per-channel std in the
    searched band, the Redis value already divided by std_bandpass_factor.
    Returns (amp_counts, predicted injected S/N).
    """
    floor_amp = max(1, int(amp_floor))

    def snr(a: int) -> float:
        return truncated_snr(a, sigma_ms, std_per_channel, nchan_usable, tsamp_s)

    if snr(floor_amp) >= float(target_snr):
        return floor_amp, snr(floor_amp)
    if snr(AMP_MAX) < float(target_snr):
        log.warning("injected S/N %.1f needs more than %d counts at FWHM "
                    "%.1f ms and std %.2f; using %d (S/N %.1f)", target_snr,
                    AMP_MAX, float(sigma_ms) * FWHM_PER_SIGMA, std_per_channel,
                    AMP_MAX, snr(AMP_MAX))
        return AMP_MAX, snr(AMP_MAX)
    # S/N is non-decreasing in amplitude (floor() of a scaled profile is), so
    # bisect for the crossing rather than render every count.
    lo, hi = floor_amp, AMP_MAX
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if snr(mid) >= float(target_snr):
            hi = mid
        else:
            lo = mid
    return hi, snr(hi)
