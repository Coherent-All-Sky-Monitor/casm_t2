"""Closed enum for how one injection resolved, plus human explanations.

`fail_reason` in the ledger stays what it always was: free text naming the
first failed gate. `outcome` is the small closed set the Slack poster, the
streak logic and the summary figures count over, so a new failure string
never silently invents a new category.

Mapping from the gate columns (see casm_t2.apps.inject_daemon.reconcile):

    gate_t1=1, gate_t2=1, gate_trigger=1      -> recovered
    no matching cluster at all                -> missed_t1
    cluster found but gate_t2 = 0             -> missed_t2
    gate_t1 and gate_t2 set, gate_trigger = 0 -> missed_trigger
    the shot never reached the stream         -> fire_failed
"""

from __future__ import annotations

RECOVERED = "recovered"
MISSED_T1 = "missed_t1"
MISSED_T2 = "missed_t2"
MISSED_TRIGGER = "missed_trigger"
FIRE_FAILED = "fire_failed"

#: Outcomes that say something about pipeline sensitivity. `fire_failed` is
#: injector plumbing and is deliberately not one of them.
MISSES = (MISSED_T1, MISSED_T2, MISSED_TRIGGER)

ALL = (RECOVERED, MISSED_T1, MISSED_T2, MISSED_TRIGGER, FIRE_FAILED)

#: Set by the daemon on a shot that never reached the stream.
FIRE_FAILED_PREFIXES = ("fifo_write_failed", "file_generation_failed")

#: Fallback phrases, used only when reconcile() left no detail in
#: `fail_reason`. The real message is the detail: it names the stage AND what
#: the evidence at that stage actually was, DSA style. Anything counting over
#: injections still counts over the closed enum above, never over the text.
EXPLANATIONS = {
    MISSED_T1: "lost at T1: no cluster in the reconcile window",
    MISSED_T2: "lost at T2 clustering: no cluster formed",
    MISSED_TRIGGER: "lost at T2 filters: the cluster would not have triggered",
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
    if text.startswith("trigger_filters("):
        # pre-detail rows: the parenthesised gate dump is not a sentence
        return EXPLANATIONS[MISSED_TRIGGER] + f" ({text})"
    return explain(outcome)


def short_fire_reason(fail_reason: str | None) -> str:
    """A few words for why a shot never reached the stream."""
    head = str(fail_reason or "").split(":", 1)[0]
    return FIRE_FAILED_REASONS.get(head, head or "unknown")


def classify(gate_t1, gate_t2, gate_trigger,
             fail_reason: str | None = None) -> str:
    """Outcome for one reconciled injection.

    `fail_reason` is consulted only for the fire_failed case, which is set
    before any gate is known (the gates stay NULL for a shot that never
    reached the stream).
    """
    if fail_reason and str(fail_reason).startswith(FIRE_FAILED_PREFIXES):
        return FIRE_FAILED
    if gate_t1 and gate_t2 and gate_trigger:
        return RECOVERED
    if gate_t1 and gate_t2:
        return MISSED_TRIGGER
    if gate_t1:
        return MISSED_T2
    return MISSED_T1


def explain(outcome: str | None) -> str:
    """Human sentence for a miss; falls back to naming the raw outcome."""
    return EXPLANATIONS.get(str(outcome), f"outcome={outcome}")
