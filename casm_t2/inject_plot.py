"""Truth plot for an injection, rendered from the generated filterbank.

Intensity dumps tap CAND_DUMP_BLOCK 0, upstream of where casm_beam_inj's
data is merged into the search stream, so a dump can never contain an
injected pulse. The gallery therefore shows the injection as generated:
the .fil that was pushed into the FIFO, as a raw waterfall plus the
profile dedispersed at the injected DM.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from casm_t2 import timing


def render(fil_path: str | Path, dm: float, est_snr: float | None,
           out_png: str | Path, title: str) -> Path:
    from casm_io.filterbank import FilterbankFile

    fb = FilterbankFile(str(fil_path))
    data = np.asarray(fb.data, dtype=np.float32)
    if data.shape[0] != timing.NCHAN:        # ensure (nchan, ntime)
        data = data.T
    freqs = timing.F_TOP_MHZ - np.arange(timing.NCHAN) * timing.CHAN_WIDTH_MHZ
    tsamp = timing.TSAMP_S

    shifts = np.round(4.148808e3 * dm * (freqs**-2 - freqs[0]**-2) / tsamp).astype(int)
    dedis = np.empty_like(data)
    for i in range(data.shape[0]):
        dedis[i] = np.roll(data[i], -shifts[i])
    prof = dedis.mean(axis=0)
    t = np.arange(data.shape[1]) * tsamp

    fig, (ax_wf, ax_pr) = plt.subplots(
        2, 1, figsize=(8, 7), height_ratios=(2.2, 1.0), sharex=True)
    med = np.median(data)
    sigma = 1.4826 * np.median(np.abs(data - med)) or data.std() or 1.0
    ax_wf.imshow(data, aspect="auto", interpolation="nearest",
                 extent=[t[0], t[-1], freqs[-1], freqs[0]],
                 vmin=med - sigma, vmax=med + 7 * sigma, cmap="viridis")
    ax_wf.set_ylabel("Freq (MHz)")
    ax_pr.plot(t, prof, "k-", lw=0.8)
    ax_pr.set_xlabel("Time (s)")
    ax_pr.set_ylabel("Power (arb.)")
    ax_pr.set_title(f"dedispersed at DM={dm:.1f}", fontsize=9)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110)
    plt.close(fig)
    return out_png
