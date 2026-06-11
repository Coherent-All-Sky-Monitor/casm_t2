"""Event naming.

Every tiered T2 event gets a DSA-style name — UTC date plus four random
lowercase letters, e.g. ``260611abcd`` — used as the single key across the
database, candidates/<name>/ artifact dirs, plot filenames, Slack messages
and web URLs. Names are checked for uniqueness against the clusters table
(457k combinations per day; collisions are retried, exhaustion impossible
at our event rates).
"""

from __future__ import annotations

import secrets
import sqlite3
import string
from datetime import datetime


def new_event_name(conn: sqlite3.Connection, event_utc: datetime) -> str:
    day = event_utc.strftime("%y%m%d")
    while True:
        name = day + "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))
        row = conn.execute("SELECT 1 FROM clusters WHERE name = ?", (name,)).fetchone()
        if row is None:
            return name
