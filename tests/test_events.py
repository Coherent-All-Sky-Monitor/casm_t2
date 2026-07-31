"""Event naming: shape, uniqueness, and — above all — boundedness.

The unbounded retry loop this replaces spun forever on the asyncio event
loop once the 4-letter name space filled, wedging ingest three times in
production. The bound is the point of these tests.
"""

import re
from datetime import datetime, timezone

import pytest

from casm_t2 import db, events

DAY = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
PREFIX = "260731"


def scripted_choice(suffixes):
    """Replacement for secrets.choice that emits the given suffixes in order.

    new_event_name draws one letter at a time, so the suffixes are simply
    concatenated and served letter by letter.
    """
    letters = iter("".join(suffixes))

    def choice(_alphabet):
        return next(letters)

    return choice


def test_name_shape_is_date_plus_six_lowercase(conn):
    """YYMMDD + 6 letters, and the suffix alphabet stays strictly [a-z].

    Downstream interpolates names into unquoted shell commands, so a digit
    or metacharacter in the suffix would be a command-injection hazard.
    """
    for _ in range(50):
        name = events.new_event_name(conn, DAY)
        assert len(name) == 12
        assert re.fullmatch(r"[0-9]{6}[a-z]{6}", name), name
        assert name.startswith(PREFIX)


def test_name_uses_the_event_date_not_today(conn):
    name = events.new_event_name(conn, datetime(2025, 1, 2, tzinfo=timezone.utc))
    assert name.startswith("250102")


def test_avoids_a_name_already_in_clusters(conn, cluster_row, monkeypatch):
    taken = PREFIX + "aaaaaa"
    db.insert_clusters(conn, [cluster_row(taken)])
    monkeypatch.setattr(events.secrets, "choice",
                        scripted_choice(["aaaaaa", "bbbbbb"]))
    assert events.new_event_name(conn, DAY) == PREFIX + "bbbbbb"


def test_avoids_a_name_already_in_triggers(conn, monkeypatch):
    """The fast path can mint a triggers row whose cluster never materialises.

    Checking clusters.name alone would hand that name out a second time.
    """
    taken = PREFIX + "cccccc"
    db.insert_trigger(conn, None, taken, 0, "triggered", "fast:tier_A")
    assert conn.execute("SELECT count(*) FROM clusters").fetchone()[0] == 0

    monkeypatch.setattr(events.secrets, "choice",
                        scripted_choice(["cccccc", "dddddd"]))
    assert events.new_event_name(conn, DAY) == PREFIX + "dddddd"


def test_exclude_set_is_honoured(conn, monkeypatch):
    """Names handed out earlier in the same gulp are not yet in the DB."""
    monkeypatch.setattr(events.secrets, "choice",
                        scripted_choice(["eeeeee", "ffffff"]))
    name = events.new_event_name(conn, DAY, exclude={PREFIX + "eeeeee"})
    assert name == PREFIX + "ffffff"


def test_exclude_none_still_works(conn, monkeypatch):
    monkeypatch.setattr(events.secrets, "choice", scripted_choice(["gggggg"]))
    assert events.new_event_name(conn, DAY, exclude=None) == PREFIX + "gggggg"


def test_escalates_to_seven_letters_when_six_are_exhausted(conn, cluster_row,
                                                           monkeypatch):
    """Every 6-letter draw collides, so the search widens rather than spinning."""
    db.insert_clusters(conn, [cluster_row(PREFIX + "aaaaaa")])
    monkeypatch.setattr(events.secrets, "choice", lambda _alphabet: "a")

    name = events.new_event_name(conn, DAY, max_attempts=4)

    assert name == PREFIX + "aaaaaaa"
    assert re.fullmatch(r"[0-9]{6}[a-z]{7}", name)


def test_raises_rather_than_spinning_when_both_lengths_collide(conn, cluster_row,
                                                               monkeypatch):
    db.insert_clusters(conn, [cluster_row(PREFIX + "aaaaaa"),
                              cluster_row(PREFIX + "aaaaaaa", gulp=2)])
    monkeypatch.setattr(events.secrets, "choice", lambda _alphabet: "a")

    with pytest.raises(RuntimeError, match="unique event name"):
        events.new_event_name(conn, DAY, max_attempts=4)


def test_attempt_count_is_bounded(conn, cluster_row, monkeypatch):
    """max_attempts draws at 6 letters, then max_attempts at 7, then stop."""
    db.insert_clusters(conn, [cluster_row(PREFIX + "aaaaaa"),
                              cluster_row(PREFIX + "aaaaaaa", gulp=2)])
    draws = 0

    def counting_choice(_alphabet):
        nonlocal draws
        draws += 1
        return "a"

    monkeypatch.setattr(events.secrets, "choice", counting_choice)
    with pytest.raises(RuntimeError):
        events.new_event_name(conn, DAY, max_attempts=3)

    assert draws == 3 * 6 + 3 * 7


def test_exclude_alone_cannot_cause_a_spin(conn, monkeypatch):
    """An exclude set covering every possible draw still terminates."""
    monkeypatch.setattr(events.secrets, "choice", lambda _alphabet: "a")
    blocked = {PREFIX + "aaaaaa", PREFIX + "aaaaaaa"}
    with pytest.raises(RuntimeError):
        events.new_event_name(conn, DAY, exclude=blocked, max_attempts=3)
