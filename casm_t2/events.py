"""Event naming.

A tiered T2 event is named UTC date plus six random lowercase letters, e.g.
``260731abcdef``. The name keys the database, ``candidates/<name>/`` artifact
dirs, plot filenames, Slack messages and web URLs. Legacy 10-char names (four
letters) persist in the DB, so parsers must accept both lengths.

The suffix alphabet is strictly ``[a-z]``: downstream tooling interpolates
event names into shell commands unquoted, so no digits, dashes or uppercase.
Four letters (456,976/day) was exhausted by candidate storms; six gives
308,915,776/day. The search is bounded rather than retrying forever, since a
spin on the asyncio loop stalls ingest while a crash restarts.
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

    Both tables are checked: the fast path can write a ``triggers`` row without
    ever storing a cluster, so ``clusters.name`` misses some handed-out names.
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

    ``exclude`` holds names handed out in the current batch but not yet written
    to the DB; the uniqueness SELECT cannot see siblings of an in-flight gulp.
    Raises RuntimeError if ``max_attempts`` six-letter names and then the same
    number of seven-letter names all collide.
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
