"""Closed enum for how one injection resolved, plus display text.

`fail_reason` in the ledger is free text naming the first failed gate.
`outcome` is the closed set the Slack poster, the streak logic and the summary
figures count over, so a new failure string cannot invent a category.

An outcome records only whether the search saw the pulse:

    a matching cluster, at any S/N        -> recovered
    no cluster, but matching T1 trials    -> missed_t2
    no cluster and no matching T1 trial   -> missed_t1
    the shot never reached the stream     -> fire_failed

Trigger gates are not part of it. Whether a recovered injection would also earn
a dump is policy (tiers, DM floor, beam vetoes, occupancy) and stays recorded
in `gate_trigger`.
"""

from __future__ import annotations

RECOVERED = "recovered"
MISSED_T1 = "missed_t1"
MISSED_T2 = "missed_t2"
FIRE_FAILED = "fire_failed"

#: Outcomes that bear on pipeline sensitivity. `fire_failed` is injector
#: plumbing and is excluded.
MISSES = (MISSED_T1, MISSED_T2)

ALL = (RECOVERED, MISSED_T1, MISSED_T2, FIRE_FAILED)

#: Set by the daemon on a shot that never reached the stream.
FIRE_FAILED_PREFIXES = ("fifo_write_failed", "file_generation_failed")

#: Display labels for the summary's "missed:" line and the outcome bar chart.
#: The enum strings above stay the wire/DB values.
LABELS = {
    RECOVERED: "recovered",
    MISSED_T1: "missed by hella (T1)",
    MISSED_T2: "T2 miss (no cluster formed)",
    FIRE_FAILED: "not fired",
}


def label(outcome: str | None) -> str:
    """Display label for an outcome, falling back to the raw value."""
    return LABELS.get(str(outcome), str(outcome))


#: Fallback phrases, used only when reconcile() left no detail in
#: `fail_reason`. Counting is always over the enum above, never over the text.
EXPLANATIONS = {
    MISSED_T1: "lost at T1: no cluster in the reconcile window",
    MISSED_T2: "lost at T2: matching T1 trials but no cluster formed",
    FIRE_FAILED: "injection not fired",
}

#: `fail_reason` prefixes turned into a few words for the fire_failed line.
FIRE_FAILED_REASONS = {
    "fifo_write_failed": "FIFO write failed",
    "file_generation_failed": "file generation failed",
}

#: reconcile() writes full sentences, printed verbatim. These legacy values
#: predate the detail and are mapped so old rows still read.
LEGACY_REASONS = {
    "t1_no_detection": EXPLANATIONS[MISSED_T1],
}

#: Rows written while `missed_trigger` existed. The search did find those
#: shots, so they are recovered now. Re-reconciling rewrites them.
RETIRED = ("missed_trigger",)


def detail_or_explain(fail_reason: str | None, outcome: str | None) -> str:
    """What to print after "NOT recovered: ".

    Prefers the detail reconcile() wrote, falls back to the enum phrase.
    """
    text = str(fail_reason or "").strip()
    if not text:
        return explain(outcome)
    if text in LEGACY_REASONS:
        return LEGACY_REASONS[text]
    if text.startswith("lost at ") or text.startswith("injection not fired"):
        return text
    return explain(outcome)


def short_fire_reason(fail_reason: str | None) -> str:
    """A few words for why a shot never reached the stream."""
    head = str(fail_reason or "").split(":", 1)[0]
    return FIRE_FAILED_REASONS.get(head, head or "unknown")


def classify(gate_t1, gate_t2, gate_trigger=None,
             fail_reason: str | None = None, n_t1_trials=None) -> str:
    """Outcome for one reconciled injection.

    `gate_trigger` is accepted and ignored, the trigger filters being policy
    rather than detection. A matching cluster (gate_t2) is recovered at any
    S/N; without one, `n_t1_trials` decides where it was lost. `fail_reason` is
    consulted only for fire_failed, which is set before any gate is known.
    """
    if fail_reason and str(fail_reason).startswith(FIRE_FAILED_PREFIXES):
        return FIRE_FAILED
    if gate_t2:
        return RECOVERED
    if n_t1_trials:
        return MISSED_T2
    return MISSED_T1


def explain(outcome: str | None) -> str:
    """Human sentence for a miss; falls back to naming the raw outcome."""
    return EXPLANATIONS.get(str(outcome), f"outcome={outcome}")
