"""Closed enum for how one injection resolved, plus human explanations.

`fail_reason` in the ledger stays what it always was: free text naming the
first failed gate. `outcome` is the small closed set the Slack poster, the
streak logic and the summary figures count over, so a new failure string
never silently invents a new category.

An outcome answers one question only: did the search see the pulse? (see
casm_t2.apps.inject_daemon.reconcile)

    a matching cluster, at ANY S/N        -> recovered
    no cluster, but matching T1 trials    -> missed_t2
    no cluster and no matching T1 trial   -> missed_t1
    the shot never reached the stream     -> fire_failed

The trigger gates are deliberately NOT part of this. Whether a recovered
injection would also have earned a dump is a policy question - tiers, DM
floor, beam vetoes, occupancy - and policy changes week to week. It stays
recorded in `gate_trigger` for anyone who wants it, but a shot the search
found is recovered even at S/N 15.8.
"""

from __future__ import annotations

RECOVERED = "recovered"
MISSED_T1 = "missed_t1"
MISSED_T2 = "missed_t2"
FIRE_FAILED = "fire_failed"

#: Outcomes that say something about pipeline sensitivity. `fire_failed` is
#: injector plumbing and is deliberately not one of them.
MISSES = (MISSED_T1, MISSED_T2)

ALL = (RECOVERED, MISSED_T1, MISSED_T2, FIRE_FAILED)

#: Set by the daemon on a shot that never reached the stream.
FIRE_FAILED_PREFIXES = ("fifo_write_failed", "file_generation_failed")

#: Plain labels for anywhere a human reads the outcome as a category - the
#: summary's "missed:" line and the outcome bar chart. The enum strings stay
#: the wire/DB values; these are only ever display text.
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
#: `fail_reason`. The real message is the detail: it names the stage AND what
#: the evidence at that stage actually was, DSA style. Anything counting over
#: injections still counts over the closed enum above, never over the text.
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

#: Detail strings reconcile() writes are already full sentences, so the
#: poster prints them verbatim. These legacy values are not, and predate the
#: detail; map them so old rows still read sensibly.
LEGACY_REASONS = {
    "t1_no_detection": EXPLANATIONS[MISSED_T1],
}

#: Rows written while `missed_trigger` existed: the shot WAS found by the
#: search, so under the current definition it is recovered. Re-reconciling
#: rewrites them; this only keeps an un-migrated row from reading as a miss.
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

    `gate_trigger` is accepted and ignored: the trigger filters are policy,
    not detection. A matching cluster (gate_t2) is `recovered` at any S/N.
    Without one, `n_t1_trials` decides where it was lost - reconcile() counts
    raw T1 trials in hella's candidate file. `fail_reason` is consulted only
    for fire_failed, which is set before any gate is known.
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
