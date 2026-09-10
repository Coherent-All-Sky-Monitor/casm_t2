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

#: Caption when the plot has to hang under the card instead of inside it.
_INK = "#262626"

#: Marker identity per DM bin, in bin order. Identity rides on colour AND
#: marker shape, so the figures survive greyscale printing and colour-blind
#: readers. Bin EDGES come from `injection.summary_dm_bins`; these are just
#: the styles they are drawn with. There are six, which covers the default
#: five edges (four bins plus the two open ends) without repeating; a longer
#: bin list cycles, and two bins then share a style.
DM_STYLES = [
    ("#4C6EF5", "o", "#364FC7"),
    ("#F59F00", "s", "#E67700"),
    ("#12B886", "^", "#087F5B"),
    ("#BE4BDB", "D", "#9C36B5"),
    ("#E8590C", "v", "#D9480F"),
    ("#0CA678", "P", "#087F5B"),
]

#: Default bin edges, matching the shipped `injection.sample.dm` range.
DEFAULT_DM_BINS = [100.0, 300.0, 500.0, 700.0, 900.0]


def dm_bins(icfg: dict | None = None) -> list[float]:
    """Bin edges for the per-DM tally, from config or the default."""
    bins = ((icfg or {}).get("summary_dm_bins") or DEFAULT_DM_BINS)
    return [float(b) for b in bins]


def dm_bin_labels(icfg: dict | None = None) -> list[str]:
    """One label per bin, plus the two open-ended ends."""
    edges = dm_bins(icfg)
    labels = [f"DM < {edges[0]:.0f}"]
    labels += [f"DM {lo:.0f}-{hi:.0f}"
               for lo, hi in zip(edges[:-1], edges[1:])]
    labels.append(f"DM > {edges[-1]:.0f}")
    return labels




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


def dm_bucket(dm: float | None, icfg: dict | None = None):
    """(label, fill, marker, edge) for a DM, never None.

    Bins are half-open [lo, hi), with an under- and an over-flow bin so a DM
    outside the sampled range still lands somewhere rather than vanishing.
    """
    edges = dm_bins(icfg)
    labels = dm_bin_labels(icfg)
    idx = len(edges)                       # overflow unless a bin claims it
    if dm is not None:
        value = float(dm)
        if value < edges[0]:
            idx = 0
        else:
            for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
                if lo <= value < hi:
                    idx = i + 1
                    break
    style = DM_STYLES[idx % len(DM_STYLES)]
    return (labels[idx], *style)


# ---------------------------------------------------------------------------
# message text
# ---------------------------------------------------------------------------

def display_id(row) -> str:
    """The name a person reads for a shot: `file_id`, else the ledger id."""
    return str(_g(row, "file_id") or _g(row, "id", "?"))


def display_id_md(row) -> str:
    """`display_id`, backticked for Slack mrkdwn so it renders as inline code.

    The single formatter for the id in Slack TEXT: every message body, bar,
    caption, and fallback goes through this (not the plot title - that is a
    matplotlib figure in casm_t3 - and not log lines or the ledger, which
    stay bare so they still `grep` and compare on the plain id).
    """
    return f"`{display_id(row)}`"


def injected_fwhm_ms(row) -> float | None:
    """The injected width as FWHM. The ledger stores the Gaussian sigma."""
    sigma = _f(row, "sigma_ms")
    return None if sigma is None else sigma * FWHM_PER_SIGMA


def injected_snr(row) -> float | None:
    """The S/N of the pulse that was actually written into the stream.

    `inject_snr` is the value the amplitude solver aimed at from the live
    noise; it is what the card prints (Vishnu, 2026-09-10: the generator's
    own `est_snr` reads a different noise sample and disagreed by up to 2x
    on the live cards). `est_snr` stands in for rows without a solver value.
    """
    inj = _f(row, "inject_snr")
    return inj if inj is not None else _f(row, "est_snr")


def sent_text(row, icfg: dict | None = None) -> str:
    """The message posted the moment the injection hits the FIFO.

    Deliberately short: id, where it went, and what was put there. Non-
    breaking spaces join every number to its unit so Slack's wrapping can
    never split "4.7 ms" across two lines. `icfg` is accepted and unused;
    the line no longer depends on any calibration table.
    """
    fwhm = injected_fwhm_ms(row)
    snr = injected_snr(row)
    bits = [
        f"beam {_g(row, 'beam', '?')}",
        f"DM {_f(row, 'dm') or 0:.0f}",
        f"FWHM {fwhm:.1f}{NBSP}ms" if fwhm is not None else "FWHM n/a",
        f"injected S/N {snr:.0f}" if snr is not None else "injected S/N n/a",
    ]
    # The standing state is subtraction ON, so it adds nothing to the line;
    # only the unusual state is called out.
    suffix = " (IB sub off)" if _g(row, "sub_incoh") == 0 else ""
    return (f"injection {display_id_md(row)} sent: " + ", ".join(bits) + suffix
            + "\n_awaiting recovery..._")


DEFAULT_WEB_BASE = "http://127.0.0.1:8050"


def recovered_beam_text(row) -> str:
    """Which beam it came back in, and how far that is from where it went.

    Landing in the injected beam is the ordinary case and needs no number,
    so it reads as a bare "beam 150". A different beam carries the sky
    separation of the two pointings, which is the quantity that matters -
    neighbouring beam indices are not a fixed angle apart.
    """
    rec_beam = _g(row, "rec_beam")
    if rec_beam is None:
        return "beam n/a"
    rec_beam = int(rec_beam)
    inj_beam = _g(row, "beam")
    if inj_beam is not None and int(inj_beam) == rec_beam:
        return f"beam {rec_beam}"
    offset = _f(row, "rec_offset_arcsec")
    if offset is None:
        return f"beam {rec_beam} (offset n/a)"
    return f"beam {rec_beam} (offset {offset:.0f}{NBSP}arcsec)"


def outcome_text(row, web_base: str = DEFAULT_WEB_BASE) -> str:
    """One line describing how the shot resolved.

    The recovered width is the FWHM of hella's smoothing kernel for the
    matched trial, not 2**ibox samples: the kernel is about 0.67 of the
    trial label wide, so the raw label overstates the pulse by half. It sits
    last so it is easy to drop.
    """
    outcome = _g(row, "outcome")
    if outcome == oc.FIRE_FAILED:
        # Not a pipeline miss: the pulse never reached the stream.
        return "injection not fired: " + oc.short_fire_reason(
            _g(row, "fail_reason"))
    if outcome != oc.RECOVERED:
        return "NOT recovered: " + oc.detail_or_explain(
            _g(row, "fail_reason"), outcome)

    rec_snr = _f(row, "rec_snr")
    rec_dm = _f(row, "rec_dm")
    dm = _f(row, "dm")
    inj = injected_snr(row)
    ibox = _g(row, "rec_width")

    snr_bit = "SNR n/a"
    if rec_snr is not None:
        ratio = f" (ratio {rec_snr / inj:.2f})" if inj else ""
        snr_bit = f"SNR {rec_snr:.1f}{ratio}"
    dm_bit = "DM n/a"
    if rec_dm is not None:
        delta = f" (delta {rec_dm - dm:+.1f})" if dm is not None else ""
        dm_bit = f"DM {rec_dm:.1f}{delta}"
    bits = [f"recovered -> {snr_bit}", dm_bit, recovered_beam_text(row)]
    if ibox is not None:
        bits.append(f"width {hella_kernel.kernel_fwhm_ms(int(ibox)):.1f}"
                    f"{NBSP}ms (ibox {int(ibox)})")
    return " | ".join(bits)


def injection_text(row, web_base: str = DEFAULT_WEB_BASE,
                   icfg: dict | None = None) -> str:
    """The whole shot in two lines: what went in, and what came back.

    This is the caption of the single message per injection. It carries no
    "awaiting recovery" line, because by the time it posts there is nothing
    left to await.
    """
    sent = sent_text(row, icfg).split("\n")[0]
    return sent + "\n" + outcome_text(row, web_base)


def injection_blocks(row, file_id=None, icfg=None) -> list[dict]:
    """Top-level blocks: the sent line, then the plot if there is one.

    The image is a block on the MESSAGE, not nested in the attachment: Slack
    refused blocks inside a coloured attachment on chat.update
    (invalid_attachments, shot 669). `file_id` is a file the bot owns and has
    not shared to any channel, referenced by id.
    """
    sent = sent_text(row, icfg).split("\n")[0]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": sent}}]
    if file_id:
        blocks.append({"type": "image",
                       "slack_file": {"id": file_id},
                       "alt_text": f"injection {display_id(row)}"})
    return blocks


def injection_attachments(row, web_base: str = DEFAULT_WEB_BASE) -> list[dict]:
    """The coloured bar: the outcome line, and nothing nested inside it."""
    line = outcome_text(row, web_base)
    return [{"color": outcome_color(row), "text": line, "fallback": line}]


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


def summary_text(rows, day: str, icfg: dict | None = None) -> str:
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
        label = dm_bucket(_f(r, "dm"), icfg)[0]
        got, tot = by_bucket.setdefault(label, [0, 0])
        by_bucket[label] = [got + (1 if _g(r, "outcome") == oc.RECOVERED else 0),
                            tot + 1]
    if by_bucket:
        order = dm_bin_labels(icfg)
        lines.append("per DM: " + " | ".join(
            f"{label}: {by_bucket[label][0]}/{by_bucket[label][1]}"
            for label in order if label in by_bucket))

    misses = [(k, counts[k]) for k in oc.MISSES if counts[k]]
    if misses:
        lines.append("missed: " + "; ".join(
            f"{v} {oc.label(k)}" for k, v in misses))
    if counts[oc.FIRE_FAILED]:
        lines.append(f"{counts[oc.FIRE_FAILED]} fire failures "
                     "(injector plumbing, not a pipeline miss)")

    def _ratios(subset):
        return sorted(
            (_f(r, "rec_snr") / injected_snr(r)) for r in subset
            if _g(r, "outcome") == oc.RECOVERED and _f(r, "rec_snr") is not None
            and injected_snr(r))

    def _ratio_line(subset, label=""):
        vals = _ratios(subset)
        if not vals:
            return None
        med = vals[len(vals) // 2]
        return (f"recovered/injected S/N{label}: median {med:.2f} "
                f"(range {vals[0]:.2f}-{vals[-1]:.2f})")

    # The reported/true ratio differs between subtraction states, so a day
    # that mixes them must not be averaged into one meaningless number.
    states = {_g(r, "sub_incoh") for r in rows}
    if len({s for s in states if s is not None}) > 1:
        for state, label in ((1, " (IB sub on)"), (0, " (IB sub off)")):
            line = _ratio_line([r for r in rows
                                if _g(r, "sub_incoh") == state], label)
            if line:
                lines.append(line)
        unknown = _ratio_line([r for r in rows if _g(r, "sub_incoh") is None],
                              " (IB sub unknown)")
        if unknown:
            lines.append(unknown)
    else:
        line = _ratio_line(rows)
        if line:
            lines.append(line)
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


def _dm_legend_handles(line2d, with_miss: bool,
                       icfg: dict | None = None) -> list:
    handles = []
    for i, label in enumerate(dm_bin_labels(icfg)):
        c, m, e = DM_STYLES[i % len(DM_STYLES)]
        handles.append(line2d([], [], marker=m, color=c, ls="none", ms=8,
                              mec=e, mew=0.9, label=label))
    if with_miss:
        handles.append(line2d([], [], marker="o", mfc="none", mec=_INK,
                              ls="none", ms=8, mew=1.1, label="open = missed"))
    return handles


def render_summary_figures(rows, out_dir,
                           icfg: dict | None = None) -> list[Path]:  # noqa: C901
    """The daily summary figure; returns the paths written.

    One figure: injected against recovered S/N. The outcome counts are in
    the summary text, where they read as well and cost no attention.

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

            # Figure 1: recovered vs injected S/N. The 1:1 line is the
            # reference; the dashed fit through the origin is what hella
            # actually does, and its slope is the reported/injected ratio
            # averaged over the day. A miss is the same DM marker, hollow,
            # parked at recovered S/N = 0.
            fig = Figure(figsize=(6.3, 5.2))
            ax = fig.add_subplot(111)
            injected = [v for v in (injected_snr(r) for r in rows)
                        if v is not None]
            recs = [s for s in (_f(r, "rec_snr") for r in rows) if s is not None]
            lo = min(injected) - 2 if injected else 10.0
            hi = (max(injected + recs) + 2) if (injected or recs) else 30.0
            lo = min(lo, 0.0) if not injected else lo
            ax.plot([lo, hi], [lo, hi], color=COLOR_NEUTRAL, lw=0.9,
                    ls=(0, (5, 3)), zorder=1)
            lbl = lo + 0.93 * (hi - lo)
            ax.annotate("1:1", (lbl, lbl), xytext=(5, -6),
                        textcoords="offset points", fontsize=9.5,
                        color=COLOR_NEUTRAL, ha="left", va="top")
            plotted = 0
            for r in rows:
                t = injected_snr(r)
                if t is None:
                    continue
                plotted += 1
                _lab, fill, marker, edge = dm_bucket(_f(r, "dm"), icfg)
                s_rec = _f(r, "rec_snr")
                if s_rec is not None and _g(r, "outcome") == oc.RECOVERED:
                    ax.scatter([t], [s_rec], s=70, marker=marker,
                               facecolor=fill, edgecolor=edge, linewidth=0.9,
                               alpha=0.9, zorder=3)
                else:
                    ax.scatter([t], [0.0], s=70, marker=marker,
                               facecolor="none", edgecolor=edge,
                               linewidth=1.4, zorder=3)
            if not plotted:
                # Rows with neither est_snr nor inject_snr have no x
                # coordinate. Say so rather than show a blank panel.
                ax.annotate("no shots with a recorded injected S/N",
                            (0.5, 0.5), xycoords="axes fraction", ha="center",
                            va="center", fontsize=11, color=COLOR_NEUTRAL)
            ax.set_xlim(lo, hi)
            ax.set_ylim(-1.0, hi)
            ax.set_xlabel("injected S/N")
            ax.set_ylabel("recovered S/N")
            fig.legend(handles=_dm_legend_handles(Line2D, True, icfg),
                       loc="outside center right", ncol=1,
                       handletextpad=0.3, labelspacing=0.6, fontsize=9.5)
            _despine(ax)
            _save(fig, "snr_recovery.png")


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


#: How a shot reaches the channel.
#:
#: `sent_then_update` (the default) posts the sent line the moment the pulse
#: goes in, so the channel shows a shot is in flight, and then COMPLETES that
#: same message ~100 s later: the coloured bar carrying the outcome line and
#: the replay plot inline. One message per shot, and it says something useful
#: from the first second.
#:
#: `single` posts nothing at fire time and ONE message per
#: injection once everything is known: the replay plot, captioned with the
#: sent line and the outcome line. One shot, one message, and the picture is
#: there the first time anyone looks at it.
#:
#: `sent_then_edit` is the older two-step shape: a "sent" message the moment
#: the pulse goes in, edited in place with a coloured outcome bar ~100 s
#: later, and the replay threaded under it. It shows a shot is in flight
#: before the result exists, which is worth having while the bot is new.
MODE_SENT_THEN_UPDATE = "sent_then_update"
MODE_SINGLE = "single"
MODE_SENT_THEN_EDIT = "sent_then_edit"
MODES = (MODE_SENT_THEN_UPDATE, MODE_SINGLE, MODE_SENT_THEN_EDIT)


class SlackPoster:
    """Thin Slack transport for the injection bot. Never raises.

    `enabled=False` (the default, and what t2d.yaml ships) makes every method
    a no-op returning None. `dry_run_dir` set makes every method write the
    would-be message to a .txt in that directory and touch no socket at all.
    """

    def __init__(self, enabled: bool = False, dry_run_dir=None,
                 channel: str | None = None, streak_every: int = 5,
                 icfg: dict | None = None,
                 web_base: str = DEFAULT_WEB_BASE,
                 mode: str = MODE_SENT_THEN_UPDATE):
        self.enabled = bool(enabled)
        self.dry_run_dir = Path(dry_run_dir) if dry_run_dir else None
        self.dry_run = self.dry_run_dir is not None
        self.channel = channel
        self.streak_every = int(streak_every)
        # the `injection` config block, so the expected S/N in a sent message
        # uses the same rec_per_true table the solver used
        self.icfg = icfg
        # base URL of the t3 web app, for the link in the recovered line
        self.web_base = web_base or DEFAULT_WEB_BASE
        if mode not in MODES:
            logger.warning("slack mode %r is not one of %s; using %s",
                           mode, MODES, MODE_SENT_THEN_UPDATE)
            mode = MODE_SENT_THEN_UPDATE
        self.mode = mode
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

    def _post(self, text: str, attachments=None, thread_ts=None, blocks=None,
              want_error: bool = False):
        """chat.postMessage; returns the message ts, or None on any failure.

        With `want_error` it returns (ts, error) instead, so a caller can
        tell a rejected block payload from a dead network and fall back to
        something the workspace will accept.
        """
        def out(ts, err):
            return (ts, err) if want_error else ts

        token, channel = self._auth()
        if token is None:
            return out(None, "unconfigured")
        payload = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
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
                return out(None, str(doc.get("error")))
            return out(doc.get("ts"), None)
        except Exception as exc:  # noqa: BLE001 - alerting is best-effort
            logger.warning("slack post failed: %s", exc)
            return out(None, str(exc))

    def _update(self, ts: str, text: str, attachments=None, blocks=None,
                want_error: bool = False):
        """chat.update; returns the ts, or (ts, error) with `want_error`."""
        def out(got, err):
            return (got, err) if want_error else got

        token, channel = self._auth()
        if token is None:
            return out(None, "unconfigured")
        payload = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
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
                return out(None, str(doc.get("error")))
            return out(doc.get("ts"), None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack update failed: %s", exc)
            return out(None, str(exc))

    def _share_ts(self, file_id: str, channel: str, auth: dict,
                  timeout_s: float = 10.0) -> str | None:
        """Message ts of the uploaded file's share into `channel`.

        Slack materialises the upload -> message share asynchronously, so
        poll files.info briefly. Needs the files:read scope; returns None on
        timeout or without it, which only costs later editability - the post
        itself already succeeded.
        """
        import time as _time
        import requests
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            try:
                d = requests.get(f"{_SLACK_API}/files.info", headers=auth,
                                 params={"file": file_id},
                                 timeout=_TIMEOUT_S).json()
            except Exception:  # noqa: BLE001
                return None
            if d.get("ok"):
                shares = (d.get("file") or {}).get("shares") or {}
                for vis in ("public", "private"):
                    entries = (shares.get(vis) or {}).get(channel)
                    if entries and entries[0].get("ts"):
                        return entries[0]["ts"]
            elif d.get("error") == "missing_scope":
                return None
            _time.sleep(1.0)
        return None

    def _upload_unshared(self, png: Path, title: str) -> str | None:
        """Upload a file the bot owns, shared to no channel; returns its id.

        The two-step external upload, completed WITHOUT `channel_id`. The
        file then exists but appears nowhere, which is what lets a block
        reference render it inside an attachment instead of as its own
        message with the picture hanging beneath.
        """
        token, _channel = self._auth()
        if token is None:
            return None
        png = Path(png)
        if not png.is_file():
            logger.warning("slack upload: no such file %s", png)
            return None
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
                return None
            with png.open("rb") as fh:
                r2 = requests.post(d1["upload_url"], files={"file": fh},
                                   timeout=_TIMEOUT_S)
            r2.raise_for_status()
            r3 = requests.post(
                f"{_SLACK_API}/files.completeUploadExternal", headers=auth,
                json={"files": [{"id": d1["file_id"], "title": title}]},
                timeout=_TIMEOUT_S)
            r3.raise_for_status()
            d3 = r3.json()
            if not d3.get("ok"):
                logger.warning("slack completeUploadExternal failed: %s",
                               d3.get("error"))
                return None
            return d1["file_id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack unshared upload failed: %s", exc)
            return None

    def _post_file(self, png: Path, title: str, thread_ts=None,
                   comment: str | None = None, want_ts: bool = False):
        """External-upload flow, same three steps as casm_t3.alerts.

        Returns True/False normally; with `want_ts` it returns the share's
        message ts (or None), which single mode stores as the shot's
        `slack_ts`.
        """
        fail = None if want_ts else False
        token, channel = self._auth()
        if token is None:
            return fail
        png = Path(png)
        if not png.is_file():
            logger.warning("slack post: no such figure %s", png)
            return fail
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
                return fail
            with png.open("rb") as fh:
                r2 = requests.post(d1["upload_url"], files={"file": fh},
                                   timeout=_TIMEOUT_S)
            r2.raise_for_status()
            body = {"files": [{"id": d1["file_id"], "title": title}],
                    "channel_id": channel}
            if comment:
                body["initial_comment"] = comment
            if thread_ts:
                body["thread_ts"] = thread_ts
            r3 = requests.post(f"{_SLACK_API}/files.completeUploadExternal",
                               headers=auth, json=body, timeout=_TIMEOUT_S)
            r3.raise_for_status()
            d3 = r3.json()
            if not d3.get("ok"):
                logger.warning("slack completeUploadExternal failed: %s",
                               d3.get("error"))
                return fail
            if want_ts:
                return self._share_ts(d1["file_id"], channel, auth)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack file upload failed: %s", exc)
            return fail

    # ----- public ----------------------------------------------------------

    def post_sent(self, row) -> str | None:
        """Post the "injection sent" message; returns its ts for the ledger.

        Nothing at all in `single` mode, which posts one finished message
        later. In the other two this is the message the channel sees at fire
        time, and it is the one that later gets completed or edited.
        """
        if not self.enabled or self.mode == MODE_SINGLE:
            return None
        text = sent_text(row, self.icfg)
        if self.dry_run:
            import json
            body = json.dumps({"text": text}, indent=2)
            return self._dry_write(f"inject_{display_id(row)}_sent", body)
        return self._post(text)

    def post_outcome(self, row) -> str | None:
        """Resolve the shot: edit the sent message in place, else post fresh.

        Not used in `single` mode - `post_injection` carries the outcome.
        """
        if not self.enabled or self.mode == MODE_SINGLE:
            return None
        line = outcome_text(row, self.web_base)
        color = outcome_color(row)
        if self.dry_run:
            return self._dry_write(f"outcome_{display_id(row)}",
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
        return self._post(f"{display_id_md(row)}: {line}", attachments=[attachment])

    def post_injection(self, row, png=None) -> str | None:
        """Complete (or post) the shot's message; returns its ts.

        The sent line is the message text and its first block; the plot, if
        there is one, is a top-level image block beside it; one coloured
        attachment carries the outcome. The image never goes in a thread and
        never carries a caption - the card already says what it is.

        Forms, in descending order of how much lands in one message; the one
        used is logged, so a channel that has quietly degraded says so:

          inline     one message: sent line, plot, coloured bar
          bar+image  the bar on the sent message, the plot as its own
                     top-level message (the workspace refused the blocks)
          bar        the bar alone, no plot to show
          text       both lines as text, nothing else worked
        """
        if not self.enabled:
            return None
        sent = sent_text(row, self.icfg).split("\n")[0]
        inj_id = display_id(row)
        ts = _g(row, "slack_ts")
        updating = self.mode == MODE_SENT_THEN_UPDATE and bool(ts)

        if self.dry_run:
            import json
            file_id = f"F_DRYRUN_{inj_id}" if png is not None else None
            payload = {"text": sent,
                       "blocks": injection_blocks(row, file_id, self.icfg),
                       "attachments": injection_attachments(row, self.web_base)}
            if updating:
                payload = {"ts": ts, **payload}
            body = json.dumps(payload, indent=2)
            if png is not None:
                body += f"\n\n[uploaded, unshared] {png}"
            name = f"inject_{inj_id}_update" if updating else f"inject_{inj_id}"
            return self._dry_write(name, body)

        file_id = self._upload_unshared(png, inj_id) if png else None
        blocks = injection_blocks(row, file_id, self.icfg)
        attachments = injection_attachments(row, self.web_base)

        if updating:
            got, err = self._update(ts, sent, attachments=attachments,
                                    blocks=blocks, want_error=True)
            if got:
                logger.info("injection %s completed in place (form=%s)",
                            inj_id, "inline" if file_id else "bar")
                return got
            if file_id is not None:
                # The workspace will not take a slack_file image block. Put
                # the outcome on the card anyway and give the plot its own
                # TOP-LEVEL message; never a thread reply.
                logger.warning("injection %s: update with blocks refused (%s);"
                               " posting the plot separately", inj_id, err)
                plain = injection_blocks(row, None, self.icfg)
                got, err2 = self._update(ts, sent, attachments=attachments,
                                         blocks=plain, want_error=True)
                if got:
                    self._post_file(Path(png), inj_id, comment=f"`{inj_id}`")
                    logger.info("injection %s completed (form=bar+image)",
                                inj_id)
                    return got
                err = err2
            logger.warning("injection %s: update failed (%s); posting a new "
                           "message instead", inj_id, err)

        got, err = self._post(sent, attachments=attachments, blocks=blocks,
                              want_error=True)
        if got:
            logger.info("injection %s posted (form=%s)", inj_id,
                        "inline" if file_id else "bar")
            return got
        if file_id is not None:
            logger.warning("injection %s: block post refused (%s); posting "
                           "the bar and the plot separately", inj_id, err)
            got, _ = self._post(sent, attachments=attachments, want_error=True)
            if got:
                self._post_file(Path(png), inj_id, comment=f"`{inj_id}`")
                logger.info("injection %s posted (form=bar+image)", inj_id)
                return got
        logger.warning("injection %s: falling back to plain text", inj_id)
        got = self._post(f"{sent}\n{outcome_text(row, self.web_base)}")
        if got:
            logger.info("injection %s posted (form=text)", inj_id)
        return got


    def post_replay(self, row, png, caption: str) -> bool:
        """Thread the replay plot under this shot's own message.

        A reply, not a new message: the channel keeps one top-level line per
        injection, and the plot sits with the shot it belongs to. Without a
        stored ts there is nothing to reply to, so the plot is skipped rather
        than posted loose.
        """
        if not self.enabled:
            return False
        if self.dry_run:
            self._dry_write(f"replay_{_g(row, 'id', 'x')}",
                            f"[thread_ts={_g(row, 'slack_ts')}] {caption}\n{png}")
            return True
        ts = _g(row, "slack_ts")
        if not ts:
            logger.warning("no slack_ts for injection %s; not posting the "
                           "replay loose in the channel", _g(row, "id"))
            return False
        return self._post_file(Path(png), f"inj{_g(row, 'id')} replay",
                               thread_ts=ts, comment=caption)

    def post_streak(self, n: int, ids, why: str | None) -> str | None:
        if not self.enabled:
            return None
        text = streak_text(n, ids, why)
        if self.dry_run:
            return self._dry_write(f"streak_{n}", text)
        return self._post(text, attachments=[{"color": COLOR_MISSED,
                                              "text": "", "fallback": text}])

    def post_summary(self, rows, day: str, fig_dir) -> str | None:
        """Summary text as one top-level message, the figure as a reply in its thread."""
        if not self.enabled:
            return None
        text = summary_text(rows, day, self.icfg)
        want_figures = bool(((self.icfg or {}).get("slack") or {})
                            .get("summary_figures", False))
        figures = (render_summary_figures(rows, fig_dir, self.icfg)
                   if want_figures else [])
        if want_figures and not figures:
            text += "\n(summary figure failed to render - see the log)"
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
                       icfg=icfg,
                       web_base=scfg.get("web_base") or DEFAULT_WEB_BASE,
                       mode=scfg.get("mode", MODE_SENT_THEN_UPDATE))


def utc_day(when: datetime | None = None) -> str:
    from datetime import timezone
    return f"{when or datetime.now(timezone.utc):%Y-%m-%d}"
