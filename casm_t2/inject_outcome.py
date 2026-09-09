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

#: Short, plain phrases. These are read at a skim in Slack, so they name the
#: stage that lost the shot and nothing else - no mechanism, no tier names.
#: The detail is already in the ledger (`fail_reason`, the gate columns).
EXPLANATIONS = {
    MISSED_T1: "not detected by hella (T1)",
    MISSED_T2: "dropped by T2 clustering",
    MISSED_TRIGGER: "dropped by T2 filter criteria",
    FIRE_FAILED: "injection not fired",
}

#: `fail_reason` prefixes turned into a few words for the fire_failed line.
FIRE_FAILED_REASONS = {
    "fifo_write_failed": "FIFO write failed",
    "file_generation_failed": "file generation failed",
}


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
