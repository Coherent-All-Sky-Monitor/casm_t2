"""Automated replay of an injection: dump the stream, render it, post the plot.

The truth plot the daemon already makes is rendered from the .fil that was
pushed into the FIFO, so it shows the pulse as generated. It cannot show what
the pulse looked like once it was merged into the live stream, because
intensity dumps tap upstream of the injection merge - which is precisely the
thing worth seeing when a shot is recovered at half the expected S/N, or not
at all.

The replay closes that gap: dump the injected stream around the shot, add the
pulse to the dump at the time it should have arrived, and render it. On a
recovered shot it shows what hella saw. On a miss it shows what hella should
have found and did not, which is the more useful of the two.

Timeline per shot, from the FIFO write:

    0 s     ledger row, FIFO write, Slack "injection sent"
    ~20 s   the intensity dump completes (the daemon replies after writing)
    ~100 s  reconcile: the outcome edits the Slack message
    ~110 s  the replay PNG lands as a thread reply under it

Everything here is fail-soft: a failure to dump, render or post leaves the
injection and its ledger row untouched.
"""

from __future__ import annotations

import logging
import shutil
import statistics
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
    """Should a dump be requested for this shot at all?

    Requested whenever posting is possible: whether the PNG is *posted* is
    decided later, once the outcome is known, but the dump has to exist by
    then and cannot be taken retrospectively.
    """
    return rcfg.get("post") != "never"


def post_due(rcfg: dict, outcome: str | None, last_post_day: str | None,
             now: datetime | None = None) -> bool:
    """Should the replay PNG be posted for this shot?

    `never` posts nothing. `always` posts every shot. `on_miss` posts only
    misses. `daily` posts every miss plus the first shot of each UTC day, so
    a quiet day still shows one worked example and a bad day shows all of it.
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

    The median lead of the last ten recovered shots: the sidecar lag drifts
    over weeks, so a measured recent median beats a constant. Falls back to
    DEFAULT_LEAD_S when nothing has been recovered yet.
    """
    rows = conn.execute(
        "SELECT rec_lead_s FROM injections WHERE outcome = ?"
        " AND rec_lead_s IS NOT NULL ORDER BY id DESC LIMIT 10",
        (oc.RECOVERED,)).fetchall()
    leads = [float(r[0]) for r in rows if r[0] is not None]
    lead = statistics.median(leads) if leads else float(default_lead_s)
    return inject_utc + timedelta(seconds=lead)


def replay_paths(inj_id: int, rcfg: dict) -> dict:
    """Where this shot's replay products go: <events_root>/inj<id>/inj<id>.*"""
    root = Path(rcfg.get("events_root", "/mnt/nvme3/T3/EVENTS")) / f"inj{inj_id}"
    return {"dir": root,
            "png": root / f"inj{inj_id}.png",
            "json": root / f"inj{inj_id}.json",
            "fil": root / f"inj{inj_id}.fil"}


def build_command(inj_id: int, dump_dir, rcfg: dict, db_path: str | None = None,
                  event_utc: datetime | None = None) -> list[str]:
    """The replay tool's argv.

    `event_utc` is passed only for a shot with no recovered cluster: the tool
    then adds the pulse at the time it should have arrived, so the reader
    sees what hella should have found.
    """
    paths = replay_paths(inj_id, rcfg)
    cmd = [*str(rcfg.get("command", "t3-replay-injection")).split(),
           "--inject-id", str(inj_id),
           "--dump", str(dump_dir),
           "--out", str(paths["png"]),
           "--card-json", str(paths["json"]),
           "--fil", str(paths["fil"])]
    if db_path:
        cmd += ["--db", str(db_path)]
    if event_utc is not None:
        cmd += ["--event-utc", event_utc.isoformat(timespec="milliseconds")]
    return cmd


def run_replay(inj_id: int, dump_dir, rcfg: dict, db_path: str | None = None,
               event_utc: datetime | None = None, runner=None) -> Path | None:
    """Render the replay; returns the PNG path, or None on any failure."""
    paths = replay_paths(inj_id, rcfg)
    cmd = build_command(inj_id, dump_dir, rcfg, db_path, event_utc)
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


def cleanup_dump(dump_dir, rcfg: dict) -> bool:
    """Delete the dump unless keep_dump. Returns True if it was removed."""
    if rcfg.get("keep_dump"):
        logger.info("keeping dump %s (keep_dump)", dump_dir)
        return False
    path = Path(dump_dir)
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        logger.warning("could not delete dump %s: %s", path, exc)
        return False
    logger.info("deleted dump %s", path)
    return True


CAPTION = "replay: injected pulse added to the stream dump"
