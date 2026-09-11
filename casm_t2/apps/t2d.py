"""T2 daemon: ingest, cluster, classify, trigger, record.

Owns the eight TCP ports hella publishes to, coalesces the jobs' batches per
gulp, clusters each gulp with DBSCAN, and runs every cluster through the
decision chain:

    injection match -> beam veto -> wide-beam RFI cut -> beam-occupancy
    veto -> known-source match -> SNR tier -> trigger budgets + disk guard
    -> dump + trigger card

The dump ring reaches only ~20 s back and T1 reports 20-24 s after the pulse.
With ``trigger.fast_path`` on, trigger-worthy candidates are evaluated per batch
on cheap per-trial thresholds and the dump fires at once; the clustering path
then recognises the same event, reuses its name, back-fills cluster_id and
enriches the trigger card. The post-cluster path is the fallback and the audit
trail. Strict cluster-first (fast_path off) records ring-window misses as
refused_daemon instead.

Everything lands in the SQLite event DB: tiered clusters with their event names,
per-gulp funnel statistics, and an audit row for every trigger decision
including refusals. Per-kind token buckets cap intensity and voltage dumps per
day and any trigger whose target filesystem is low is refused. Injections are
matched against the ledger and never trigger.

Storm defences, both counted per gulp in gulp_stats: ``veto_widths`` drops whole
boxcar-width indices at parse time, ``max_cands_per_gulp`` bounds what reaches
DBSCAN with a per-beam S/N quota then a global truncation with a per-beam floor.
Without them a storm gulp of 80,000 trials takes DBSCAN 83-137 s against an
8.7 s real-time budget and ingest stalls.

All tunables live in config/t2d.yaml. ``--shadow`` runs the full chain without
dump commands or trigger cards; ``dumps_enabled: false`` is the same
suppression as a persistent key, recorded as 'suppressed_commissioning'.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import dataclasses
import functools
import heapq
import json
import logging
import math
import os
import socket
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from casm_t2 import (beams, cluster, db, events, known_source, logsetup, policy, weights_registry,
                     sdnotify, timing, wire)
from casm_t2.dump_client import request_dump_async, request_voltage_dump_async

logger = logging.getLogger("t2d")

LOCAL_HOSTNAME = socket.gethostname().split(".")[0]

# Stage-1 per-beam quota when the storm cap trips, as a fraction of the cap:
# quota = ceil(max_cands_per_gulp / BEAM_QUOTA_DIVISOR).
BEAM_QUOTA_DIVISOR = 64

# Stage-2 floor: candidates every populated beam keeps regardless of its global
# rank, so a bright storm elsewhere cannot evict a single-beam FRB.
BEAM_FLOOR = 4


def apply_width_veto(cands: list[wire.Candidate],
                     veto_widths: set[int] | list[int]) -> tuple[list[wire.Candidate], int]:
    """Drop candidates whose boxcar width index is vetoed.

    Width index 6 (67 ms) is 97-98.6% of a quiet day's stored rows, red-noise
    junk at DM >= 200 that pays full DBSCAN cost and triggers nothing. An empty
    veto list returns the input untouched. Returns (kept, n_vetoed).
    """
    if not veto_widths:
        return cands, 0
    keep = [c for c in cands if c.width not in veto_widths]
    return keep, len(cands) - len(keep)


def apply_storm_cap(cands: list[wire.Candidate], max_cands: int,
                    beam_floor: int = BEAM_FLOOR) -> tuple[list[wire.Candidate], int]:
    """Shed a runaway gulp down to a bound on what DBSCAN will see.

    Nothing is shed at or below ``max_cands``. Above it, two stages:

    Stage 1, per-beam quota. Each global beam keeps its top
    ``ceil(max_cands / 64)`` by S/N, which handles RFI concentrated in a few
    beams and keeps the shed fair across the sky.

    Stage 2, global truncation with a floor. Stage 1 alone allows
    quota x populated beams (313 x 512 at the shipped cap), so a storm spread
    over every beam passes untouched. If stage 1 leaves more than
    ``max_cands``, keep the global top ``max_cands`` by S/N, then add back each
    populated beam's top ``beam_floor`` so a storm elsewhere cannot evict a
    single-beam FRB. Worst case kept is ``max_cands + beam_floor x 512``.

    Returns (kept, n_shed), survivors in input order. ``max_cands <= 0``
    disables the cap.
    """
    if max_cands <= 0 or len(cands) <= max_cands:
        return cands, 0

    def by_beam(idxs: list[int]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in idxs:
            out[cands[i].beam].append(i)
        return out

    def top(idxs: list[int], n: int) -> list[int]:
        if len(idxs) <= n:
            return idxs
        return heapq.nlargest(n, idxs, key=lambda i: cands[i].snr)

    quota = max(1, math.ceil(max_cands / BEAM_QUOTA_DIVISOR))
    survivors: list[int] = []
    for idxs in by_beam(range(len(cands))).values():
        survivors.extend(top(idxs, quota))

    if len(survivors) > max_cands:
        keep_idx = set(top(survivors, max_cands))
        for idxs in by_beam(survivors).values():
            keep_idx.update(top(idxs, beam_floor))
        survivors = list(keep_idx)

    keep = [cands[i] for i in sorted(survivors)]
    return keep, len(cands) - len(keep)


def build_beam_footprint(
        cands: list[wire.Candidate]) -> tuple[list[int], list[int]]:
    """(samples sorted ascending, beams in the same order) for one gulp.

    Built from the raw candidates before the storm cap sheds anything, so a junk
    event's beam footprint survives the shed. hella-side truncation is the
    remaining blind spot: the production binary stops searching a gulp at its
    own 10k cap, so capped storm gulps arrive with a 1-beam footprint (see
    casm-wiki hella-t1-saturation.md).
    """
    order = sorted(range(len(cands)), key=lambda i: cands[i].samp)
    return ([cands[i].samp for i in order], [cands[i].beam for i in order])


def occupancy_beams(samps: list[int], beam_by_samp: list[int],
                    peak_samp: int, window_samp: int) -> int:
    """Distinct beams with any candidate within +-window_samp of peak_samp.

    The zero-DM junk discriminator: real sources occupy 0-2 beams, broadband
    junk 40-64. Counted on raw candidates, DBSCAN fragmenting junk into
    single-beam clusters. See casm-wiki hella-sigma.md.
    """
    lo = bisect.bisect_left(samps, peak_samp - window_samp)
    hi = bisect.bisect_right(samps, peak_samp + window_samp)
    return len(set(beam_by_samp[lo:hi]))


class DiskMonitor:
    """Cached free-space checks for the dump filesystems on both nodes.

    The local node is checked synchronously via statvfs, remote nodes over ssh
    on a timer. Unknown state refuses the dump.
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
            beam_fwhm_x_deg=cc.get("beam_fwhm_x_deg", 18.1),
            beam_fwhm_y_deg=cc.get("beam_fwhm_y_deg", 3.9),
            beam_scale=cc.get("beam_scale", 4.0))
        # Pointing tables, one per weights product, built lazily and reused.
        # Weights change days apart, so a gulp costs a registry lookup and a
        # dict hit.
        self._sky_tables: dict[str, cluster.SkyTable] = {}
        # Cluster params per weights product. The sky link scales are the beam
        # ellipse of the live weights, off the registry product when it carries
        # one and from config otherwise.
        self._sky_params: dict[str, cluster.ClusterParams] = {}
        # injection matching uses sky neighbours; warn once if it cannot
        self._inj_index_warned = False
        self.injection_match_scale = float(
            (cfg.get('clustering', {}) or {}).get('injection_match_scale', 1.0))

        tiers = cfg.get("tiers", {})
        self.tier_a = tiers.get("A", 30.0)
        self.tier_b = tiers.get("B", 15.0)
        self.tier_c = tiers.get("C", 12.0)
        self.store_min_snr = cfg.get("store_min_snr", self.tier_c)

        filt = cfg.get("filters", {})
        self.veto = set(filt.get("beam_veto", []))
        self.max_nbeam = filt.get("max_nbeam", 32)
        # Sky-extent RFI cut, degrees. n_beams is a count, not a footprint: on
        # the non-sky-ordered grid a burst across 20 scattered beams never
        # reaches max_nbeam. A real source spans about one E-W beam width plus a
        # beam spacing. 0 disables.
        self.max_sky_extent_deg = float(filt.get("max_sky_extent_deg", 25.0))
        self.dm_floor = filt.get("dm_floor", 20.0)
        # DM-floor veto: a bright zero-DM impulse of 10-20 ms still gives
        # S/N ~20 at the lowest DM trial, and hella never searches below DM_MIN
        # so it cannot see that DM 0 fits better. A cluster whose dm_lo sits at
        # the floor with a wide boxcar is that leak.
        dfv = filt.get("dm_floor_veto") or {}
        self.dm_floor_veto_max_dm_lo = float(dfv.get("max_dm_lo", 0.0))
        self.dm_floor_veto_min_width = int(dfv.get("min_width", 4))
        # Beam-occupancy veto: tag any cluster whose surrounding raw candidates
        # span >= min_beams distinct beams within +-window_samp. 0 disables.
        occ = cfg.get("occupancy", {}) or {}
        self.occ_min_beams = int(occ.get("min_beams", 0))
        self.occ_window_samp = int(occ.get("window_samp", 256))

        # Storm defences. The width veto discards real data, so it defaults
        # off; the storm cap is a liveness guard and defaults on.
        self.veto_widths = set(cfg.get("veto_widths", []))
        self.max_cands_per_gulp = int(cfg.get("max_cands_per_gulp", 20000))
        # Alternative to the two-stage cap: a gulp over the cap is dropped
        # whole (n_shed = everything), trading FRB-during-a-storm survival for
        # zero storm work.
        self.storm_skip_gulp = bool(cfg.get("storm_skip_gulp", False))
        # Kill-switch: evaluate and record every trigger decision, send no dump
        # command and write no trigger card.
        self.dumps_enabled = bool(cfg.get("dumps_enabled", True))

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
        # Storm lockout, seconds: a blind (tier) trigger also needs this much
        # blind-eligible quiet. Every eligible blind event restarts the clock
        # whether or not it dumped, so a storm gets one dump. Known-source
        # triggers are exempt, a pulsar train must not lock itself out.
        # 0 disables.
        self.storm_lockout_s = float(trig.get("storm_lockout_s", 0.0))
        self._last_blind_eligible: datetime | None = None
        # Strict mode (fast_path: false) waits for clustering before any dump.
        # Ring-window misses are then recorded rather than dodged.
        self.fast_path = bool(trig.get("fast_path", True))
        self.disk = DiskMonitor(trig.get("disk_floor_gb", 200.0),
                                set(beams.STREAM_HOSTS.values()) - {LOCAL_HOSTNAME},
                                beams.CAND_BEAM_DUMP_DIR)

        self.sources = known_source.load_sources(cfg.get("known_sources", []))
        # Known-source triggers may sit below tier C; each block carries its
        # own snr_min (default 11).
        self.source_snr_min = {b["name"]: b.get("snr_min", 11.0)
                               for b in cfg.get("known_sources", [])}

        ctx = cfg.get("context", {})
        self.ctx_window_s = ctx.get("window_s", 4.0)
        self.ctx_delay_s = ctx.get("delay_s", 8.0)
        self.ctx_max_members = ctx.get("max_members", 3000)
        self.context: deque[tuple[float, int, float, float, int]] = deque(maxlen=400_000)

        # Gulp coalescing. The eight hella jobs finish seconds apart, so a
        # fixed hold after the first batch splits a gulp into fragments and each
        # per-gulp veto sees only its own fragment. A key flushes when all
        # expected jobs have reported and the quiet hold has passed, or at
        # coalesce_max_s, whichever comes first.
        self.coalesce_s = float(cfg.get("coalesce_s", 0.25))
        self.coalesce_jobs = int(cfg.get(
            "coalesce_jobs", len(cfg.get("ports", list(range(12345, 12353))))))
        # One gulp length. A job over a gulp late is stuck and the dump ring
        # cannot absorb the wait: measured event-to-request lag is 36.5-50.8 s,
        # of which hella's own 13-25 s reporting delay is most.
        self.coalesce_max_s = float(cfg.get("coalesce_max_s", 8.0))

        self.conn = db.connect(cfg.get("db", db.DEFAULT_PATH))
        # Beam pointings come from the weights live at the event time
        # (casm_t2.weights_registry), never from a static table.
        self.registry = weights_registry.Registry(
            cfg.get("weights_registry", weights_registry.REGISTRY_DIR))
        self.pending: dict[tuple, list[wire.Candidate]] = defaultdict(list)
        self.pending_jobs: dict[tuple, int] = {}
        # Coalescer bookkeeping: when the key opened, when its last batch
        # landed, and the event a new batch sets to wake the waiter.
        self.pending_first: dict[tuple, float] = {}
        self.pending_last: dict[tuple, float] = {}
        self.pending_event: dict[tuple, asyncio.Event] = {}
        # Keys already flushed, with the monotonic flush time, so a batch
        # arriving afterwards is recognised and counted.
        self._flushed: dict[tuple, float] = {}
        self.n_late_batches = 0
        self.n_skipped_gulps = 0
        # Width-vetoed trials per coalescer key: filtered per batch in _handle,
        # reported per gulp by _flush_later.
        self.pending_vetoed: dict[tuple, int] = {}
        self.tasks: set[asyncio.Task] = set()
        self.n_batches = self.n_cands = self.n_clusters = self.n_triggers = 0
        # Fast-path bookkeeping: fast triggers awaiting their cluster
        # (name -> (event_epoch, beam, dm)), plus a dedup clock so an event
        # spread over several jobs' batches fires once.
        self.pending_fast: dict[str, tuple[float, int, float]] = {}
        self._last_fast_mono = 0.0
        # Rate-limiter state for the two log lines that can fire per gulp or
        # per connection during a storm.
        self._ingest_errors: dict[str, int] = {}
        self._n_storm_caps = 0
        # Injection ledger cache, refreshed per gulp from the DB.
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
        # Ready only once every port is bound. systemd starts the watchdog
        # clock from here (see _heartbeat).
        sdnotify.ready()
        sdnotify.status(f"listening on {len(servers)} ports")
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
            self._log_ingest_error(exc)
            return
        finally:
            writer.close()
        batch = wire.parse_batch(payload.decode(errors="replace"))
        self.n_batches += 1
        self.n_cands += len(batch.cands)   # raw arrivals, before any veto
        # Width veto at parse time, ahead of every consumer: the fast path
        # triggers per batch, so a later veto would still let junk fire a dump.
        # Vetoed trials also stay out of the trigger-card context deque.
        key = (batch.utc_start, batch.gulp)
        cands, n_vetoed = apply_width_veto(batch.cands, self.veto_widths)
        if n_vetoed:
            # Filtering is per batch, accounting per gulp: aggregate under the
            # coalescer key for _flush_later.
            self.pending_vetoed[key] = self.pending_vetoed.get(key, 0) + n_vetoed
        # Empty batches still register, so all-quiet gulps get a gulp_stats row
        # and the duty cycle counts observed time, not busy time.
        if cands and batch.utc_start is not None:
            epoch = timing.parse_dada_utc(batch.utc_start).timestamp()
            tsamp = batch.tsamp_s or timing.TSAMP_S
            for c in cands:
                self.context.append((epoch + c.samp * tsamp, c.beam, c.dm, c.snr, c.width))
        if self.fast_path and cands:
            self._spawn(self._fast_path(dataclasses.replace(batch, cands=cands)))
        now_mono = time.monotonic()
        first = key not in self.pending
        if first and key in self._flushed:
            # A ninth-or-later batch, or one after the coalesce timeout. It
            # opens a fresh pending entry under the same key and is processed as
            # its own fragment gulp. Logged, since a hella job was late enough
            # that the per-gulp vetoes did not see it.
            self.n_late_batches += 1
            logger.warning("late batch for gulp %s (job %d, %d candidates) "
                           "%.1f s after that gulp flushed; it becomes its own "
                           "fragment (occurrence %d)", key, job, len(cands),
                           now_mono - self._flushed[key], self.n_late_batches)
        self.pending[key].extend(cands)
        self.pending_jobs[key] = self.pending_jobs.get(key, 0) + 1
        self.pending_last[key] = now_mono
        if first:
            self.pending_first[key] = now_mono
            self.pending_event[key] = asyncio.Event()
            self._spawn(self._flush_later(key))
        else:
            self.pending_event[key].set()

    def _log_ingest_error(self, exc: BaseException) -> None:
        """Rate-limited ingest-error log: first of each kind, then every 100th.

        The exception class is logged because `str(asyncio.TimeoutError())` is
        empty, and an outage otherwise emits thousands of blank warnings.
        """
        kind = type(exc).__name__
        n = self._ingest_errors[kind] = self._ingest_errors.get(kind, 0) + 1
        if n == 1 or n % 100 == 0:
            logger.warning("ingest connection error: %s: %s (occurrence %d)",
                           kind, str(exc) or "<no detail>", n)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _wait_for_jobs(self, key: tuple) -> tuple[float, bool]:
        """Hold a coalescer key open until the gulp is complete, or time out.

        Returns (wait in seconds, whether all expected jobs reported). Two
        deadlines race: all ``coalesce_jobs`` reported plus ``coalesce_s`` of
        quiet since the last batch, or ``coalesce_max_s`` since the key opened.
        The waiter sleeps on an Event each later batch sets, so a slow gulp
        costs one wakeup per batch rather than a poll. Completeness is read at
        exit, a trickling gulp being able to reach the maximum wait with every
        job present.
        """
        ev = self.pending_event[key]
        t0 = self.pending_first[key]
        while True:
            now = time.monotonic()
            max_left = self.coalesce_max_s - (now - t0)
            if max_left <= 0:
                break
            quiet_left = self.coalesce_s - (now - self.pending_last[key])
            complete = self.pending_jobs.get(key, 0) >= self.coalesce_jobs
            if complete and quiet_left <= 0:
                break
            timeout = min(max_left, quiet_left) if quiet_left > 0 else max_left
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=max(timeout, 0.0))
            except asyncio.TimeoutError:
                pass
        return (time.monotonic() - t0,
                self.pending_jobs.get(key, 0) >= self.coalesce_jobs)

    async def _flush_later(self, key: tuple) -> None:
        waited, complete = await self._wait_for_jobs(key)
        coalesce_wait_ms = waited * 1e3
        cands = self.pending.pop(key, [])
        n_jobs = self.pending_jobs.pop(key, 0)
        self.pending_first.pop(key, None)
        self.pending_last.pop(key, None)
        self.pending_event.pop(key, None)
        # Remember the flush so a later batch under the same key is spotted.
        # Bounded: a gulp key is never revisited after minutes.
        self._flushed[key] = time.monotonic()
        if len(self._flushed) > 512:
            cutoff = time.monotonic() - 300
            for k in [k for k, t in self._flushed.items() if t < cutoff]:
                self._flushed.pop(k, None)
        # Already applied per batch in _handle; this is the gulp total.
        n_vetoed = self.pending_vetoed.pop(key, 0)
        utc_start_s, gulp = key
        if not complete:
            # An incomplete gulp is dropped rather than clustered on partial
            # sky. Every per-gulp veto reasons about the whole sky, and the
            # fragment holding the bright beam need not hold the evidence
            # against it.
            self.n_skipped_gulps += 1
            logger.warning("gulp %s skipped: only %d/%d jobs within %.1f s "
                           "(%d candidates discarded, occurrence %d)",
                           gulp, n_jobs, self.coalesce_jobs, waited,
                           len(cands) + n_vetoed, self.n_skipped_gulps)
            db.insert_gulp_stats(self.conn, utc_start_s or "", gulp,
                                 self._gulp_utc(key), n_jobs,
                                 len(cands) + n_vetoed, 0, 0, 0, 0.0,
                                 n_vetoed=n_vetoed, n_shed=0,
                                 coalesce_wait_ms=coalesce_wait_ms, skipped=1)
            return
        if not cands:
            # Quiet gulp: nothing survived, but it was observed. A fully
            # vetoed gulp lands here too, so n_cands is the veto count.
            db.insert_gulp_stats(self.conn, utc_start_s or "", gulp,
                                 self._gulp_utc(key), n_jobs, n_vetoed,
                                 0, 0, 0, 0.0, n_vetoed=n_vetoed, n_shed=0,
                                 coalesce_wait_ms=coalesce_wait_ms)
            return
        # Beam footprint for the occupancy veto, from the FULL pre-shed set.
        footprint = (build_beam_footprint(cands)
                     if self.occ_min_beams else None)
        # Shed before clustering: DBSCAN cost is what wedges the loop. n_cands
        # is the raw count in, with the two losses accounted separately.
        n_cands = len(cands) + n_vetoed
        if (self.storm_skip_gulp and self.max_cands_per_gulp > 0
                and len(cands) > self.max_cands_per_gulp):
            # storm_skip_gulp: drop the whole gulp rather than shed to the cap.
            n_shed, cands = len(cands), []
            self._n_storm_caps += 1
            if self._n_storm_caps == 1 or self._n_storm_caps % 50 == 0:
                logger.warning(
                    "storm skip: gulp %s dropped whole (%d candidates > cap "
                    "%d, occurrence %d)", key, n_shed,
                    self.max_cands_per_gulp, self._n_storm_caps)
        else:
            cands, n_shed = apply_storm_cap(cands, self.max_cands_per_gulp)
            if n_shed:
                self._n_storm_caps += 1
                if self._n_storm_caps == 1 or self._n_storm_caps % 50 == 0:
                    logger.warning(
                        "storm cap: gulp %s shed %d of %d candidates down to %d "
                        "(cap %d, per-beam quota %d, per-beam floor %d, "
                        "occurrence %d)", key, n_shed, n_cands, len(cands),
                        self.max_cands_per_gulp,
                        max(1, math.ceil(self.max_cands_per_gulp / BEAM_QUOTA_DIVISOR)),
                        BEAM_FLOOR, self._n_storm_caps)
        # One pointing-table lookup per gulp, cached by weights id. DBSCAN
        # clusters on sky position, not beam index.
        sky = self._sky_table(key)
        params = self._params_for(sky)
        t0 = time.monotonic()
        clusters = await asyncio.to_thread(cluster.cluster_candidates, cands,
                                           params, sky)
        dt = time.monotonic() - t0
        try:
            await self._process(key, clusters, n_jobs, n_cands, dt * 1e3,
                                n_vetoed, n_shed, footprint=footprint,
                                coalesce_wait_ms=coalesce_wait_ms)
        except Exception:
            logger.exception("processing gulp %s failed", key)

    # ------------------------------------------------------------ fast path

    def _inj_beams(self, inj_beam: int, sky: cluster.SkyTable | None) -> set[int]:
        """Beams that count as the injected one, on the sky.

        Beam indices are not sky-ordered, so an index window is not a sky test.
        Falls back to one only without a pointing table, warning once.
        """
        if sky is None:
            if not self._inj_index_warned:
                self._inj_index_warned = True
                logger.warning("no beam pointing table: injection matching "
                               "falls back to a beam-INDEX window (+-2), "
                               "which is not a sky match")
            return set(range(inj_beam - 2, inj_beam + 3))
        params = self._sky_params.get(sky.weights_id or "", self.params)
        return cluster.neighbour_beams(sky, int(inj_beam),
                                       params.beam_fwhm_x_deg,
                                       params.beam_fwhm_y_deg,
                                       self.injection_match_scale)

    def _cand_injection_match(self, epoch: float, beam: int, dm: float,
                              sky: cluster.SkyTable | None = None) -> bool:
        for inj_epoch, inj_beam, inj_dm in self._inj_cache:
            if (abs(epoch - inj_epoch) <= 60
                    and beam in self._inj_beams(inj_beam, sky)
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
        # Same sky neighbours the slow path uses, so a fast trigger cannot fire
        # on an injection the slow path would have tagged.
        sky = self._sky_table((batch.utc_start, batch.gulp))

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
            # Injections are never dumped: CAND_DUMP_BLOCK 0 sits upstream of
            # the injection merge, so a dump cannot contain the pulse. The
            # gallery renders truth plots from the generated .fil.
            if self._cand_injection_match(event_utc.timestamp(), c.beam,
                                          c.dm, sky):
                continue
            if best is None or c.snr > best[0].snr:
                best = (c, event_utc, reason)
        if best is None:
            return
        if time.monotonic() - self._last_fast_mono < 60:
            return  # one fast attempt per minute; budgets do the real limiting
        self._last_fast_mono = time.monotonic()

        c, event_utc, reason = best
        # Names of fast triggers still awaiting their cluster. Each has a
        # triggers row already, but this batch's row is written only when
        # _trigger runs below, so exclude the in-flight ones.
        exclude = set(self.pending_fast)
        name = events.new_event_name(self.conn, event_utc, exclude=exclude)
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

    def _injection_match(self, cl: cluster.Cluster, event_epoch: float,
                         sky: cluster.SkyTable | None = None) -> bool:
        for inj_epoch, inj_beam, inj_dm in self._inj_cache:
            if (abs(event_epoch - inj_epoch) <= 60
                    and cl.peak.beam in self._inj_beams(inj_beam, sky)
                    and cl.dm_lo - max(0.1 * inj_dm, 5) <= inj_dm
                    and inj_dm <= cl.dm_hi + max(0.1 * inj_dm, 5)):
                return True
        return False

    def _is_dm_floor_leak(self, cl: cluster.Cluster) -> bool:
        """Wide cluster whose lowest DM sits at hella's first trial."""
        return (self.dm_floor_veto_max_dm_lo > 0
                and cl.dm_lo <= self.dm_floor_veto_max_dm_lo
                and cl.peak.width >= self.dm_floor_veto_min_width)

    def _classify(self, cl: cluster.Cluster, event_utc: datetime | None,
                  sky: cluster.SkyTable | None = None) -> tuple[str, list[str]]:
        tags = []
        if event_utc is not None and self._injection_match(
                cl, event_utc.timestamp(), sky):
            tags.append("injection")
        if cl.peak.beam in self.veto:
            tags.append("veto")
        if (cl.n_beams > self.max_nbeam
                or (self.max_sky_extent_deg > 0
                    and cl.sky_extent_deg > self.max_sky_extent_deg)):
            tags.append("rfi_wide")
        if self._is_dm_floor_leak(cl):
            tags.append("dm_floor")
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
        if any(t in ("injection", "veto", "rfi_wide", "dm_floor")
               or t.startswith("occupancy:") for t in tags):
            return None
        src = next((t[4:] for t in tags if t.startswith("src:")), None)
        if src is not None and cl.peak.snr >= self.source_snr_min.get(src, 11.0):
            return f"known_source:{src}"
        if tier in ("A", "B") and cl.peak.dm >= self.dm_floor:
            return f"tier_{tier}"
        return None

    async def _process(self, key: tuple, clusters: list[cluster.Cluster],
                       n_jobs: int, n_cands: int, clustering_ms: float,
                       n_vetoed: int = 0, n_shed: int = 0,
                       footprint: tuple[list[int], list[int]] | None = None,
                       coalesce_wait_ms: float = 0.0) -> None:
        utc_start_s, gulp = key
        utc_start = timing.parse_dada_utc(utc_start_s) if utc_start_s else None
        self._refresh_injections()

        # Expire stale fast-trigger entries whose cluster never showed up.
        now_ts = datetime.now(timezone.utc).timestamp()
        for name in [n for n, (e, _, _) in self.pending_fast.items()
                     if now_ts - e > 120]:
            self.pending_fast.pop(name, None)

        rows, to_trigger, fast_names = [], [], []
        # Names minted for this gulp. Inserts happen after the loop, so the
        # uniqueness SELECT cannot see them and the batch could collide with
        # itself.
        minted: set[str] = set()
        # DM-floor leak: DBSCAN fragments one zero-DM impulse into several
        # clusters up the bowtie and only the lowest touches the floor. Tag
        # every cluster within the occupancy window of a floor fragment so the
        # next fragment up cannot trigger.
        sky = self._sky_table(key)
        floor_samps = [cl.peak.samp for cl in clusters if self._is_dm_floor_leak(cl)]
        for cl in clusters:
            event_utc = (timing.samp_to_utc(cl.peak.samp, utc_start)
                         if utc_start else None)
            tier, tags = self._classify(cl, event_utc, sky)
            if ("dm_floor" not in tags and floor_samps
                    and any(abs(cl.peak.samp - s) <= self.occ_window_samp
                            for s in floor_samps)):
                tags.append("dm_floor")
            if self.occ_min_beams and footprint is not None:
                n_occ = occupancy_beams(footprint[0], footprint[1],
                                        cl.peak.samp, self.occ_window_samp)
                if n_occ >= self.occ_min_beams:
                    tags.append(f"occupancy:{n_occ}")
            reason = self._wants_trigger(cl, tier, tags)
            fast_name = (self._match_fast(cl, event_utc.timestamp())
                         if event_utc else None)
            store = (tier != "-" or reason is not None or "injection" in tags
                     or fast_name is not None)
            if not store:
                continue
            if fast_name is not None:
                # The fast path already triggered and named this event.
                name = self.pending_fast.pop(fast_name, None) and fast_name
                tags.append("fast_triggered")
                fast_names.append(name)
                reason = None
            else:
                name = events.new_event_name(
                    self.conn, event_utc or datetime.now(timezone.utc),
                    exclude=minted)
                minted.add(name)
            ev_iso = event_utc.isoformat(timespec="milliseconds") if event_utc else ""
            sky_row = self._sky(event_utc, cl.peak.beam, radec=False)
            rows.append((cl, utc_start_s or "", gulp, ev_iso, tier, ",".join(tags), name, sky_row))
            if reason is not None and event_utc is not None:
                to_trigger.append((cl, name, event_utc, tier, reason))

        if rows:
            # One vectorised RA/Dec transform per gulp, not one per cluster.
            weights_registry.fill_radec([r[7] for r in rows],
                                        [datetime.fromisoformat(r[3]) if r[3] else datetime.now(timezone.utc) for r in rows])
        ids = db.insert_clusters(self.conn, rows) if rows else []
        # insert_clusters returns None for a skipped row. A trigger for such a
        # cluster is still recorded, with a NULL cluster_id.
        id_by_name = {row[6]: cid for row, cid in zip(rows, ids) if cid is not None}
        n_stored = sum(1 for cid in ids if cid is not None)
        self.n_clusters += n_stored
        for name in fast_names:
            if name in id_by_name:
                with self.conn:
                    self.conn.execute(
                        "UPDATE triggers SET cluster_id = ? WHERE candname = ?"
                        " AND cluster_id IS NULL", (id_by_name[name], name))

        gulp_utc = (timing.samp_to_utc(min(c.samp_lo for c in clusters), utc_start)
                    .isoformat(timespec="milliseconds") if clusters and utc_start else "")
        db.insert_gulp_stats(self.conn, utc_start_s or "", gulp, gulp_utc, n_jobs,
                             n_cands, len(clusters), n_stored, len(to_trigger),
                             clustering_ms, n_vetoed, n_shed,
                             coalesce_wait_ms=coalesce_wait_ms)

        # One dump per gulp: one physical event can fragment into several
        # clusters. Fire the strongest and audit the rest.
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

        logger.info("trigger candidate %s (%s): snr=%.1f dm=%.2f beam=%d nbeam=%d "
                    "sky_extent=%.1f deg",
                    name, reason, c.snr, c.dm, c.beam, cl.n_beams,
                    cl.sky_extent_deg)

        # Two suppressions, both recording the decision in full: --shadow /
        # shadow: true (dry run), and dumps_enabled: false (kill-switch).
        # Neither sends a dump command or spools a trigger card.
        if self.shadow or not self.dumps_enabled:
            action = "shadow" if self.shadow else "suppressed_commissioning"
            db.insert_trigger(self.conn, cluster_id, name, stream, action, reason,
                              dump_utc_start=start_s, dump_utc_stop=stop_s)
            return

        # Storm lockout, blind triggers only: any eligible blind event restarts
        # the clock whether or not it dumped, so a storm costs one dump.
        if self.storm_lockout_s > 0 and reason.startswith("tier"):
            prev, self._last_blind_eligible = self._last_blind_eligible, now
            if prev is not None and (now - prev).total_seconds() < self.storm_lockout_s:
                logger.warning("blind trigger %s locked out: previous eligible "
                               "event %.1f s ago (< %.0f s quiet required)",
                               name, (now - prev).total_seconds(),
                               self.storm_lockout_s)
                db.insert_trigger(self.conn, cluster_id, name, stream, "refused",
                                  f"{reason};storm_lockout")
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

    def _sky(self, event_utc: datetime | None, beam: int, radec: bool = True,
             sun: bool = False) -> dict | None:
        """Pointing of ``beam`` from the weights live at ``event_utc``; None if unresolvable."""
        if event_utc is None:
            return None
        try:
            return self.registry.sky_for(event_utc, int(beam), radec=radec, sun=sun)
        except Exception:
            logger.exception("weights registry lookup failed")
            return None

    def _gulp_utc(self, key: tuple) -> str:
        """ISO arrival time of a gulp's first sample, or '' without a UTC_START."""
        utc_start_s, gulp = key
        if not utc_start_s:
            return ""
        return timing.samp_to_utc(
            int(gulp or 0) * 8192,
            timing.parse_dada_utc(utc_start_s)).isoformat(timespec="milliseconds")

    def _sky_table(self, key: tuple) -> cluster.SkyTable | None:
        """Beam pointing table for one gulp, or None to fall back to beam index.

        Resolved once per gulp from the weights live at the gulp's start time
        and cached by weights id. None when the registry cannot name a single
        product for the time (no event, partial deploy, unregistered payload);
        clustering then degrades to the beam-index axis and tags nothing on an
        unmeasured sky extent.
        """
        utc_start_s, gulp = key
        if not utc_start_s:
            return None
        try:
            gulp_utc = timing.samp_to_utc(int(gulp or 0) * 8192,
                                          timing.parse_dada_utc(utc_start_s))
            pointings = self.registry.pointings_for(gulp_utc)
        except Exception:
            logger.exception("weights registry pointings lookup failed")
            return None
        if not pointings:
            return None
        wid = pointings.get("weights_id") or ""
        table = self._sky_tables.get(wid)
        if table is None:
            table = cluster.SkyTable.from_pointings(pointings)
            if table is None:
                return None
            self._sky_tables[wid] = table
            self._sky_params[wid] = self._params_for_pointings(wid, pointings)
            logger.info("beam pointing table loaded for weights %s (%d beams)",
                        wid or "<unnamed>", table.n)
        return table

    def _params_for_pointings(self, wid: str, pointings: dict) -> cluster.ClusterParams:
        """Cluster params for one weights product: the registry's beam ellipse
        when the product carries one, the configured fallback otherwise."""
        fx, fy = pointings.get("beam_fwhm_x_deg"), pointings.get("beam_fwhm_y_deg")
        if fx and fy and fx > 0 and fy > 0:
            logger.info("sky link scales for weights %s from the registry product: "
                        "%.2f deg E-W x %.2f deg N-S", wid or "<unnamed>", fx, fy)
            return dataclasses.replace(self.params, beam_fwhm_x_deg=float(fx), beam_fwhm_y_deg=float(fy))
        logger.info("weights %s carries no beam ellipse; sky link scales from config: "
                    "%.2f deg E-W x %.2f deg N-S", wid or "<unnamed>",
                    self.params.beam_fwhm_x_deg, self.params.beam_fwhm_y_deg)
        return self.params

    def _params_for(self, sky: cluster.SkyTable | None) -> cluster.ClusterParams:
        """Cluster params to use for a gulp given its pointing table."""
        if sky is None:
            return self.params
        return self._sky_params.get(sky.weights_id or "", self.params)

    def _pointings(self, event_utc: datetime | None) -> dict | None:
        if event_utc is None:
            return None
        try:
            return self.registry.pointings_for(event_utc)
        except Exception:
            logger.exception("weights registry pointings lookup failed")
            return None

    async def _delayed_card(self, cl: cluster.Cluster, name: str, event_utc: datetime,
                            start_s: str, stop_s: str, loc: beams.StreamLocation,
                            reason: str) -> None:
        # Hold the card back so context candidates from lagging jobs arrive.
        await asyncio.sleep(self.ctx_delay_s)
        c = cl.peak
        # Fast-path cards start as single trials. The cluster row usually
        # exists by now, so take the envelope numbers from it.
        row = self.conn.execute("SELECT n_members, n_beams, sky_extent_deg"
                                " FROM clusters WHERE name = ?", (name,)).fetchone()
        n_members, n_beams, sky_extent = (
            row if row else (cl.n_members, cl.n_beams, cl.sky_extent_deg))
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
            # Largest pairwise sky separation of the cluster's beams, degrees.
            # Null when no pointing table was available at clustering time.
            "sky_extent_deg": sky_extent,
            "sky": self._sky(event_utc, c.beam, sun=True),
            # Full pointing table of the weights live at the event, so the
            # plotter on either node draws the footprint without the registry.
            "pointings": self._pointings(event_utc),
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
            # Pet the watchdog from the event loop itself, that being the
            # thread that wedges. WatchdogSec=180 tolerates two missed beats.
            sdnotify.watchdog()
            n_trig = self.conn.execute(
                "SELECT count(*) FROM triggers WHERE created_utc >= ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
                    timespec="milliseconds"),)).fetchone()[0]
            mode = (" [SHADOW]" if self.shadow else
                    "" if self.dumps_enabled else " [DUMPS DISABLED]")
            logger.info("heartbeat: %d batches, %d cands -> %d clusters stored, "
                        "%d trigger decisions (last minute), %d gulps skipped "
                        "incomplete / %d late batches (totals)%s",
                        self.n_batches, self.n_cands, self.n_clusters, n_trig,
                        self.n_skipped_gulps, self.n_late_batches, mode)
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
    logger.info("t2d starting%s: db=%s, dumps=%s, voltage=%s, "
                "veto_widths=%s, max_cands_per_gulp=%d",
                " in SHADOW mode" if shadow else "",
                cfg.get("db", db.DEFAULT_PATH),
                "enabled" if daemon.dumps_enabled else "DISABLED (commissioning)",
                "enabled" if daemon.voltage_enabled else "disabled",
                sorted(daemon.veto_widths) or "none", daemon.max_cands_per_gulp)
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
