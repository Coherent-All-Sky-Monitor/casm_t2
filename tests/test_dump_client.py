"""Unit tests for the voltage dump CLI's window and stream arithmetic.

Pure functions only — nothing here opens a socket or talks to a daemon.
Run with ``pytest`` from the repo root.
"""

from datetime import datetime, timedelta, timezone

import pytest

from casm_t2.dump_client import (
    DISK_FLOOR_BYTES,
    FUTURE_LEAD_S,
    JANITOR_QUOTA_BYTES,
    PAST_LAG_S,
    VOLTAGE_BYTES_PER_SECOND,
    VOLTAGE_LOOKBACK_S,
    VOLTAGE_STREAMS_PER_NODE,
    node_volumes,
    parse_streams,
    preflight_issues,
    ring_age_refusal,
    voltage_window,
)

NOW = datetime(2026, 7, 31, 18, 0, 0, tzinfo=timezone.utc)


QUOTA_S = JANITOR_QUOTA_BYTES / VOLTAGE_BYTES_PER_SECOND  # ~72.7 s per stream

# Free space that clears the 200 GB floor for every case below.
ROOMY = {"casm-corr1": 900_000_000_000, "casm-corr2": 900_000_000_000}


def test_parse_streams_sorts_and_dedupes():
    assert parse_streams("3,4") == [3, 4]
    assert parse_streams("4, 3 ,4") == [3, 4]
    assert parse_streams("0,1,2,3,4,5") == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("spec,expected", [("3,", [3]), ("0,,1", [0, 1]), (",4", [4])])
def test_parse_streams_skips_empty_tokens(spec, expected):
    assert parse_streams(spec) == expected


@pytest.mark.parametrize("spec", ["", " ", "6", "-1", "two", "3;4"])
def test_parse_streams_rejects_junk(spec):
    with pytest.raises(ValueError):
        parse_streams(spec)


def test_last_window_ends_before_now():
    start, stop = voltage_window(NOW, last=5.0)
    assert stop == NOW - timedelta(seconds=PAST_LAG_S)
    assert start == stop - timedelta(seconds=5.0)


def test_next_window_starts_after_now():
    start, stop = voltage_window(NOW, ahead=30.0)
    assert start == NOW + timedelta(seconds=FUTURE_LEAD_S)
    assert stop == start + timedelta(seconds=30.0)


def test_explicit_window_with_and_without_fractional_seconds():
    start, stop = voltage_window(NOW, start="2026-07-31-18:00:00",
                                 stop="2026-07-31-18:00:02.500")
    assert start == NOW
    assert stop == NOW + timedelta(seconds=2.5)


@pytest.mark.parametrize("kwargs", [
    {},
    {"last": 2.0, "ahead": 2.0},
    {"start": "2026-07-31-18:00:00", "last": 2.0},
    {"start": "2026-07-31-18:00:00"},
    {"stop": "2026-07-31-18:00:02"},
])
def test_window_needs_exactly_one_form(kwargs):
    with pytest.raises(ValueError):
        voltage_window(NOW, **kwargs)


@pytest.mark.parametrize("kwargs", [{"last": 0.0}, {"last": -1.0}, {"ahead": 0.0}])
def test_window_rejects_nonpositive_durations(kwargs):
    with pytest.raises(ValueError):
        voltage_window(NOW, **kwargs)


def test_window_rejects_inverted_pair():
    with pytest.raises(ValueError):
        voltage_window(NOW, start="2026-07-31-18:00:02", stop="2026-07-31-18:00:00")


def test_ring_age_allows_a_window_inside_the_lookback():
    start, _ = voltage_window(NOW, last=VOLTAGE_LOOKBACK_S - PAST_LAG_S - 1.0)
    assert ring_age_refusal(NOW, start) is None


def test_ring_age_refuses_a_window_past_the_lookback():
    start, _ = voltage_window(NOW, last=VOLTAGE_LOOKBACK_S)
    refusal = ring_age_refusal(NOW, start)
    assert refusal is not None and "past" in refusal


def test_ring_age_allows_the_exact_lookback_boundary():
    start = NOW - timedelta(seconds=VOLTAGE_LOOKBACK_S)
    assert ring_age_refusal(NOW, start) is None
    assert ring_age_refusal(NOW, start - timedelta(seconds=0.001)) is not None


def test_ring_age_never_refuses_a_future_window():
    start, _ = voltage_window(NOW, ahead=60.0)
    assert ring_age_refusal(NOW, start) is None


def test_node_volumes_splits_by_node_and_counts_commanded_streams():
    vols = node_volumes(2.0, [0, 1, 3])
    assert set(vols) == {"casm-corr1", "casm-corr2"}
    assert vols["casm-corr1"][0] == 2
    assert vols["casm-corr2"][0] == 1
    assert vols["casm-corr1"][1] == int(2.0 * VOLTAGE_BYTES_PER_SECOND * 2)
    assert vols["casm-corr2"][1] == int(2.0 * VOLTAGE_BYTES_PER_SECOND * 1)


def test_disk_guard_ignores_how_many_streams_were_commanded():
    one = node_volumes(4.0, [3])
    three = node_volumes(4.0, [3, 4, 5])
    guard = int(4.0 * VOLTAGE_BYTES_PER_SECOND * VOLTAGE_STREAMS_PER_NODE)
    assert one["casm-corr2"][2] == guard
    assert three["casm-corr2"][2] == guard


def test_node_volumes_skips_nodes_with_no_commanded_streams():
    assert set(node_volumes(1.0, [4, 5])) == {"casm-corr2"}


def test_short_dump_on_an_empty_disk_has_nothing_to_say():
    assert preflight_issues(2.0, [0, 1, 2, 3, 4, 5], ROOMY) == ([], [])


def test_janitor_quota_refuses_dumps_past_the_per_stream_retention():
    assert preflight_issues(QUOTA_S - 1.0, [0], ROOMY)[0] == []
    refusals, _ = preflight_issues(QUOTA_S + 1.0, [0], ROOMY)
    assert len(refusals) == 1 and "janitor" in refusals[0]


def test_janitor_quota_counts_one_stream_not_the_whole_dump():
    """The quota is per stream_N tree, so commanding more streams is fine."""
    assert preflight_issues(10.0, [0, 1, 2, 3, 4, 5], ROOMY)[0] == []


def test_disk_floor_refuses_a_dump_that_eats_the_recorders_headroom():
    dump_bytes = 10.0 * VOLTAGE_BYTES_PER_SECOND
    tight = {"casm-corr2": int(DISK_FLOOR_BYTES + dump_bytes - 1)}
    refusals, _ = preflight_issues(10.0, [3], tight)
    assert len(refusals) == 1 and "floor" in refusals[0]

    ample = {"casm-corr2": int(DISK_FLOOR_BYTES + dump_bytes + 1)}
    assert preflight_issues(10.0, [3], ample)[0] == []


def test_unknown_free_space_warns_but_does_not_refuse():
    refusals, warnings = preflight_issues(2.0, [0, 3], {"casm-corr1": None,
                                                        "casm-corr2": None})
    assert refusals == []
    assert len(warnings) == 2 and all("could not read free space" in w for w in warnings)


def test_missing_host_counts_as_unknown_free_space():
    refusals, warnings = preflight_issues(2.0, [0], {})
    assert refusals == [] and len(warnings) == 1


def test_disk_guard_warnings_survive_as_warnings():
    """Room for this dump and the floor, but not for the guard's three streams.

    Only long dumps can be in that state: the guard wants three streams'
    worth, so it only outgrows the 200 GB floor past ~32 s.
    """
    duration_s = 40.0
    dump_bytes = duration_s * VOLTAGE_BYTES_PER_SECOND
    free = {"casm-corr2": int(dump_bytes + DISK_FLOOR_BYTES + 1)}
    refusals, warnings = preflight_issues(duration_s, [3], free)
    assert refusals == []
    assert len(warnings) == 1 and "next one will be discarded" in warnings[0]

    duration_s = 60.0
    dump_bytes = duration_s * VOLTAGE_BYTES_PER_SECOND
    free = {"casm-corr2": int(dump_bytes * VOLTAGE_STREAMS_PER_NODE - 1)}
    refusals, warnings = preflight_issues(duration_s, [3], free)
    assert refusals == []
    assert len(warnings) == 1 and "already under the disk guard" in warnings[0]


def test_force_turns_every_refusal_into_a_warning():
    tight = {"casm-corr1": DISK_FLOOR_BYTES}
    refusals, warnings = preflight_issues(QUOTA_S + 1.0, [0], tight)
    assert len(refusals) == 2

    forced_refusals, forced_warnings = preflight_issues(QUOTA_S + 1.0, [0], tight,
                                                        force=True)
    assert forced_refusals == []
    assert set(refusals) <= set(forced_warnings)


# ---------------------------------------------------------------------------
# --gather arithmetic
# ---------------------------------------------------------------------------

from casm_t2.dump_client import (  # noqa: E402
    expected_stream_bytes,
    reader_prefix,
    stream_dump_state,
)


def _name(utc="2026-07-30-22:25:22", offset=0, num=0):
    return f"{utc}_{offset:016d}.{num:06d}.dada"


def test_expected_bytes_track_the_stream_rate():
    assert expected_stream_bytes(2.0) == int(2.0 * VOLTAGE_BYTES_PER_SECOND * 0.999)


def test_dump_state_waiting_before_any_new_file():
    before = {_name(offset=1)}
    assert stream_dump_state(before, {_name(offset=1): 10**9}, 10**6) == ("waiting", [])


def test_dump_state_growing_until_the_payload_lands():
    new = _name(offset=2)
    state, files = stream_dump_state(set(), {new: 4096 + 500}, 1000)
    assert (state, files) == ("growing", [new])


def test_dump_state_done_ignores_preexisting_files():
    old, new = _name(offset=1), _name(offset=2)
    sizes = {old: 123, new: 4096 + 1000}
    assert stream_dump_state({old}, sizes, 1000) == ("done", [new])


def test_dump_state_sums_a_split_dump():
    a, b = _name(offset=0, num=0), _name(offset=500, num=1)
    sizes = {a: 4096 + 600, b: 4096 + 400}
    assert stream_dump_state(set(), sizes, 1000) == ("done", [a, b])


def test_reader_prefix_pins_a_single_file_dump():
    new = {0: [_name(offset=7)], 3: [_name(offset=7)]}
    prefix, notes = reader_prefix(new)
    assert prefix == f"2026-07-30-22:25:22_{7:016d}"
    assert notes == []


def test_reader_prefix_falls_back_for_split_dumps():
    new = {0: [_name(offset=0, num=0), _name(offset=500, num=1)],
           1: [_name(offset=0, num=0)]}
    prefix, notes = reader_prefix(new)
    assert prefix == "2026-07-30-22:25:22"
    assert any("several files" in n for n in notes)


def test_reader_prefix_refuses_mixed_utc_starts():
    new = {0: [_name(utc="2026-07-30-22:25:22")],
           1: [_name(utc="2026-07-30-23:00:00")]}
    prefix, notes = reader_prefix(new)
    assert prefix is None
    assert any("different dumps" in n for n in notes)


def test_reader_prefix_notes_offset_mismatch():
    new = {0: [_name(offset=1)], 1: [_name(offset=2)]}
    prefix, notes = reader_prefix(new)
    assert prefix == "2026-07-30-22:25:22"
    assert any("OBS_OFFSET" in n for n in notes)
