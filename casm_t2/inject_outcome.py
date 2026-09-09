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

EXPLANATIONS = {
    MISSED_T1: (
        "lost at T1: hella reported no candidate in the reconcile window "
        "matching the injected beam and DM"
    ),
    MISSED_T2: (
        "lost at T2: candidates arrived but nothing clustered into an event "
        "at the injected beam and DM"
    ),
    MISSED_TRIGGER: (
        "clustered but below the trigger filters (S/N tier, DM floor, beam "
        "count or beam veto) - it would not have produced a dump"
    ),
    FIRE_FAILED: (
        "the injection never reached the stream: file generation or the FIFO "
        "write failed, so this is an injector fault, not a pipeline miss"
    ),
}


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
