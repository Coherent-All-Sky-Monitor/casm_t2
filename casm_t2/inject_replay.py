"""Automated replay of an injection: dump the stream, render it, post the plot.

The daemon's truth plot is rendered from the .fil pushed into the FIFO, so it
shows the pulse as generated. Intensity dumps tap upstream of the injection
merge, so they cannot show the pulse in the live stream. The replay closes that
gap: dump the injected stream around the shot, add the pulse at the time it
should have arrived, render it. On a recovered shot that is what hella saw, on a
miss what it should have found.

Timeline per shot, from the FIFO write:

    0 s     ledger row, FIFO write, Slack "injection sent"
    ~20 s   the intensity dump completes (the daemon replies after writing)
    ~100 s  reconcile: the outcome edits the Slack message
    ~110 s  the replay PNG lands as a thread reply under it

Fail-soft: a failure to dump, render or post leaves the injection and its ledger
row untouched.
"""

from __future__ import annotations

import logging
import shutil
import statistics
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from casm_t2 import inject_outcome as oc

logger = logging.getLogger("t2.inject.replay")

#: When no shot has been recovered yet, this is where the pulse is assumed to
#: have landed relative to inject_utc. Negative: the sidecar joins a gulp
#: whose samples are already seconds old, so the pulse precedes the write.
DEFAULT_LEAD_S = -16.0

POST_MODES = ("daily", "on_miss", "always", "never")


def replay_cfg(icfg: dict | None) -> dict:
    """The `injection.replay` block with defaults filled in."""
    cfg = dict((icfg or {}).get("replay") or {})
    cfg.setdefault("post", "daily")
    cfg.setdefault("dump_pre_s", 24.0)
    cfg.setdefault("dump_post_s", 10.0)
    cfg.setdefault("dump_timeout_s", 60.0)
    cfg.setdefault("keep_dump", False)
    cfg.setdefault("events_root", "/mnt/nvme3/T3/EVENTS")
    cfg.setdefault("command", "t3-replay-injection")
    # A shot the search did not find. `none` renders nothing, the card carrying
    # the red bar and the reason. `expected` renders it with the pulse where it
    # should have been.
    cfg.setdefault("on_miss", "none")
    # On a miss the dump is the only copy of what hella saw, so keep it.
    cfg.setdefault("keep_dump_on_miss", True)
    if cfg["on_miss"] not in ("none", "expected"):
        logger.warning("injection.replay.on_miss %r is not none|expected; "
                       "using none", cfg["on_miss"])
        cfg["on_miss"] = "none"
    if cfg["post"] not in POST_MODES:
        logger.warning("injection.replay.post %r is not one of %s; "
                       "treating it as 'never'", cfg["post"], POST_MODES)
        cfg["post"] = "never"
    return cfg


def dump_window(inject_utc: datetime, rcfg: dict) -> tuple[datetime, datetime]:
    """[inject_utc - dump_pre_s, inject_utc - dump_post_s].

    Both offsets go backwards from the FIFO write, because that is where the
    pulse is: the sidecar joins a gulp whose samples are already 5-20 s old.
    """
    pre = float(rcfg.get("dump_pre_s", 24.0))
    post = float(rcfg.get("dump_post_s", 10.0))
    return (inject_utc - timedelta(seconds=pre),
            inject_utc - timedelta(seconds=post))


def dump_due(rcfg: dict) -> bool:
    """Whether a dump should be requested for this shot.

    Requested whenever posting is possible. Whether the PNG is posted is decided
    once the outcome is known, by which time the dump must already exist.
    """
    return rcfg.get("post") != "never"


def post_due(rcfg: dict, outcome: str | None, last_post_day: str | None,
             now: datetime | None = None) -> bool:
    """Whether the replay PNG should be posted for this shot.

    `never` posts nothing, `always` every shot, `on_miss` only misses, `daily`
    every miss plus the first shot of each UTC day.
    """
    mode = rcfg.get("post", "daily")
    if mode == "never":
        return False
    if mode == "always":
        return True
    missed = outcome in oc.MISSES
    if mode == "on_miss":
        return missed
    # daily
    if missed:
        return True
    today = f"{now or datetime.now(timezone.utc):%Y-%m-%d}"
    return last_post_day != today


def last_replay_day(conn) -> str | None:
    """UTC day of the most recent shot whose replay was posted."""
    row = conn.execute(
        "SELECT max(substr(inject_utc, 1, 10)) FROM injections"
        " WHERE replay_posted = 1").fetchone()
    return row[0] if row and row[0] else None


def expected_event_utc(conn, inject_utc: datetime,
                       default_lead_s: float = DEFAULT_LEAD_S) -> datetime:
    """When the pulse should have arrived, for a shot that was not recovered.

    The median lead of the last ten recovered shots, the sidecar lag drifting
    over weeks. Falls back to DEFAULT_LEAD_S when nothing has been recovered.
    """
    rows = conn.execute(
        "SELECT rec_lead_s FROM injections WHERE outcome = ?"
        " AND rec_lead_s IS NOT NULL ORDER BY id DESC LIMIT 10",
        (oc.RECOVERED,)).fetchall()
    leads = [float(r[0]) for r in rows if r[0] is not None]
    lead = statistics.median(leads) if leads else float(default_lead_s)
    return inject_utc + timedelta(seconds=lead)


def replay_paths(inj_id, rcfg: dict, label: str | None = None) -> dict:
    """Where this shot's products go: <events_root>/<label>/<label>.*

    `label` is the display name (`inj_20260910_0002`); without one it falls
    back to the ledger id, which is what the very first shots used.
    """
    name = str(label or f"inj{inj_id}")
    root = Path(rcfg.get("events_root", "/mnt/nvme3/T3/EVENTS")) / name
    return {"dir": root,
            "png": root / f"{name}.png",
            "json": root / f"{name}.json",
            "fil": root / f"{name}.fil"}


def resolve_command(command: str) -> list[str]:
    """Split `replay.command` into argv, finding the tool if PATH lacks it.

    Under systemd the PATH excludes the venv, so a bare `t3-replay-injection`
    is looked for next to the running interpreter. An absolute path or a
    `python -m ...` form is left alone.
    """
    parts = str(command).split()
    if not parts:
        return parts
    head = parts[0]
    if "/" in head or shutil.which(head):
        return parts
    beside = Path(sys.executable).parent / head
    if beside.is_file():
        logger.info("replay command %r is not on PATH; using %s", head, beside)
        return [str(beside), *parts[1:]]
    logger.warning("replay command %r is not on PATH and not beside %s",
                   head, sys.executable)
    return parts


def build_command(inj_id: int, dump_dir, rcfg: dict, db_path: str | None = None,
                  event_utc: datetime | None = None,
                  label: str | None = None,
                  card_only: bool = False) -> list[str]:
    """The replay tool's argv.

    `event_utc` is passed only for a shot with no recovered cluster: the tool
    then adds the pulse at the time it should have arrived, so the reader
    sees what hella should have found.
    """
    paths = replay_paths(inj_id, rcfg, label)
    cmd = [*resolve_command(rcfg.get("command", "t3-replay-injection")),
           "--inject-id", str(inj_id),
           "--dump", str(dump_dir),
           "--card-json", str(paths["json"])]
    if card_only:
        # A rendered miss archives the numbers, not the pictures. --out is
        # required by the tool, so its PNG goes to a scratch path.
        cmd += ["--out", str(paths["dir"] / f"_scratch_{paths['png'].name}")]
    else:
        cmd += ["--out", str(paths["png"]), "--fil", str(paths["fil"])]
    if db_path:
        cmd += ["--db", str(db_path)]
    if event_utc is not None:
        cmd += ["--event-utc", event_utc.isoformat(timespec="milliseconds")]
    if label:
        cmd += ["--label", label]
    return cmd


def run_replay(inj_id: int, dump_dir, rcfg: dict, db_path: str | None = None,
               event_utc: datetime | None = None, runner=None,
               label: str | None = None, card_only: bool = False) -> Path | None:
    """Render the replay; returns the PNG path, or None on any failure."""
    paths = replay_paths(inj_id, rcfg, label)
    cmd = build_command(inj_id, dump_dir, rcfg, db_path, event_utc, label,
                        card_only)
    runner = runner or subprocess.run
    try:
        paths["dir"].mkdir(parents=True, exist_ok=True)
        res = runner(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:  # noqa: BLE001 - a plot must never break the shot
        logger.warning("replay of injection %s failed to run: %s", inj_id, exc)
        return None
    if getattr(res, "returncode", 1) != 0:
        logger.warning("replay of injection %s exited %s: %s", inj_id,
                       getattr(res, "returncode", "?"),
                       (getattr(res, "stderr", "") or "")[-500:])
        return None
    if not paths["png"].is_file():
        logger.warning("replay of injection %s produced no PNG at %s",
                       inj_id, paths["png"])
        return None
    return paths["png"]


#: Intensity dump rate per stream, the ring's BYTES_PER_SECOND. A file's
#: byte offset in its name converts to seconds from the observation start
#: with this (same constant casm_t3's janitor uses).
BYTES_PER_SECOND = 375e6

#: A dump of the configured window is one or two files. More than this means
#: the window arithmetic is wrong, so delete nothing.
MAX_DELETE = 4


def dump_file_span(path) -> tuple[datetime, datetime] | None:
    """Sky-time interval a .dada file covers, from its name and size.

    Files are named ``<UTC_START>_<byteoffset>.000000.dada``, the offset being
    bytes since the observation started. None when the name does not parse,
    which must not be read as no overlap.
    """
    path = Path(path)
    try:
        obs_s, rest = path.name.split("_", 1)
        offset = int(rest.split(".")[0])
        size = path.stat().st_size
    except (ValueError, IndexError, OSError):
        return None
    try:
        from casm_t2 import timing
        t0 = timing.parse_dada_utc(obs_s) + timedelta(
            seconds=offset / BYTES_PER_SECOND)
    except ValueError:
        return None
    return t0, t0 + timedelta(seconds=size / BYTES_PER_SECOND)


def dump_files_in_window(dump_dir, start: datetime, stop: datetime,
                         margin_s: float = 2.0) -> list[Path]:
    """The .dada files in `dump_dir` overlapping [start, stop].

    `dump_dir` is a per-stream directory shared with T2's triggered dumps, so
    selection is by the window each file covers, from its name and size. A file
    whose name does not parse is left alone.
    """
    lo = start - timedelta(seconds=margin_s)
    hi = stop + timedelta(seconds=margin_s)
    hits = []
    for path in sorted(Path(dump_dir).glob("*.dada")):
        span = dump_file_span(path)
        if span is None:
            continue
        f0, f1 = span
        if f0 <= hi and f1 >= lo:
            hits.append(path)
    return hits


def keep_for_miss(rcfg: dict, outcome: str | None) -> bool:
    """Should this shot's dump be kept because the search missed it?"""
    return bool(rcfg.get("keep_dump_on_miss", True)) and outcome in oc.MISSES


def cleanup_dump(dump_dir, rcfg: dict, start=None, stop=None,
                 outcome: str | None = None, inj_label=None) -> int:
    """Delete this injection's dump files. Returns how many went.

    Never removes the directory: `dump_dir` is
    `/mnt/nvme4/data/casm/cand_beam_dumps/stream_N`, shared with T2's triggered
    dumps. Only files whose window overlaps the recorded [start, stop] are
    removed, at most MAX_DELETE of them, each logged by full path. Without a
    recorded window nothing is deleted, this shot's files then being
    indistinguishable from anyone else's.
    """
    if rcfg.get("keep_dump"):
        logger.info("keeping dump files in %s (keep_dump)", dump_dir)
        return 0
    path = Path(dump_dir)
    if not path.is_dir():
        return 0
    if keep_for_miss(rcfg, outcome):
        kept = (dump_files_in_window(path, start, stop)
                if start and stop else [])
        for f in kept:
            logger.info("injection %s missed (%s): keeping raw dump %s",
                        inj_label or "?", outcome, f)
        if not kept:
            logger.info("injection %s missed (%s): no dump files matched the "
                        "window in %s", inj_label or "?", outcome, path)
        return 0
    if start is None or stop is None:
        logger.warning("no dump window recorded for %s; deleting nothing "
                       "(the janitor will reclaim it)", path)
        return 0
    hits = dump_files_in_window(path, start, stop)
    if not hits:
        logger.info("no dump files in %s overlap [%s .. %s]", path, start, stop)
        return 0
    if len(hits) > MAX_DELETE:
        logger.error("dump cleanup in %s selected %d files for [%s .. %s], "
                     "more than the %d expected; deleting nothing",
                     path, len(hits), start, stop, MAX_DELETE)
        return 0
    n = 0
    for f in hits:
        try:
            f.unlink()
        except OSError as exc:
            logger.warning("could not delete %s: %s", f, exc)
            continue
        logger.info("deleted dump file %s", f)
        n += 1
    return n


#: Comment on a separately posted plot: the shot id alone.
CAPTION = ""
