"""Effective width of hella's smoothing kernel, per boxcar trial.

hella does not convolve with a boxcar. Trial `ibox` smooths with a squared
polynomial approximation to exp(-x^2/2) (casm-hella `src/hella/src/smooth.cpp`)
over a window of 2*sm+1 samples, sm = 2**ibox:

    x = (j - sm) / (sm / 2.355),   j = 0 .. 2*sm
    v = 1 - x^2/2 + (x^2/4)*(x^2/4) - (x^2*0.083)^3
    kernel = v^2, clipped at zero and normalised

The matching kernel is narrower than the 2**ibox trial label: FWHM about
0.67 * 2**ibox samples. `kernel_fwhm_ms` is the number to quote and plot.
Computed from the polynomial, not hardcoded, so smooth.cpp changes land here:

    ibox 0 ->  1.0 ms      ibox 4 -> 11.5 ms
    ibox 1 ->  1.0 ms      ibox 5 -> 22.0 ms
    ibox 2 ->  3.1 ms      ibox 6 -> 45.1 ms  (vetoed by t2d, never injected)
    ibox 3 ->  5.2 ms

ibox 0 and 1 share a kernel FWHM of one sample, the window being too short to
resolve anything narrower. Their equivalent boxcar widths (1.1, 1.8 ms) differ.
"""

from __future__ import annotations

import numpy as np

TSAMP_MS = 1.048576

#: t2d's width veto drops clusters at this ibox and above, so injections must
#: stay narrow enough to land below it.
VETOED_IBOX = 6


def _kernel(ibox: int) -> np.ndarray:
    """The normalised smoothing kernel for one trial, as smooth.cpp builds it."""
    sm = 2 ** int(ibox)
    j = np.arange(2 * sm + 1)
    x = (j - sm) / (sm / 2.355)
    x2 = x ** 2
    v = 1 - 0.5 * x2 + 0.25 * x2 * 0.25 * x2 - 0.083 * x2 * 0.083 * x2 * 0.083 * x2
    k = np.clip(v * v, 0, None)
    return k / k.sum()


def kernel_fwhm_samp(ibox: int) -> int:
    """FWHM of the trial's smoothing kernel, in samples (integer, as sampled)."""
    k = _kernel(ibox)
    above = np.where(k >= k.max() / 2)[0]
    return int(above[-1] - above[0] + 1)


def kernel_fwhm_ms(ibox: int, tsamp_ms: float = TSAMP_MS) -> float:
    """FWHM of the trial's smoothing kernel, in milliseconds.

    The width to quote for a recovered candidate, not the 2**ibox trial label.
    """
    return kernel_fwhm_samp(ibox) * tsamp_ms


def boxcar_equiv_samp(ibox: int) -> float:
    """Boxcar width with the same noise averaging as the kernel, in samples.

    1 / sum(k^2) for a normalised kernel. About 0.93 * 2**ibox above ibox 1.
    """
    k = _kernel(ibox)
    return float(1.0 / (k ** 2).sum())


def boxcar_equiv_ms(ibox: int, tsamp_ms: float = TSAMP_MS) -> float:
    return boxcar_equiv_samp(ibox) * tsamp_ms


def best_ibox_for_fwhm(fwhm_ms: float, max_ibox: int = VETOED_IBOX - 1) -> int:
    """The trial hella is expected to report for a pulse of this FWHM.

    The widest trial whose kernel FWHM still fits inside the pulse, not the
    nearest: on a powers-of-two grid the wider kernel adds more noise than
    signal. hella's real choice also depends on S/N and the gulp noise, so this
    is for checking that a sampling range exercises the intended trials.
    """
    fits = [b for b in range(0, max_ibox + 1) if kernel_fwhm_ms(b) <= fwhm_ms]
    return max(fits) if fits else 0
