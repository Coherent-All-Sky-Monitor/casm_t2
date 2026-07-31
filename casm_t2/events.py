"""Event naming.

Every tiered T2 event gets a DSA-style name: the UTC date plus six random
lowercase letters, e.g. ``260731abcdef`` (12 chars). The name is the single
key across the database, ``candidates/<name>/`` artifact dirs, plot
filenames, Slack messages and web URLs.

Legacy 10-char names (four letters) minted before 2026-07-31 persist in the
DB and in artifact paths, so anything parsing names must accept both
lengths.

The alphabet after the ``[0-9]{6}`` date prefix is strictly ``[a-z]`` and
must stay that way: downstream tooling interpolates event names into shell
commands *unquoted* (ssh one-liners, spool paths), so a name containing a
shell metacharacter would be a command-injection hazard. Do not add digits,
dashes or uppercase to the suffix.

Why the size and the bound: four letters gave 456,976 combinations per day,
which candidate storms actually exhausted three times in production. The
old unbounded ``while True`` retry then spun forever on the asyncio
event-loop thread, freezing ingest and back-pressuring the whole telescope
DAQ. Six letters give 308,915,776 per day, and the search is now bounded:
``max_attempts`` tries at six letters, one escalation to seven, then a
RuntimeError. A loud crash is recoverable (systemd ``Restart=always``); a
spin on the event loop is not.
"""

from __future__ import annotations

import secrets
import sqlite3
import string
from collections.abc import Set
from datetime import datetime

ALPHABET = string.ascii_lowercase
SUFFIX_LEN = 6
ESCALATED_SUFFIX_LEN = 7


def _taken(conn: sqlite3.Connection, name: str) -> bool:
    """True if the name is already used by a cluster or a trigger row.

    Both tables must be checked: the fast path mints a name and writes a
    ``triggers`` row for it before (or without ever) storing a cluster, so
    ``clusters.name`` alone does not see every name handed out.
    """
    row = conn.execute(
        "SELECT 1 FROM clusters WHERE name = ?"
        " UNION ALL"
        " SELECT 1 FROM triggers WHERE candname = ?"
        " LIMIT 1", (name, name)).fetchone()
    return row is not None


def new_event_name(conn: sqlite3.Connection, event_utc: datetime,
                   exclude: Set[str] | None = None,
                   max_attempts: int = 8) -> str:
    """Mint an unused event name for ``event_utc``'s UTC date.

    ``exclude`` holds names already handed out in the current batch but not
    yet written to the DB — the uniqueness SELECT cannot see siblings of an
    in-flight gulp, and an intra-batch birthday collision used to surface as
    an IntegrityError that discarded the whole gulp.

    Raises RuntimeError if ``max_attempts`` six-letter names and then
    ``max_attempts`` seven-letter names all collide. That is astronomically
    unlikely with an intact database; if it happens the DB is wrong in a way
    that a retry loop cannot fix, and crashing is the recoverable outcome.
    """
    day = event_utc.strftime("%y%m%d")
    for n_letters in (SUFFIX_LEN, ESCALATED_SUFFIX_LEN):
        for _ in range(max_attempts):
            name = day + "".join(secrets.choice(ALPHABET)
                                 for _ in range(n_letters))
            if exclude is not None and name in exclude:
                continue
            if not _taken(conn, name):
                return name
    raise RuntimeError(
        f"could not mint a unique event name for {day} in "
        f"{max_attempts} attempts at {SUFFIX_LEN} letters plus "
        f"{max_attempts} at {ESCALATED_SUFFIX_LEN}")
