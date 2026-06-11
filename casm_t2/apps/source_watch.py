"""Watch the live hella candidate stream for a known source and trigger dumps.

This is the Phase-0 seed of the T2 daemon: it listens on the eight TCP ports
that the hella jobs already publish to, filters candidates down to a known
source (DM window + per-beam transit schedule + SNR floor), and for each
accepted event immediately requests a beam intensity dump from the owning
casm_cand_dump daemon. A small JSON "trigger card" is dropped in the owning
node's T2 spool directory so the per-node plotter (casm_t3) can pick the
event up once the dump lands.

Latency matters: the dump ring buffers only reach ~20 s into the past and
hella itself reports up to ~16 s after the pulse, so triggering happens
inline in the ingest path, before any bookkeeping.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import functools
from collections import deque
import json
import logging
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from casm_t2 import beams, timing, wire
from casm_t2.dump_client import request_dump_async

logger = logging.getLogger("t2.source_watch")

LOCAL_HOSTNAME = socket.gethostname().split(".")[0]


@dataclass(slots=True)
class TransitWindow:
    beam: int
    start: datetime
    end: datetime


class Schedule:
    """Per-beam transit windows, with a configurable pad on either side."""

    def __init__(self, windows: list[TransitWindow], pad_s: float):
        self._by_beam: dict[int, list[TransitWindow]] = {}
        for w in windows:
            self._by_beam.setdefault(w.beam, []).append(w)
        self._pad = timedelta(seconds=pad_s)

    @classmethod
    def from_csv(cls, path: str | Path, pad_s: float) -> "Schedule":
        windows = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                windows.append(TransitWindow(
                    beam=int(row["beam"]),
                    start=datetime.fromisoformat(row["utc_start"]),
                    end=datetime.fromisoformat(row["utc_end"]),
                ))
        logger.info("loaded %d transit windows for %d beams from %s",
                    len(windows), len({w.beam for w in windows}), path)
        return cls(windows, pad_s)

    def active(self, beam: int, t: datetime) -> bool:
        return any(w.start - self._pad <= t <= w.end + self._pad
                   for w in self._by_beam.get(beam, ()))


class Observation:
    """Tracks the current observation's UTC_START from the hella output files."""

    def __init__(self, cands_dir: str, recheck_s: float = 60.0):
        self._dir = Path(cands_dir)
        self._recheck = recheck_s
        self._checked_at = 0.0
        self._utc_start: datetime | None = None
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked_at < self._recheck:
            return
        self._checked_at = now
        newest = max(self._dir.glob("cands_*.dat.*"), key=lambda p: p.stat().st_mtime, default=None)
        if newest is None:
            logger.error("no hella candidate files in %s", self._dir)
            return
        utc_start = timing.utc_start_from_cands_path(newest)
        if utc_start != self._utc_start:
            logger.info("observation UTC_START = %s (from %s)", utc_start, newest.name)
            self._utc_start = utc_start

    @property
    def utc_start(self) -> datetime | None:
        self.refresh()
        return self._utc_start


class TriggerPolicy:
    """Global rate limiting: minimum spacing between dumps plus a daily cap."""

    def __init__(self, min_spacing_s: float, daily_max: int):
        self._min_spacing = timedelta(seconds=min_spacing_s)
        self._daily_max = daily_max
        self._last_trigger: datetime | None = None
        self._count_day: str | None = None
        self._count = 0

    def check(self, now: datetime) -> str | None:
        """Return a suppression reason, or None if a trigger is allowed."""
        if self._last_trigger is not None and now - self._last_trigger < self._min_spacing:
            return "spacing"
        day = now.strftime("%Y-%m-%d")
        if day == self._count_day and self._count >= self._daily_max:
            return "daily_cap"
        return None

    def record(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self._count_day:
            self._count_day, self._count = day, 0
        self._last_trigger = now
        self._count += 1


class SourceWatch:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.source = cfg["source_name"]
        self.obs = Observation(cfg["hella_cands_dir"])
        self.schedule = Schedule.from_csv(cfg["schedule_csv"], cfg.get("schedule_pad_s", 120))
        self.policy = TriggerPolicy(cfg["trigger"]["min_spacing_s"], cfg["trigger"]["daily_max"])
        self.dm_min = cfg["dm_min"]
        self.dm_max = cfg["dm_max"]
        self.snr_min = cfg["snr_min"]
        self.veto = set(cfg.get("beam_veto", []))
        self.enabled = cfg["trigger"].get("enabled", True)
        ctx = cfg.get("context", {})
        self.ctx_window_s = ctx.get("window_s", 4.0)
        self.ctx_delay_s = ctx.get("delay_s", 8.0)
        self.ctx_max_members = ctx.get("max_members", 3000)
        # Rolling buffer of every candidate seen on any port, kept long
        # enough to reconstruct the beam/DM neighbourhood of a trigger.
        self.context: deque[tuple[float, int, float, float, int]] = deque(maxlen=400_000)
        self._card_tasks: set[asyncio.Task] = set()
        # Optional tee: forward each raw payload to 127.0.0.1:<base>+job so a
        # shadow consumer (t2d) sees the stream without owning hella's ports.
        # Strictly fire-and-forget; a dead consumer must never cost latency.
        self.tee_base = cfg.get("tee_base_port")
        self._tee_tasks: set[asyncio.Task] = set()
        self.log_path = Path(cfg["log_csv"])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text(
                "recv_utc,event_utc,beam,stream,snr,dm,width,samp,action,detail\n")
        self.n_batches = 0
        self.n_cands = 0

    # ------------------------------------------------------------- ingest

    async def serve(self) -> None:
        host = self.cfg.get("listen_host", "0.0.0.0")
        servers = []
        for job, port in enumerate(self.cfg["ports"]):
            handler = functools.partial(self._handle, job=job)
            servers.append(await asyncio.start_server(handler, host, port))
            logger.info("listening on %s:%d", host, port)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._heartbeat())
            for s in servers:
                tg.create_task(s.serve_forever())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      job: int = 0) -> None:
        try:
            # Hella closes the connection after each gulp; read to EOF.
            payload = await asyncio.wait_for(reader.read(-1), timeout=30)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            logger.warning("ingest connection error: %s", exc)
            return
        finally:
            writer.close()
        if self.tee_base is not None and payload:
            task = asyncio.create_task(self._tee(job, payload))
            self._tee_tasks.add(task)
            task.add_done_callback(self._tee_tasks.discard)
        batch = wire.parse_batch(payload.decode(errors="replace"))
        self.n_batches += 1
        self.n_cands += len(batch.cands)
        if batch.cands:
            await self._process(batch)

    async def _tee(self, job: int, payload: bytes) -> None:
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.tee_base + job), timeout=1.0)
            w.write(payload)
            await w.drain()
            w.close()
            await w.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass  # shadow consumer down; the live path does not care

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(60)
            logger.info("heartbeat: %d batches, %d candidates in last minute",
                        self.n_batches, self.n_cands)
            self.n_batches = self.n_cands = 0

    # ------------------------------------------------------------ filtering

    async def _process(self, batch: wire.Batch) -> None:
        # Prefer the observation identity carried on the wire by the
        # deployed hella preamble; fall back to the output filenames.
        if batch.utc_start is not None:
            utc_start = timing.parse_dada_utc(batch.utc_start)
        else:
            utc_start = self.obs.utc_start
        if utc_start is None:
            return
        tsamp = batch.tsamp_s or timing.TSAMP_S
        now = datetime.now(timezone.utc)

        epoch = utc_start.timestamp()
        for c in batch.cands:
            self.context.append((epoch + c.samp * tsamp, c.beam, c.dm, c.snr, c.width))

        hits = []
        for c in batch.cands:
            if not (self.dm_min <= c.dm <= self.dm_max) or c.snr < self.snr_min:
                continue
            if c.beam in self.veto:
                continue
            event_utc = timing.samp_to_utc(c.samp, utc_start, tsamp)
            if not self.schedule.active(c.beam, event_utc):
                continue
            hits.append((c, event_utc))
        if not hits:
            return

        # One trigger per batch: the same pulse shows up in several DM/width
        # trials and neighbouring beams, so take the highest-SNR hit.
        best, event_utc = max(hits, key=lambda h: h[0].snr)
        logger.info("hit: snr=%.1f dm=%.2f beam=%d width=%d event=%s (%d hits in batch)",
                    best.snr, best.dm, best.beam, best.width,
                    event_utc.isoformat(timespec="milliseconds"), len(hits))

        reason = self.policy.check(now)
        if reason is not None:
            self._log(now, event_utc, best, "suppressed", reason)
            return
        if not self.enabled:
            self._log(now, event_utc, best, "dry_run", "")
            return
        self.policy.record(now)
        await self._trigger(best, event_utc, now)

    # ------------------------------------------------------------ triggering

    async def _trigger(self, c: wire.Candidate, event_utc: datetime, now: datetime) -> None:
        stream = beams.stream_for_beam(c.beam)
        loc = beams.stream_location(stream)
        pre = self.cfg["trigger"].get("pre_s", 2.0)
        post = self.cfg["trigger"].get("post_s", 2.0)
        start = event_utc - timedelta(seconds=pre)
        stop = event_utc + timedelta(
            seconds=timing.dispersion_sweep_s(c.dm) + c.width * timing.TSAMP_S + post)

        try:
            reply = await request_dump_async(loc.host, loc.control_port, start, stop)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.error("dump request to %s:%d failed: %s", loc.host, loc.control_port, exc)
            self._log(now, event_utc, c, "trigger_failed", str(exc))
            return

        action = "triggered" if reply == "OK" else "trigger_refused"
        self._log(now, event_utc, c, action, reply)
        if reply == "OK":
            # The dump is already requested; hold the trigger card back a few
            # seconds so context candidates from the other jobs' gulps (which
            # may lag this one) make it into the card before T3 reads it.
            task = asyncio.create_task(self._delayed_card(c, event_utc, start, stop, loc))
            self._card_tasks.add(task)
            task.add_done_callback(self._card_tasks.discard)

    async def _delayed_card(self, c: wire.Candidate, event_utc: datetime,
                            start: datetime, stop: datetime,
                            loc: beams.StreamLocation) -> None:
        await asyncio.sleep(self.ctx_delay_s)
        try:
            await self._write_trigger_card(c, event_utc, start, stop, loc)
        except Exception:
            logger.exception("trigger card write failed for beam %d", c.beam)

    def _collect_context(self, event_epoch: float) -> list[list]:
        """T1 candidates near the event, as [dt_s, beam, dm, snr, width] rows."""
        window = self.ctx_window_s
        sel = [m for m in self.context if abs(m[0] - event_epoch) <= window]
        sel.sort(key=lambda m: -m[3])
        del sel[self.ctx_max_members:]
        sel.sort(key=lambda m: m[0])
        return [[round(t - event_epoch, 4), b, round(dm, 3), round(snr, 2), w]
                for t, b, dm, snr, w in sel]

    async def _write_trigger_card(self, c: wire.Candidate, event_utc: datetime,
                                  start: datetime, stop: datetime,
                                  loc: beams.StreamLocation) -> None:
        candname = (f"{self.source.replace('+', 'p').lower()}_"
                    f"{event_utc.strftime('%Y%m%d_%H%M%S')}_b{c.beam:03d}")
        card = {
            "candname": candname,
            "source": self.source,
            "event_utc": event_utc.isoformat(timespec="milliseconds"),
            "beam": c.beam,
            "local_beam": beams.local_beam(c.beam),
            "stream": loc.stream,
            "snr": c.snr,
            "dm": c.dm,
            "width": c.width,
            "samp": c.samp,
            "dump_utc_start": timing.format_dada_utc(start),
            "dump_utc_stop": timing.format_dada_utc(stop),
            "dump_dir": loc.dump_dir,
            "context": {
                "window_s": self.ctx_window_s,
                "members": self._collect_context(event_utc.timestamp()),
            },
        }
        body = json.dumps(card, indent=2)
        fname = f"{candname}.json"

        if loc.host == LOCAL_HOSTNAME:
            Path(loc.spool_dir).mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=loc.spool_dir, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.rename(tmp, Path(loc.spool_dir) / fname)
        else:
            # Remote stream: push the card over ssh. Small file, best effort.
            proc = await asyncio.create_subprocess_exec(
                "ssh", loc.host,
                f"mkdir -p {loc.spool_dir} && cat > {loc.spool_dir}/{fname}.tmp "
                f"&& mv {loc.spool_dir}/{fname}.tmp {loc.spool_dir}/{fname}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate(body.encode())
            if proc.returncode != 0:
                logger.error("failed to push trigger card to %s: %s",
                             loc.host, err.decode(errors="replace"))
                return
        logger.info("trigger card %s -> %s:%s", candname, loc.host, loc.spool_dir)

    # ------------------------------------------------------------- logging

    def _log(self, recv_utc: datetime, event_utc: datetime, c: wire.Candidate,
             action: str, detail: str) -> None:
        stream = beams.stream_for_beam(c.beam)
        with open(self.log_path, "a", newline="") as f:
            csv.writer(f).writerow([
                recv_utc.isoformat(timespec="milliseconds"),
                event_utc.isoformat(timespec="milliseconds"),
                c.beam, stream, f"{c.snr:.2f}", f"{c.dm:.3f}", c.width, c.samp,
                action, detail,
            ])


def main() -> None:
    p = argparse.ArgumentParser(description="Watch hella candidates for a known source")
    p.add_argument("config", help="YAML config file")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    watch = SourceWatch(cfg)
    try:
        asyncio.run(watch.serve())
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
