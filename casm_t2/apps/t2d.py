"""T2 daemon: ingest, cluster, classify, trigger, record.

The always-on heart of T2. Owns the eight TCP ports hella publishes to,
coalesces the jobs' batches per gulp, clusters each gulp with DBSCAN, and
runs every cluster through the decision chain:

    injection match -> beam veto -> wide-beam RFI cut -> known-source match
    -> SNR tier -> trigger budgets + disk guard -> dump + trigger card

Latency note: the dump ring only reaches ~20 s back and T1 itself reports
20-24 s after the pulse, so dump triggering CANNOT wait for clustering.
A fast path evaluates trigger-worthy candidates per batch the moment they
arrive (cheap per-trial thresholds + injection/veto checks) and fires the
dump immediately; the clustering path then recognises the same event,
reuses its name, back-fills cluster_id on the trigger row, and enriches
the delayed trigger card. The slow (post-cluster) trigger path remains as
a fallback and audit trail for anything the fast path skipped.

Everything is recorded in the SQLite event DB: tiered clusters (with their
YYMMDDxxxx event names), per-gulp funnel statistics, and a full audit row
for every trigger decision including refusals and their reasons.

Dumps are deliberately scarce (disks are nearly full): per-kind token
buckets cap intensity and voltage dumps per day, and any trigger whose
target filesystem is low on space is refused outright. Injections are
matched against the ledger and never trigger anything.

All tunables live in one YAML config (config/t2d.yaml). `--shadow` runs the
full chain without sending dump commands or writing trigger cards.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import socket
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from casm_t2 import beams, cluster, db, events, known_source, logsetup, policy, timing, wire
from casm_t2.dump_client import request_dump_async, request_voltage_dump_async

logger = logging.getLogger("t2d")

LOCAL_HOSTNAME = socket.gethostname().split(".")[0]


class DiskMonitor:
    """Cached free-space checks for the dump filesystems on both nodes.

    The local node is checked synchronously via statvfs; remote nodes are
    polled over ssh on a timer. Unknown state fails CLOSED — a dump is
    refused rather than risked onto a possibly-full disk.
    """

    def __init__(self, floor_gb: float, remote_hosts: set[str], probe_path: str):
        self.floor_gb = floor_gb
        self.probe_path = probe_path
        self.remote_free: dict[str, tuple[float, float]] = {}  # host -> (mono_ts, GB)
        self._hosts = remote_hosts

    async def poll_remotes(self) -> None:
        while True:
            for host in self._hosts:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", host, f"df --output=avail -B1 {self.probe_path} | tail -1",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                    self.remote_free[host] = (time.monotonic(), int(out.strip()) / 1e9)
                except (OSError, ValueError, asyncio.TimeoutError) as exc:
                    logger.warning("disk poll of %s failed: %s", host, exc)
            await asyncio.sleep(120)

    def refusal(self, host: str, path: str) -> str | None:
        if host == LOCAL_HOSTNAME:
            return policy.disk_refusal(path, self.floor_gb)
        ts_free = self.remote_free.get(host)
        if ts_free is None or time.monotonic() - ts_free[0] > 600:
            return "disk_unknown_remote"
        if ts_free[1] < self.floor_gb:
            return f"disk_low:{ts_free[1]:.0f}GB<{self.floor_gb:.0f}GB"
        return None


class T2Daemon:
    def __init__(self, cfg: dict, shadow: bool):
        self.cfg = cfg
        self.shadow = shadow
        cc = cfg.get("cluster", {})
        self.params = cluster.ClusterParams(
            eps=cc.get("eps", 1.0), min_samples=cc.get("min_samples", 5),
            samp_scale=cc.get("samp_scale", 64.0),
            dm_idx_scale=cc.get("dm_idx_scale", 32.0),
            width_scale=cc.get("width_scale", 2.0),
            beam_scale=cc.get("beam_scale", 4.0))

        tiers = cfg.get("tiers", {})
        self.tier_a = tiers.get("A", 30.0)
        self.tier_b = tiers.get("B", 15.0)
        self.tier_c = tiers.get("C", 12.0)
        self.store_min_snr = cfg.get("store_min_snr", self.tier_c)

        filt = cfg.get("filters", {})
        self.veto = set(filt.get("beam_veto", []))
        self.max_nbeam = filt.get("max_nbeam", 32)
        self.dm_floor = filt.get("dm_floor", 20.0)

        trig = cfg.get("trigger", {})
        icfg = trig.get("intensity", {})
        vcfg = trig.get("voltage", {})
        self.budget_int = policy.TriggerBudget(icfg.get("min_spacing_s", 120),
                                               icfg.get("daily_max", 20))
        self.budget_vol = policy.TriggerBudget(vcfg.get("min_spacing_s", 600),
                                               vcfg.get("daily_max", 2))
        self.voltage_enabled = bool(vcfg.get("enabled", False))
        self.voltage_tier = vcfg.get("tier", "A")
        self.pre_s = trig.get("pre_s", 2.0)
        self.post_s = trig.get("post_s", 2.0)
        # Strict mode (fast_path: false) waits for clustering before any
        # dump — DSA-110 style. Misses from the ring window expiring are
        # then deliberate and audited, the data that argues for a deeper
        # intensity ring. Flip back on if the latency budget tightens.
        self.fast_path = bool(trig.get("fast_path", True))
        self.disk = DiskMonitor(trig.get("disk_floor_gb", 200.0),
                                set(beams.STREAM_HOSTS.values()) - {LOCAL_HOSTNAME},
                                beams.CAND_BEAM_DUMP_DIR)

        self.sources = known_source.load_sources(cfg.get("known_sources", []))
        # known-source triggers may sit below tier C; each block carries its
        # own snr_min (default 11).
        self.source_snr_min = {b["name"]: b.get("snr_min", 11.0)
                               for b in cfg.get("known_sources", [])}

        ctx = cfg.get("context", {})
        self.ctx_window_s = ctx.get("window_s", 4.0)
        self.ctx_delay_s = ctx.get("delay_s", 8.0)
        self.ctx_max_members = ctx.get("max_members", 3000)
        self.context: deque[tuple[float, int, float, float, int]] = deque(maxlen=400_000)

        self.conn = db.connect(cfg.get("db", db.DEFAULT_PATH))
        self.pending: dict[tuple, list[wire.Candidate]] = defaultdict(list)
        self.pending_jobs: dict[tuple, int] = {}
        self.tasks: set[asyncio.Task] = set()
        self.n_batches = self.n_cands = self.n_clusters = self.n_triggers = 0
        # fast-path bookkeeping: recently fired fast triggers awaiting their
        # cluster (name -> (event_epoch, beam, dm)), plus a dedup clock so a
        # bright event spread over several jobs' batches fires only once.
        self.pending_fast: dict[str, tuple[float, int, float]] = {}
        self._last_fast_mono = 0.0
        # injection ledger cache, refreshed per gulp from the DB
        self._inj_cache: list[tuple[float, int, float]] = []  # (epoch, beam, dm)
        self._inj_cache_ts = 0.0

    # ------------------------------------------------------------- ingest

    async def serve(self) -> None:
        host = self.cfg.get("listen_host", "0.0.0.0")
        ports = self.cfg.get("ports", list(range(12345, 12353)))
        servers = []
        for job, port in enumerate(ports):
            handler = functools.partial(self._handle, job=job)
            servers.append(await asyncio.start_server(handler, host, port))
            logger.info("listening on %s:%d", host, port)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._heartbeat())
            tg.create_task(self.disk.poll_remotes())
            for s in servers:
                tg.create_task(s.serve_forever())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      job: int = 0) -> None:
        try:
            payload = await asyncio.wait_for(reader.read(-1), timeout=30)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            logger.warning("ingest connection error: %s", exc)
            return
        finally:
            writer.close()
        batch = wire.parse_batch(payload.decode(errors="replace"))
        self.n_batches += 1
        self.n_cands += len(batch.cands)
        # empty batches still register in the coalescer so all-quiet gulps
        # get a gulp_stats row (duty cycle = observed time, not busy time)
        if batch.cands and batch.utc_start is not None:
            epoch = timing.parse_dada_utc(batch.utc_start).timestamp()
            tsamp = batch.tsamp_s or timing.TSAMP_S
            for c in batch.cands:
                self.context.append((epoch + c.samp * tsamp, c.beam, c.dm, c.snr, c.width))
        if self.fast_path and batch.cands:
            self._spawn(self._fast_path(batch))
        key = (batch.utc_start, batch.gulp)
        first = key not in self.pending
        self.pending[key].extend(batch.cands)
        self.pending_jobs[key] = self.pending_jobs.get(key, 0) + 1
        if first:
            self._spawn(self._flush_later(key))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _flush_later(self, key: tuple) -> None:
        await asyncio.sleep(self.cfg.get("coalesce_s", 2.0))
        cands = self.pending.pop(key, [])
        n_jobs = self.pending_jobs.pop(key, 0)
        if not cands:
            # quiet gulp: no candidates from any job, but it was observed
            utc_start_s, gulp = key
            gulp_utc = ""
            if utc_start_s:
                utc_start = timing.parse_dada_utc(utc_start_s)
                gulp_utc = timing.samp_to_utc(gulp * 8192, utc_start).isoformat(
                    timespec="milliseconds")
            db.insert_gulp_stats(self.conn, utc_start_s or "", gulp, gulp_utc,
                                 n_jobs, 0, 0, 0, 0, 0.0)
            return
        t0 = time.monotonic()
        clusters = await asyncio.to_thread(cluster.cluster_candidates, cands, self.params)
        dt = time.monotonic() - t0
        try:
            await self._process(key, clusters, n_jobs, len(cands), dt * 1e3)
        except Exception:
            logger.exception("processing gulp %s failed", key)

    # ------------------------------------------------------------ fast path

    def _cand_injection_match(self, epoch: float, beam: int, dm: float) -> bool:
        for inj_epoch, inj_beam, inj_dm in self._inj_cache:
            if (abs(epoch - inj_epoch) <= 60 and abs(beam - inj_beam) <= 2
                    and abs(dm - inj_dm) <= max(0.1 * inj_dm, 5)):
                return True
        return False

    async def _fast_path(self, batch: wire.Batch) -> None:
        """Per-batch trigger evaluation, ~1 s after T1 reports."""
        if batch.utc_start is None:
            return
        utc_start = timing.parse_dada_utc(batch.utc_start)
        tsamp = batch.tsamp_s or timing.TSAMP_S
        self._refresh_injections()

        best = None
        for c in batch.cands:
            if c.beam in self.veto:
                continue
            event_utc = timing.samp_to_utc(c.samp, utc_start, tsamp)
            reason = None
            for src in self.sources:
                if (src.dm_min <= c.dm <= src.dm_max
                        and c.snr >= self.source_snr_min.get(src.name, 11.0)
                        and src.active(c.beam, event_utc)):
                    reason = f"known_source:{src.name}"
                    break
            if reason is None and c.snr >= self.tier_b and c.dm >= self.dm_floor:
                reason = "tier_A" if c.snr >= self.tier_a else "tier_B"
            if reason is None:
                continue
            # injections are never dumped: CAND_DUMP_BLOCK 0 is upstream of
            # the injection merge, so a dump cannot contain the pulse anyway;
            # the gallery renders truth plots from the generated .fil instead
            if self._cand_injection_match(event_utc.timestamp(), c.beam, c.dm):
                continue
            if best is None or c.snr > best[0].snr:
                best = (c, event_utc, reason)
        if best is None:
            return
        if time.monotonic() - self._last_fast_mono < 60:
            return  # one fast attempt per minute; budgets do the real limiting
        self._last_fast_mono = time.monotonic()

        c, event_utc, reason = best
        name = events.new_event_name(self.conn, event_utc)
        self.pending_fast[name] = (event_utc.timestamp(), c.beam, c.dm)
        pseudo = cluster.Cluster(peak=c, n_members=1, n_beams=1,
                                 beam_lo=c.beam, beam_hi=c.beam, dm_lo=c.dm,
                                 dm_hi=c.dm, samp_lo=c.samp, samp_hi=c.samp)
        tier = ("A" if c.snr >= self.tier_a else
                "B" if c.snr >= self.tier_b else "C")
        await self._trigger(pseudo, name, event_utc, tier, f"fast:{reason}", None)

    def _match_fast(self, cl: cluster.Cluster, event_epoch: float) -> str | None:
        """Name of a pending fast trigger this cluster corresponds to."""
        for name, (epoch, beam, dm) in self.pending_fast.items():
            if (abs(event_epoch - epoch) <= 30
                    and cl.beam_lo - 2 <= beam <= cl.beam_hi + 2
                    and cl.dm_lo - 5 <= dm <= cl.dm_hi + 5):
                return name
        return None

    # ----------------------------------------------------------- decisions

    def _refresh_injections(self) -> None:
        if time.monotonic() - self._inj_cache_ts < 30:
            return
        self._inj_cache_ts = time.monotonic()
        since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
            timespec="milliseconds")
        self._inj_cache = [
            (datetime.fromisoformat(r[0]).timestamp(), int(r[1]), float(r[2]))
            for r in self.conn.execute(
                "SELECT inject_utc, beam, dm FROM injections WHERE inject_utc >= ?",
                (since,))]

    def _injection_match(self, cl: cluster.Cluster, event_epoch: float) -> bool:
        for inj_epoch, inj_beam, inj_dm in self._inj_cache:
            if (abs(event_epoch - inj_epoch) <= 60
                    and cl.beam_lo - 2 <= inj_beam <= cl.beam_hi + 2
                    and cl.dm_lo - max(0.1 * inj_dm, 5) <= inj_dm
                    and inj_dm <= cl.dm_hi + max(0.1 * inj_dm, 5)):
                return True
        return False

    def _classify(self, cl: cluster.Cluster, event_utc: datetime | None) -> tuple[str, list[str]]:
        tags = []
        if event_utc is not None and self._injection_match(cl, event_utc.timestamp()):
            tags.append("injection")
        if cl.peak.beam in self.veto:
            tags.append("veto")
        if cl.n_beams > self.max_nbeam:
            tags.append("rfi_wide")
        if event_utc is not None:
            for src in self.sources:
                if src.matches(cl, event_utc):
                    tags.append(f"src:{src.name}")
                    break
        snr = cl.peak.snr
        tier = ("A" if snr >= self.tier_a else
                "B" if snr >= self.tier_b else
                "C" if snr >= self.tier_c else "-")
        return tier, tags

    def _wants_trigger(self, cl: cluster.Cluster, tier: str, tags: list[str]) -> str | None:
        """Why this cluster deserves a dump, or None."""
        if any(t in ("injection", "veto", "rfi_wide") for t in tags):
            return None
        src = next((t[4:] for t in tags if t.startswith("src:")), None)
        if src is not None and cl.peak.snr >= self.source_snr_min.get(src, 11.0):
            return f"known_source:{src}"
        if tier in ("A", "B") and cl.peak.dm >= self.dm_floor:
            return f"tier_{tier}"
        return None

    async def _process(self, key: tuple, clusters: list[cluster.Cluster],
                       n_jobs: int, n_cands: int, clustering_ms: float) -> None:
        utc_start_s, gulp = key
        utc_start = timing.parse_dada_utc(utc_start_s) if utc_start_s else None
        self._refresh_injections()

        # expire stale fast-trigger entries (cluster never showed up)
        now_ts = datetime.now(timezone.utc).timestamp()
        for name in [n for n, (e, _, _) in self.pending_fast.items()
                     if now_ts - e > 120]:
            self.pending_fast.pop(name, None)

        rows, to_trigger, fast_names = [], [], []
        for cl in clusters:
            event_utc = (timing.samp_to_utc(cl.peak.samp, utc_start)
                         if utc_start else None)
            tier, tags = self._classify(cl, event_utc)
            reason = self._wants_trigger(cl, tier, tags)
            fast_name = (self._match_fast(cl, event_utc.timestamp())
                         if event_utc else None)
            store = (tier != "-" or reason is not None or "injection" in tags
                     or fast_name is not None)
            if not store:
                continue
            if fast_name is not None:
                # the fast path already triggered and named this event
                name = self.pending_fast.pop(fast_name, None) and fast_name
                tags.append("fast_triggered")
                fast_names.append(name)
                reason = None
            else:
                name = events.new_event_name(self.conn,
                                             event_utc or datetime.now(timezone.utc))
            ev_iso = event_utc.isoformat(timespec="milliseconds") if event_utc else ""
            rows.append((cl, utc_start_s or "", gulp, ev_iso, tier, ",".join(tags), name))
            if reason is not None and event_utc is not None:
                to_trigger.append((cl, name, event_utc, tier, reason))

        ids = db.insert_clusters(self.conn, rows) if rows else []
        id_by_name = {row[6]: cid for row, cid in zip(rows, ids)}
        self.n_clusters += len(rows)
        for name in fast_names:
            if name in id_by_name:
                with self.conn:
                    self.conn.execute(
                        "UPDATE triggers SET cluster_id = ? WHERE candname = ?"
                        " AND cluster_id IS NULL", (id_by_name[name], name))

        gulp_utc = (timing.samp_to_utc(min(c.samp_lo for c in clusters), utc_start)
                    .isoformat(timespec="milliseconds") if clusters and utc_start else "")
        db.insert_gulp_stats(self.conn, utc_start_s or "", gulp, gulp_utc, n_jobs,
                             n_cands, len(clusters), len(rows), len(to_trigger),
                             clustering_ms)

        # One dump per gulp: the same physical event can fragment into a few
        # clusters; fire only the strongest and audit the rest, so duplicates
        # never burn the daily budget.
        to_trigger.sort(key=lambda t: -t[0].peak.snr)
        for i, (cl, name, event_utc, tier, reason) in enumerate(to_trigger):
            if i == 0:
                await self._trigger(cl, name, event_utc, tier, reason,
                                    id_by_name.get(name))
            else:
                db.insert_trigger(self.conn, id_by_name.get(name), name,
                                  beams.stream_for_beam(cl.peak.beam),
                                  "suppressed", f"{reason};gulp_dup")

    # ----------------------------------------------------------- triggering

    async def _trigger(self, cl: cluster.Cluster, name: str, event_utc: datetime,
                       tier: str, reason: str, cluster_id: int | None) -> None:
        c = cl.peak
        stream = beams.stream_for_beam(c.beam)
        loc = beams.stream_location(stream)
        start = event_utc - timedelta(seconds=self.pre_s)
        stop = event_utc + timedelta(
            seconds=timing.dispersion_sweep_s(c.dm) + (2 ** c.width) * timing.TSAMP_S + self.post_s)
        now = datetime.now(timezone.utc)
        start_s, stop_s = timing.format_dada_utc(start), timing.format_dada_utc(stop)

        logger.info("trigger candidate %s (%s): snr=%.1f dm=%.2f beam=%d nbeam=%d",
                    name, reason, c.snr, c.dm, c.beam, cl.n_beams)

        if self.shadow:
            db.insert_trigger(self.conn, cluster_id, name, stream, "shadow", reason,
                              dump_utc_start=start_s, dump_utc_stop=stop_s)
            return

        budget = self.budget_int
        refusal = budget.check(now) or self.disk.refusal(loc.host, loc.dump_dir)
        if refusal:
            logger.warning("intensity trigger %s refused: %s", name, refusal)
            db.insert_trigger(self.conn, cluster_id, name, stream, "refused",
                              f"{reason};{refusal}")
        else:
            try:
                reply = await request_dump_async(loc.host, loc.control_port, start, stop)
            except (OSError, asyncio.TimeoutError) as exc:
                db.insert_trigger(self.conn, cluster_id, name, stream, "failed",
                                  f"{reason};{exc}")
            else:
                action = "triggered" if reply == "OK" else "refused_daemon"
                db.insert_trigger(self.conn, cluster_id, name, stream, action,
                                  f"{reason};{reply}", dump_utc_start=start_s,
                                  dump_utc_stop=stop_s)
                if reply == "OK":
                    budget.record(now)
                    self._spawn(self._delayed_card(cl, name, event_utc,
                                                   start_s, stop_s, loc, reason))

        if self.voltage_enabled and tier <= self.voltage_tier:
            v_refusal = self.budget_vol.check(now)
            if v_refusal:
                db.insert_trigger(self.conn, cluster_id, name, -1, "refused",
                                  f"{reason};voltage_{v_refusal}", kind="voltage")
            else:
                replies = await request_voltage_dump_async(start, stop)
                ok = sum(1 for r in replies.values() if r == "OK")
                if ok:
                    self.budget_vol.record(now)
                db.insert_trigger(self.conn, cluster_id, name, -1,
                                  "triggered" if ok == len(replies) else "partial",
                                  f"{reason};{json.dumps(replies)}", kind="voltage",
                                  dump_utc_start=start_s, dump_utc_stop=stop_s)

    # ---------------------------------------------------------- trigger card

    def _collect_context(self, event_epoch: float) -> list[list]:
        sel = [m for m in self.context if abs(m[0] - event_epoch) <= self.ctx_window_s]
        sel.sort(key=lambda m: -m[3])
        del sel[self.ctx_max_members:]
        sel.sort(key=lambda m: m[0])
        return [[round(t - event_epoch, 4), b, round(dm, 3), round(snr, 2), w]
                for t, b, dm, snr, w in sel]

    async def _delayed_card(self, cl: cluster.Cluster, name: str, event_utc: datetime,
                            start_s: str, stop_s: str, loc: beams.StreamLocation,
                            reason: str) -> None:
        # Hold the card back so context candidates from lagging jobs arrive.
        await asyncio.sleep(self.ctx_delay_s)
        c = cl.peak
        # fast-path cards start as single trials; by now the cluster row
        # usually exists, so take the envelope numbers from it.
        row = self.conn.execute("SELECT n_members, n_beams FROM clusters"
                                " WHERE name = ?", (name,)).fetchone()
        n_members, n_beams = row if row else (cl.n_members, cl.n_beams)
        card = {
            "candname": name,
            "source": reason.split(":", 1)[1] if reason.startswith("known_source") else "blind",
            "event_utc": event_utc.isoformat(timespec="milliseconds"),
            "beam": c.beam,
            "local_beam": beams.local_beam(c.beam),
            "stream": loc.stream,
            "snr": c.snr,
            "dm": c.dm,
            "width": c.width,
            "samp": c.samp,
            "n_members": n_members,
            "n_beams": n_beams,
            "trigger_reason": reason,
            "dump_utc_start": start_s,
            "dump_utc_stop": stop_s,
            "dump_dir": loc.dump_dir,
            "context": {
                "window_s": self.ctx_window_s,
                "members": self._collect_context(event_utc.timestamp()),
            },
        }
        body = json.dumps(card, indent=2)
        fname = f"{name}.json"
        try:
            if loc.host == LOCAL_HOSTNAME:
                Path(loc.spool_dir).mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=loc.spool_dir, suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    f.write(body)
                os.rename(tmp, Path(loc.spool_dir) / fname)
            else:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", loc.host,
                    f"mkdir -p {loc.spool_dir} && cat > {loc.spool_dir}/{fname}.tmp "
                    f"&& mv {loc.spool_dir}/{fname}.tmp {loc.spool_dir}/{fname}",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate(body.encode())
                if proc.returncode != 0:
                    raise OSError(err.decode(errors="replace"))
        except Exception:
            logger.exception("trigger card %s write failed", name)
            return
        logger.info("trigger card %s -> %s:%s", name, loc.host, loc.spool_dir)

    # ------------------------------------------------------------- heartbeat

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(60)
            n_trig = self.conn.execute(
                "SELECT count(*) FROM triggers WHERE created_utc >= ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
                    timespec="milliseconds"),)).fetchone()[0]
            logger.info("heartbeat: %d batches, %d cands -> %d clusters stored, "
                        "%d trigger decisions (last minute)%s",
                        self.n_batches, self.n_cands, self.n_clusters, n_trig,
                        " [SHADOW]" if self.shadow else "")
            self.n_batches = self.n_cands = self.n_clusters = 0


def main() -> None:
    p = argparse.ArgumentParser(description="T2 clustering + trigger daemon")
    p.add_argument("config", nargs="?",
                   default="/home/casm/software/dev/casm_t2/config/t2d.yaml")
    p.add_argument("--shadow", action="store_true",
                   help="full chain but no dumps, no trigger cards")
    p.add_argument("--log-file", default="/mnt/nvme5/casm_pipeline/logs/t2d.out")
    args = p.parse_args()

    logsetup.setup(args.log_file)
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    shadow = args.shadow or cfg.get("shadow", False)

    daemon = T2Daemon(cfg, shadow=shadow)
    logger.info("t2d starting%s: db=%s, voltage=%s",
                " in SHADOW mode" if shadow else "",
                cfg.get("db", db.DEFAULT_PATH),
                "enabled" if daemon.voltage_enabled else "disabled")
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
