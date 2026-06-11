"""T2 clustering daemon (shadow mode).

Listens for the hella candidate stream — in shadow deployment on the tee
ports that t2-source-watch forwards to, so the live trigger path is
untouched — coalesces the eight jobs' batches per gulp, clusters each gulp
with DBSCAN, applies the filter chain, and records every cluster and
would-trigger decision in the T2 SQLite database. No dumps are requested
in shadow mode; the point is to accumulate the evidence (cluster rates,
RFI cut behaviour, recovery of known-source pulses) needed to promote the
clustered path to live triggering.

Filter chain per cluster: beam-extent RFI cut -> beam veto -> SNR tiers
(A/B/C). A 'would_trigger' tag marks clusters that live triggering would
have dumped: tier A or B, not RFI-wide, not vetoed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from casm_t2 import cluster, db, timing, wire

logger = logging.getLogger("t2d")

TIER_A_SNR = 30.0
TIER_B_SNR = 15.0
TIER_C_SNR = 12.0


class T2Daemon:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.params = cluster.ClusterParams(
            eps=args.eps, min_samples=args.min_samples,
            samp_scale=args.samp_scale, dm_idx_scale=args.dm_idx_scale,
            width_scale=args.width_scale, beam_scale=args.beam_scale)
        self.veto = {int(b) for b in args.beam_veto.split(",") if b.strip()}
        self.conn = db.connect(args.db)
        # (utc_start, gulp) -> accumulating candidate list; flushed
        # coalesce_s after the first job's batch for that gulp arrives.
        self.pending: dict[tuple, list[wire.Candidate]] = defaultdict(list)
        self.flushers: set[asyncio.Task] = set()
        self.n_batches = self.n_cands = self.n_clusters = self.n_would = 0

    # ------------------------------------------------------------- ingest

    async def serve(self) -> None:
        servers = []
        for i in range(self.args.nports):
            port = self.args.listen_base + i
            servers.append(await asyncio.start_server(self._handle, self.args.listen_host, port))
            logger.info("listening on %s:%d", self.args.listen_host, port)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._heartbeat())
            for s in servers:
                tg.create_task(s.serve_forever())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
        if not batch.cands:
            return
        key = (batch.utc_start, batch.gulp)
        first = key not in self.pending
        self.pending[key].extend(batch.cands)
        if first:
            task = asyncio.create_task(self._flush_later(key))
            self.flushers.add(task)
            task.add_done_callback(self.flushers.discard)

    async def _flush_later(self, key: tuple) -> None:
        await asyncio.sleep(self.args.coalesce_s)
        cands = self.pending.pop(key, [])
        if not cands:
            return
        t0 = time.monotonic()
        clusters = await asyncio.to_thread(cluster.cluster_candidates, cands, self.params)
        try:
            self._record(key, clusters)
        except Exception:
            logger.exception("recording gulp %s failed", key)
        dt = time.monotonic() - t0
        if dt > 2.0:
            logger.warning("slow gulp %s: %d cands -> %d clusters in %.1f s",
                           key, len(cands), len(clusters), dt)

    # ----------------------------------------------------------- decisions

    def _classify(self, cl: cluster.Cluster) -> tuple[str, list[str]]:
        tags = []
        if cl.n_beams > self.args.max_nbeam:
            tags.append("rfi_wide")
        if cl.peak.beam in self.veto:
            tags.append("veto")
        snr = cl.peak.snr
        tier = ("A" if snr >= TIER_A_SNR else
                "B" if snr >= TIER_B_SNR else
                "C" if snr >= TIER_C_SNR else "-")
        if tier in ("A", "B") and not tags and cl.peak.dm >= self.args.dm_floor:
            tags.append("would_trigger")
            self.n_would += 1
        return tier, tags

    def _record(self, key: tuple, clusters: list[cluster.Cluster]) -> None:
        utc_start_s, gulp = key
        utc_start = timing.parse_dada_utc(utc_start_s) if utc_start_s else None
        rows = []
        for cl in clusters:
            if cl.peak.snr < self.args.store_min_snr:
                continue
            tier, tags = self._classify(cl)
            event_utc = (timing.samp_to_utc(cl.peak.samp, utc_start).isoformat(
                timespec="milliseconds") if utc_start else "")
            rows.append((cl, utc_start_s or "", gulp, event_utc, tier, ",".join(tags)))
            if "would_trigger" in tags:
                logger.info("WOULD TRIGGER: snr=%.1f dm=%.2f beam=%d width=%d "
                            "nmemb=%d nbeam=%d event=%s",
                            cl.peak.snr, cl.peak.dm, cl.peak.beam, cl.peak.width,
                            cl.n_members, cl.n_beams, event_utc)
        if rows:
            db.insert_clusters(self.conn, rows)
            self.n_clusters += len(rows)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(60)
            logger.info("heartbeat: %d batches, %d cands -> %d clusters stored, "
                        "%d would-trigger (last minute)",
                        self.n_batches, self.n_cands, self.n_clusters, self.n_would)
            self.n_batches = self.n_cands = self.n_clusters = self.n_would = 0


def main() -> None:
    p = argparse.ArgumentParser(description="T2 clustering daemon (shadow mode)")
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-base", type=int, default=13345,
                   help="first ingest port (tee ports in shadow deployment)")
    p.add_argument("--nports", type=int, default=8)
    p.add_argument("--db", default=db.DEFAULT_PATH)
    p.add_argument("--coalesce-s", type=float, default=2.0,
                   help="wait this long after a gulp's first batch before clustering")
    p.add_argument("--eps", type=float, default=1.0)
    p.add_argument("--min-samples", type=int, default=5)
    p.add_argument("--samp-scale", type=float, default=64.0)
    p.add_argument("--dm-idx-scale", type=float, default=32.0)
    p.add_argument("--width-scale", type=float, default=2.0)
    p.add_argument("--beam-scale", type=float, default=4.0)
    p.add_argument("--max-nbeam", type=int, default=32,
                   help="clusters wider than this many beams are tagged rfi_wide")
    p.add_argument("--beam-veto", default="2", help="comma-separated beams")
    p.add_argument("--dm-floor", type=float, default=20.0,
                   help="would-trigger requires peak DM at or above this")
    p.add_argument("--store-min-snr", type=float, default=9.0)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    daemon = T2Daemon(args)
    logger.info("shadow mode: recording to %s, no dumps will be requested", args.db)
    try:
        asyncio.run(daemon.serve())
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
