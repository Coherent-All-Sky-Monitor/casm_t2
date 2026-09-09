"""Slack messages for the injection bot: text builders, figures, transport.

Split the way the DSA-110 inject bot splits it, and for the same reason: the
message *text* is pure functions of a ledger row, so it can be rendered and
reviewed offline (see `t2-inject-slack-preview`) without a token, a network,
or a running daemon. Only `SlackPoster` touches the network.

One message per injection. The "sent" message posts as soon as the FIFO write
succeeds and its `ts` goes into the ledger; when reconcile finishes ~3 min
later the same message is EDITED in place, with a coloured attachment holding
the outcome line, so the channel holds one line per shot and a skim shows
green or red. A single miss says nothing more; a run of `streak_every`
consecutive misses posts one attention message.

Everything is fail-soft: a Slack failure logs a warning and returns None. The
injector must never stop because Slack did.

Configuration lives in `injection.slack` in t2d.yaml and ships DISABLED. The
token and channel are the same two dotfiles casm_t3.alerts uses
(``~/.config/slack_api``, ``~/.config/slack_channel``), plus an optional
``~/.config/slack_channel_injections`` that overrides the channel when it
exists, so injection chatter can live away from the candidate alerts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from casm_t2 import hella_kernel, inject_calib, inject_outcome as oc

logger = logging.getLogger("t2.inject.slack")

TOKEN_PATH = Path.home() / ".config" / "slack_api"
CHANNEL_PATH = Path.home() / ".config" / "slack_channel"
CHANNEL_OVERRIDE_PATH = Path.home() / ".config" / "slack_channel_injections"

_SLACK_API = "https://slack.com/api"
_TIMEOUT_S = 15.0

#: FWHM = 2.355 sigma. The ledger keeps the Gaussian sigma for backward
#: compatibility; every message and axis label is FWHM.
FWHM_PER_SIGMA = 2.355

NBSP = " "

COLOR_RECOVERED = "#2E7D32"
COLOR_MISSED = "#C62828"
COLOR_NEUTRAL = "#9E9E9E"
_INK = "#262626"

#: DM buckets for the summary figures, as (upper bound exclusive, label,
#: fill, marker, edge). Identity rides on colour AND marker shape, so the
#: figures survive greyscale printing and colour-blind readers.
DM_BUCKETS = [
    (250.0, "DM < 250", "#4C6EF5", "o", "#364FC7"),
    (500.0, "DM 250-500", "#F59F00", "s", "#E67700"),
    (750.0, "DM 500-750", "#12B886", "^", "#087F5B"),
    (float("inf"), "DM > 750", "#BE4BDB", "D", "#9C36B5"),
]

OUTCOME_COLORS = {
    oc.RECOVERED: COLOR_RECOVERED,
    oc.MISSED_T1: COLOR_MISSED,
    oc.MISSED_T2: "#E65100",
    oc.MISSED_TRIGGER: "#6A1B9A",
    oc.FIRE_FAILED: COLOR_NEUTRAL,
}


# ---------------------------------------------------------------------------
# row access
# ---------------------------------------------------------------------------

def _g(row, key, default=None):
    """Read one field from a dict, a sqlite3.Row, or anything mapping-like."""
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        try:
            val = getattr(row, key)
        except AttributeError:
            return default
    return default if val is None else val


def _f(row, key):
    """Float field, or None when absent/NULL/unparseable."""
    val = _g(row, key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def dm_bucket(dm: float | None):
    """(label, fill, marker, edge) for a DM, never None."""
    for hi, label, fill, marker, edge in DM_BUCKETS:
        if dm is not None and float(dm) < hi:
            return label, fill, marker, edge
    return DM_BUCKETS[-1][1:]


# ---------------------------------------------------------------------------
# message text
# ---------------------------------------------------------------------------

def injected_fwhm_ms(row) -> float | None:
    """The injected width as FWHM. The ledger stores the Gaussian sigma."""
    sigma = _f(row, "sigma_ms")
    return None if sigma is None else sigma * FWHM_PER_SIGMA


def expected_snr(row, icfg: dict | None = None) -> tuple[float | None, bool]:
    """The S/N this shot is expected to be reported at, and whether it is a guess.

    Known at fire time: the solver picked the amplitude from the live beam
    std so that hella would report `target_snr`, so that number IS the
    expectation. Legacy rows fired in the fixed-amplitude `amp_range` mode
    have no target; for those, scale the generator's own true-S/N estimate
    by the measured reported/true ratio for that width and mark it a guess.
    (`est_snr` may come from the generator's numerical matched filter rather
    than the analytical one the ratio is defined against; they agree to a
    few percent above FWHM 5 ms, which is well inside a "~".)
    """
    target = _f(row, "target_snr")
    if target is not None:
        return target, False
    est = _f(row, "est_snr")
    fwhm = injected_fwhm_ms(row)
    if est is None or fwhm is None:
        return None, True
    return est * inject_calib.rec_per_true(fwhm, icfg), True


def sent_text(row, icfg: dict | None = None) -> str:
    """The message posted the moment the injection hits the FIFO.

    Deliberately short: id, where, and the three numbers that describe the
    shot. Non-breaking spaces join every number to its unit so Slack's
    wrapping can never split "11.8 ms" across two lines.
    """
    fwhm = injected_fwhm_ms(row)
    snr, approx = expected_snr(row, icfg)
    bits = [
        f"beam {_g(row, 'beam', '?')} (stream {_g(row, 'stream', '?')})",
        f"DM {_f(row, 'dm') or 0:.0f}",
        f"FWHM {fwhm:.1f}{NBSP}ms" if fwhm is not None else "FWHM n/a",
        f"amp {_f(row, 'amp') or 0:.0f}{NBSP}counts",
        (f"expected S/N {'~' if approx else ''}{snr:.0f}" if snr is not None
         else "expected S/N n/a"),
    ]
    return (f"injection {_g(row, 'id', '?')} sent: " + ", ".join(bits)
            + "\n_awaiting recovery..._")


def outcome_text(row) -> str:
    """One line describing how the shot resolved.

    The recovered width is the FWHM of hella's smoothing kernel for the
    matched trial, not 2**ibox samples: the kernel is about 0.67 of the
    trial label wide, so the raw label overstates the pulse by half.
    """
    outcome = _g(row, "outcome")
    if outcome == oc.FIRE_FAILED:
        # Not a pipeline miss: the pulse never reached the stream.
        return "injection not fired: " + oc.short_fire_reason(
            _g(row, "fail_reason"))
    if outcome != oc.RECOVERED:
        return "NOT recovered: " + oc.explain(outcome)

    rec_snr = _f(row, "rec_snr")
    rec_dm = _f(row, "rec_dm")
    ibox = _g(row, "rec_width")

    bits = [f"recovered: S/N {rec_snr:.1f}" if rec_snr is not None
            else "recovered: S/N n/a"]
    if rec_dm is not None:
        bits.append(f"DM {rec_dm:.1f}")
    if ibox is not None:
        bits.append(f"width {hella_kernel.kernel_fwhm_ms(int(ibox)):.1f}"
                    f"{NBSP}ms (ibox {int(ibox)})")
    return ", ".join(bits)


def outcome_color(row) -> str:
    """Attachment colour: green recovered, grey never fired, red missed."""
    outcome = _g(row, "outcome")
    if outcome == oc.RECOVERED:
        return COLOR_RECOVERED
    if outcome == oc.FIRE_FAILED:
        return COLOR_NEUTRAL
    return COLOR_MISSED


def streak_text(n: int, ids, why: str | None) -> str:
    """Attention message for a run of consecutive misses."""
    return (
        f"*ATTENTION*: {n} consecutive test injections missed "
        f"({', '.join('`%s`' % i for i in ids)}). The search may be missing "
        f"real FRBs; latest loss stage: {oc.explain(why)}"
    )


def summary_text(rows, day: str) -> str:
    """The daily roll-up posted once per UTC day."""
    rows = list(rows)
    counts = {o: 0 for o in oc.ALL}
    for r in rows:
        o = str(_g(r, "outcome") or "")
        if o in counts:
            counts[o] += 1
    n = len(rows)
    lines = [f"test injections: 24 h summary, {day}",
             f"{n} injected, {counts[oc.RECOVERED]} recovered"]

    by_bucket: dict[str, list[int]] = {}
    for r in rows:
        label = dm_bucket(_f(r, "dm"))[0]
        got, tot = by_bucket.setdefault(label, [0, 0])
        by_bucket[label] = [got + (1 if _g(r, "outcome") == oc.RECOVERED else 0),
                            tot + 1]
    if by_bucket:
        order = [b[1] for b in DM_BUCKETS]
        lines.append("per DM: " + " | ".join(
            f"{label}: {by_bucket[label][0]}/{by_bucket[label][1]}"
            for label in order if label in by_bucket))

    misses = [(k, counts[k]) for k in oc.MISSES if counts[k]]
    if misses:
        lines.append("missed: " + "; ".join(f"{v} {k}" for k, v in misses))
    if counts[oc.FIRE_FAILED]:
        lines.append(f"{counts[oc.FIRE_FAILED]} fire failures "
                     "(injector plumbing, not a pipeline miss)")

    ratios = sorted(
        (_f(r, "rec_snr") / _f(r, "target_snr")) for r in rows
        if _g(r, "outcome") == oc.RECOVERED and _f(r, "rec_snr") is not None
        and _f(r, "target_snr"))
    if ratios:
        med = ratios[len(ratios) // 2]
        lines.append(f"reported/expected S/N: median {med:.2f} "
                     f"(range {ratios[0]:.2f}-{ratios[-1]:.2f})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------

def _pub_rc() -> dict:
    """Publication rcParams, applied through rc_context so nothing leaks."""
    return {
        "font.family": "serif",
        "font.serif": ["DejaVu Serif"],
        "mathtext.fontset": "dejavuserif",
        "font.size": 11.0,
        "axes.labelsize": 11.5,
        "axes.linewidth": 0.8,
        "xtick.direction": "in",
        "ytick.direction": "in",
        "xtick.major.size": 4.0,
        "ytick.major.size": 4.0,
        "xtick.minor.size": 2.2,
        "ytick.minor.size": 2.2,
        "xtick.minor.visible": True,
        "ytick.minor.visible": True,
        "xtick.top": False,
        "ytick.right": False,
        "legend.frameon": False,
        "legend.fontsize": 10.0,
        "savefig.dpi": 200,
        "savefig.facecolor": "white",
        "figure.constrained_layout.use": True,
    }


def _despine(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_INK)
    ax.tick_params(colors=_INK, labelsize=10)
    ax.yaxis.label.set_color(_INK)
    ax.xaxis.label.set_color(_INK)


def _dm_legend_handles(line2d, with_miss: bool) -> list:
    handles = [line2d([], [], marker=m, color=c, ls="none", ms=8,
                      mec=e, mew=0.9, label=label)
               for _hi, label, c, m, e in DM_BUCKETS]
    if with_miss:
        handles.append(line2d([], [], marker="o", mfc="none", mec=_INK,
                              ls="none", ms=8, mew=1.1, label="open = missed"))
    return handles


def render_summary_figures(rows, out_dir) -> list[Path]:  # noqa: C901
    """Three figures for the daily summary; returns the paths written.

    Never raises: a matplotlib problem logs a warning and returns whatever
    rendered, so the summary text still posts.
    """
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
        from matplotlib import rc_context
        from matplotlib.figure import Figure
        from matplotlib.lines import Line2D
    except Exception as exc:  # noqa: BLE001
        logger.warning("matplotlib unavailable: %s", exc)
        return []

    rows = list(rows)
    out_dir = Path(out_dir)
    out: list[Path] = []

    def _save(fig, name: str) -> None:
        path = out_dir / name
        fig.savefig(str(path))
        out.append(path)

    with rc_context(_pub_rc()):
        try:
            out_dir.mkdir(parents=True, exist_ok=True)

            # Figure 1: recovered vs expected S/N against the 1:1 line. A miss
            # is the same DM marker, hollow, parked at recovered S/N = 0.
            fig = Figure(figsize=(6.3, 5.2))
            ax = fig.add_subplot(111)
            targets = [t for t in (_f(r, "target_snr") for r in rows)
                       if t is not None]
            recs = [s for s in (_f(r, "rec_snr") for r in rows) if s is not None]
            lo = min(targets) - 2 if targets else 10.0
            hi = (max(targets + recs) + 2) if (targets or recs) else 30.0
            ax.plot([lo, hi], [lo, hi], color=COLOR_NEUTRAL, lw=0.9,
                    ls=(0, (5, 3)), zorder=1)
            lbl = lo + 0.93 * (hi - lo)
            ax.annotate("1:1", (lbl, lbl), xytext=(5, -6),
                        textcoords="offset points", fontsize=9.5,
                        color=COLOR_NEUTRAL, ha="left", va="top")
            plotted = 0
            for r in rows:
                t = _f(r, "target_snr")
                if t is None:
                    continue
                plotted += 1
                _lab, fill, marker, edge = dm_bucket(_f(r, "dm"))
                s = _f(r, "rec_snr")
                if s is not None and _g(r, "outcome") == oc.RECOVERED:
                    ax.scatter([t], [s], s=70, marker=marker, facecolor=fill,
                               edgecolor=edge, linewidth=0.9, alpha=0.9,
                               zorder=3)
                else:
                    ax.scatter([t], [0.0], s=70, marker=marker,
                               facecolor="none", edgecolor=edge,
                               linewidth=1.4, zorder=3)
            if not plotted:
                # Shots taken before the solver recorded its target have no
                # x coordinate. Say so rather than show a blank panel.
                ax.annotate("no shots with a recorded expected S/N",
                            (0.5, 0.5), xycoords="axes fraction", ha="center",
                            va="center", fontsize=11, color=COLOR_NEUTRAL)
            ax.set_xlim(lo, hi)
            ax.set_ylim(-1.0, hi)
            ax.set_xlabel("expected S/N")
            ax.set_ylabel("recovered S/N")
            ax.set_title("Injection recovery", fontsize=11.5, color=_INK,
                         loc="left", pad=10)
            fig.legend(handles=_dm_legend_handles(Line2D, True),
                       loc="outside center right", ncol=1,
                       handletextpad=0.3, labelspacing=0.6, fontsize=9.5)
            _despine(ax)
            _save(fig, "snr_recovery.png")

            # Figure 2: outcome counts, horizontal, direct-labeled.
            fig = Figure(figsize=(6.2, 3.2))
            ax = fig.add_subplot(111)
            counts = {o: 0 for o in oc.ALL}
            for r in rows:
                o = str(_g(r, "outcome") or "")
                if o in counts:
                    counts[o] += 1
            labels = [o for o in oc.ALL if counts[o] > 0] or [oc.RECOVERED]
            labels = labels[::-1]          # recovered on top
            vals = [counts[o] for o in labels]
            bars = ax.barh(range(len(labels)), vals,
                           color=[OUTCOME_COLORS[o] for o in labels],
                           height=0.6, zorder=3)
            for rect, v in zip(bars, vals):
                ax.annotate(f" {v}", (v, rect.get_y() + rect.get_height() / 2),
                            ha="left", va="center", fontsize=11, color=_INK)
            ax.set_yticks(range(len(labels)))
            ax.set_yticklabels([o.replace("_", " ") for o in labels])
            ax.set_xlim(0, max(vals) * 1.15 if max(vals) else 1)
            ax.xaxis.set_visible(False)
            ax.minorticks_off()
            ax.tick_params(axis="y", length=0)
            for side in ("top", "right", "bottom"):
                ax.spines[side].set_visible(False)
            ax.spines["left"].set_color(_INK)
            ax.tick_params(colors=_INK, labelsize=10.5)
            ax.set_title("Outcomes", fontsize=11.5, color=_INK, loc="left",
                         pad=10)
            _save(fig, "outcomes.png")

            # Figure 3: DM error against the recovered width, as the FWHM of
            # hella's smoothing kernel for the matched trial. A wider kernel
            # smears the pulse, so DM error growing with width is the expected
            # shape; anything else is a search-grid problem.
            fig = Figure(figsize=(6.6, 4.9))
            ax = fig.add_subplot(111)
            for r in rows:
                rdm, dm, ibox = _f(r, "rec_dm"), _f(r, "dm"), _g(r, "rec_width")
                if rdm is None or dm is None or ibox is None:
                    continue
                _lab, fill, marker, edge = dm_bucket(dm)
                ax.scatter([hella_kernel.kernel_fwhm_ms(int(ibox))], [rdm - dm],
                           s=70, marker=marker, facecolor=fill, edgecolor=edge,
                           linewidth=0.9, alpha=0.9, zorder=3)
            ax.axhline(0.0, color=COLOR_NEUTRAL, lw=0.8, alpha=0.7, zorder=1)
            ax.set_xscale("log")
            ax.set_xlabel("recovered width, kernel FWHM [ms]")
            ax.set_ylabel("DM error (recovered $-$ injected) [pc cm$^{-3}$]")
            ax.set_title("DM accuracy against recovered width", fontsize=11.5,
                         color=_INK, loc="left", pad=10)
            fig.legend(handles=_dm_legend_handles(Line2D, False),
                       loc="outside center right", ncol=1,
                       handletextpad=0.3, labelspacing=0.6, fontsize=9.5)
            _despine(ax)
            _save(fig, "dm_accuracy.png")
        except Exception as exc:  # noqa: BLE001
            logger.warning("summary figure render failed: %s", exc,
                           exc_info=True)
    return out


def render_card(text: str, color: str, out_png) -> Path:
    """A PNG of one message, with the attachment colour bar down the left.

    This is only for offline review (imgcat in a terminal); Slack renders
    the real thing from the text and the attachment colour.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib import rc_context
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace(NBSP, " ")
    nlines = sum(max(1, len(line) // 96 + 1) for line in body.split("\n"))
    with rc_context({"font.family": "monospace", "font.size": 10.0,
                     "savefig.dpi": 160, "savefig.facecolor": "white"}):
        fig = Figure(figsize=(9.0, max(1.0, 0.32 * nlines + 0.5)))
        ax = fig.add_subplot(111)
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.add_patch(Rectangle((0.0, 0.0), 0.012, 1.0,
                               transform=ax.transAxes, color=color,
                               clip_on=False))
        ax.text(0.03, 0.95, body, va="top", ha="left", fontsize=10,
                color=_INK, wrap=True, transform=ax.transAxes)
        fig.savefig(str(out_png), bbox_inches="tight")
    return out_png


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _read_first_word(path: Path) -> str | None:
    try:
        return path.read_text().split()[0]
    except (OSError, IndexError):
        return None


def load_channel() -> str | None:
    """Injection channel: the override dotfile if present, else the shared one."""
    return _read_first_word(CHANNEL_OVERRIDE_PATH) or _read_first_word(CHANNEL_PATH)


class SlackPoster:
    """Thin Slack transport for the injection bot. Never raises.

    `enabled=False` (the default, and what t2d.yaml ships) makes every method
    a no-op returning None. `dry_run_dir` set makes every method write the
    would-be message to a .txt in that directory and touch no socket at all.
    """

    def __init__(self, enabled: bool = False, dry_run_dir=None,
                 channel: str | None = None, streak_every: int = 5,
                 icfg: dict | None = None):
        self.enabled = bool(enabled)
        self.dry_run_dir = Path(dry_run_dir) if dry_run_dir else None
        self.dry_run = self.dry_run_dir is not None
        self.channel = channel
        self.streak_every = int(streak_every)
        # the `injection` config block, so the expected S/N in a sent message
        # uses the same rec_per_true table the solver used
        self.icfg = icfg
        self._seq = 0

    # ----- dry run ---------------------------------------------------------

    def _dry_write(self, name: str, text: str) -> str:
        self.dry_run_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        path = self.dry_run_dir / f"{self._seq:03d}_{name}.txt"
        path.write_text(text + "\n")
        logger.info("slack dry run: wrote %s", path)
        return f"dry-{self._seq:03d}"

    # ----- raw api ---------------------------------------------------------

    def _auth(self):
        token = _read_first_word(TOKEN_PATH)
        channel = self.channel or load_channel()
        if token is None or channel is None:
            logger.warning("slack unconfigured (token %s, channel %s); "
                           "injection message dropped", TOKEN_PATH, CHANNEL_PATH)
            return None, None
        return token, channel

    def _post(self, text: str, attachments=None, thread_ts=None) -> str | None:
        """chat.postMessage; returns the message ts, or None on any failure."""
        token, channel = self._auth()
        if token is None:
            return None
        payload = {"channel": channel, "text": text}
        if attachments:
            payload["attachments"] = attachments
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            import requests
            r = requests.post(f"{_SLACK_API}/chat.postMessage",
                              headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=_TIMEOUT_S)
            r.raise_for_status()
            doc = r.json()
            if not doc.get("ok"):
                logger.warning("slack chat.postMessage failed: %s",
                               doc.get("error"))
                return None
            return doc.get("ts")
        except Exception as exc:  # noqa: BLE001 - alerting is best-effort
            logger.warning("slack post failed: %s", exc)
            return None

    def _update(self, ts: str, text: str, attachments=None) -> str | None:
        token, channel = self._auth()
        if token is None:
            return None
        payload = {"channel": channel, "ts": ts, "text": text}
        if attachments:
            payload["attachments"] = attachments
        try:
            import requests
            r = requests.post(f"{_SLACK_API}/chat.update",
                              headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=_TIMEOUT_S)
            r.raise_for_status()
            doc = r.json()
            if not doc.get("ok"):
                logger.warning("slack chat.update failed: %s", doc.get("error"))
                return None
            return doc.get("ts")
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack update failed: %s", exc)
            return None

    def _post_file(self, png: Path, title: str, thread_ts=None) -> bool:
        """External-upload flow, same three steps as casm_t3.alerts."""
        token, channel = self._auth()
        if token is None:
            return False
        png = Path(png)
        if not png.is_file():
            logger.warning("slack post: no such figure %s", png)
            return False
        try:
            import requests
            auth = {"Authorization": f"Bearer {token}"}
            r1 = requests.get(f"{_SLACK_API}/files.getUploadURLExternal",
                              headers=auth,
                              params={"filename": png.name,
                                      "length": png.stat().st_size},
                              timeout=_TIMEOUT_S)
            r1.raise_for_status()
            d1 = r1.json()
            if not d1.get("ok"):
                logger.warning("slack getUploadURLExternal failed: %s",
                               d1.get("error"))
                return False
            with png.open("rb") as fh:
                r2 = requests.post(d1["upload_url"], files={"file": fh},
                                   timeout=_TIMEOUT_S)
            r2.raise_for_status()
            body = {"files": [{"id": d1["file_id"], "title": title}],
                    "channel_id": channel}
            if thread_ts:
                body["thread_ts"] = thread_ts
            r3 = requests.post(f"{_SLACK_API}/files.completeUploadExternal",
                               headers=auth, json=body, timeout=_TIMEOUT_S)
            r3.raise_for_status()
            d3 = r3.json()
            if not d3.get("ok"):
                logger.warning("slack completeUploadExternal failed: %s",
                               d3.get("error"))
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack file upload failed: %s", exc)
            return False

    # ----- public ----------------------------------------------------------

    def post_sent(self, row) -> str | None:
        """Post the "injection sent" message; returns its ts for the ledger."""
        if not self.enabled:
            return None
        text = sent_text(row, self.icfg)
        if self.dry_run:
            return self._dry_write(f"sent_{_g(row, 'id', 'x')}", text)
        return self._post(text)

    def post_outcome(self, row) -> str | None:
        """Resolve the shot: edit the sent message in place, else post fresh."""
        if not self.enabled:
            return None
        line = outcome_text(row)
        color = outcome_color(row)
        if self.dry_run:
            return self._dry_write(f"outcome_{_g(row, 'id', 'x')}",
                                   f"[{color}] {line}")
        attachment = {"color": color, "text": line, "fallback": line}
        ts = _g(row, "slack_ts")
        if ts:
            got = self._update(ts, sent_text(row, self.icfg),
                               attachments=[attachment])
            if got:
                return got
            logger.warning("slack edit of injection %s failed; posting fresh",
                           _g(row, "id"))
        return self._post(f"`{_g(row, 'id')}`: {line}", attachments=[attachment])

    def post_streak(self, n: int, ids, why: str | None) -> str | None:
        if not self.enabled:
            return None
        text = streak_text(n, ids, why)
        if self.dry_run:
            return self._dry_write(f"streak_{n}", text)
        return self._post(text, attachments=[{"color": COLOR_MISSED,
                                              "text": "", "fallback": text}])

    def post_summary(self, rows, day: str, fig_dir) -> str | None:
        """Summary text plus the three figures threaded under it."""
        if not self.enabled:
            return None
        text = summary_text(rows, day)
        figures = render_summary_figures(rows, fig_dir)
        if not figures:
            text += "\n(summary figures failed to render - see the log)"
        if self.dry_run:
            return self._dry_write(f"summary_{day}", text)
        ts = self._post(text)
        for path in figures:
            self._post_file(path, path.stem, thread_ts=ts)
        return ts


# ---------------------------------------------------------------------------
# streak bookkeeping, read straight off the ledger
# ---------------------------------------------------------------------------

def miss_streak(conn, before_id: int | None = None) -> tuple[int, list[int], str | None]:
    """Length of the current run of consecutive misses, newest first.

    Read from the ledger rather than a state file: the DB already is the
    state, so a daemon restart cannot double-count or reset a streak.
    Returns (n, ids newest-first capped at 10, latest outcome).
    """
    sql = ("SELECT id, outcome FROM injections WHERE outcome IS NOT NULL"
           + (" AND id <= ?" if before_id else "")
           + " ORDER BY id DESC LIMIT 50")
    rows = conn.execute(sql, (before_id,) if before_id else ()).fetchall()
    ids: list[int] = []
    latest = None
    for rid, outcome in rows:
        if outcome not in oc.MISSES:
            break
        if latest is None:
            latest = outcome
        ids.append(int(rid))
    return len(ids), ids[:10], latest


def check_streak(conn, poster: SlackPoster, before_id: int | None = None) -> int:
    """Post one attention message at each multiple of `streak_every`."""
    every = poster.streak_every
    n, ids, latest = miss_streak(conn, before_id)
    if every > 0 and n > 0 and n % every == 0:
        poster.post_streak(n, ids, latest)
    return n


def poster_from_cfg(icfg: dict) -> SlackPoster:
    """Build the poster from the `injection.slack` config block (ships off)."""
    scfg = (icfg or {}).get("slack") or {}
    return SlackPoster(enabled=bool(scfg.get("enabled", False)),
                       dry_run_dir=scfg.get("dry_run_dir"),
                       channel=scfg.get("channel"),
                       streak_every=int(scfg.get("streak_every", 5)),
                       icfg=icfg)


def utc_day(when: datetime | None = None) -> str:
    from datetime import timezone
    return f"{when or datetime.now(timezone.utc):%Y-%m-%d}"
