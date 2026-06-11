"""Replay recorded hella candidates through the T2 clustering, for tuning.

Reads a UTC slice of the hella .dat output files, clusters it in
gulp-sized chunks exactly as the live daemon would, and reports what the
trigger logic would have seen: cluster rate, noise fraction, the beam-span
distribution (the RFI discriminator), runtime per chunk versus real time,
and the top clusters. Purely offline — reads files, writes nothing but an
optional CSV of clusters for HiPlot.

    t2-replay --from 2026-06-10T18:00 --to 2026-06-10T19:00 \\
              --eps 1.0 --min-samples 5 --csv /tmp/clusters.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from casm_t2 import beams, candfiles, cluster, timing

# Live coalescing window: one hella gulp across all jobs.
CHUNK_SAMP = 8192


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def newest_obs(cands_dir: Path) -> datetime:
    newest = max(cands_dir.glob("cands_*.dat.*"), key=lambda p: p.stat().st_mtime)
    return timing.utc_start_from_cands_path(newest)


def main() -> None:
    p = argparse.ArgumentParser(description="Replay hella candidates through T2 clustering")
    p.add_argument("--cands-dir", default="/mnt/nvme4/data/casm/hella_cands")
    p.add_argument("--obs", help="observation UTC_START (default: newest file)")
    p.add_argument("--jobs", default="0,1,2,3", help="comma-separated hella job numbers")
    p.add_argument("--from", dest="t_from", required=True, help="slice start, ISO UTC")
    p.add_argument("--to", dest="t_to", required=True, help="slice end, ISO UTC")
    p.add_argument("--eps", type=float, default=1.0)
    p.add_argument("--min-samples", type=int, default=5)
    p.add_argument("--samp-scale", type=float, default=64.0)
    p.add_argument("--dm-idx-scale", type=float, default=32.0)
    p.add_argument("--width-scale", type=float, default=2.0)
    p.add_argument("--beam-scale", type=float, default=4.0)
    p.add_argument("--csv", help="write one row per cluster to this CSV")
    p.add_argument("--top", type=int, default=15, help="clusters to print")
    args = p.parse_args()

    cands_dir = Path(args.cands_dir)
    utc_start = (timing.parse_dada_utc(args.obs) if args.obs else newest_obs(cands_dir))
    t_from, t_to = parse_utc(args.t_from), parse_utc(args.t_to)
    samp_min = int((t_from - utc_start).total_seconds() / timing.TSAMP_S)
    samp_max = int((t_to - utc_start).total_seconds() / timing.TSAMP_S)
    if samp_max <= samp_min:
        sys.exit("empty time range")

    params = cluster.ClusterParams(
        eps=args.eps, min_samples=args.min_samples,
        samp_scale=args.samp_scale, dm_idx_scale=args.dm_idx_scale,
        width_scale=args.width_scale, beam_scale=args.beam_scale)

    print(f"obs {utc_start:%Y-%m-%d-%H:%M:%S}  samp [{samp_min}, {samp_max}] "
          f"({(samp_max - samp_min) * timing.TSAMP_S:.0f} s)")
    print(f"params: {params}")

    # Bin the slice into live-sized chunks, pooling all jobs (the live
    # daemon sees all 512 beams in one coalesced window).
    chunks: dict[int, list] = defaultdict(list)
    n_read = 0
    t0 = time.monotonic()
    for job in args.jobs.split(","):
        path = cands_dir / f"cands_{utc_start:%Y-%m-%d-%H:%M:%S}.dat.{job.strip()}"
        if not path.exists():
            print(f"  WARNING: {path.name} missing, skipping", file=sys.stderr)
            continue
        n_file = 0
        for c in candfiles.iter_span(path, samp_min, samp_max):
            chunks[c.samp // CHUNK_SAMP].append(c)
            n_file += 1
        n_read += n_file
        print(f"  {path.name}: {n_file} candidates")
    print(f"read {n_read} candidates in {time.monotonic() - t0:.1f} s, "
          f"{len(chunks)} chunks")

    all_clusters: list[cluster.Cluster] = []
    nbeam_hist: Counter = Counter()
    n_noise = 0
    t_cluster = 0.0
    worst = 0.0
    for key in sorted(chunks):
        tc = time.monotonic()
        cls = cluster.cluster_candidates(chunks[key], params)
        dt = time.monotonic() - tc
        t_cluster += dt
        worst = max(worst, dt)
        for cl in cls:
            if cl.is_noise:
                n_noise += 1
            else:
                nbeam_hist[cl.n_beams] += 1
                all_clusters.append(cl)

    span_s = (samp_max - samp_min) * timing.TSAMP_S
    n_real = len(all_clusters)
    print(f"\n{n_real} clusters + {n_noise} noise singletons "
          f"({n_read / max(n_real + n_noise, 1):.1f} cands/object)")
    print(f"rates: {n_read / span_s:.1f} cand/s in -> {n_real / span_s:.2f} clusters/s "
          f"+ {n_noise / span_s:.2f} noise/s out")
    print(f"clustering cost: {t_cluster:.1f} s total ({t_cluster / span_s * 100:.2f}% "
          f"of real time), worst chunk {worst * 1e3:.0f} ms")

    print("\nbeam-span distribution of clusters:")
    for lo, hi, label in [(1, 1, "1"), (2, 3, "2-3"), (4, 8, "4-8"),
                          (9, 32, "9-32"), (33, 512, ">32")]:
        n = sum(v for k, v in nbeam_hist.items() if lo <= k <= hi)
        print(f"  {label:>5} beams: {n:7d}  ({n / max(n_real, 1) * 100:.1f}%)")

    all_clusters.sort(key=lambda cl: -cl.peak.snr)
    print(f"\ntop {args.top} clusters:")
    print("   snr     dm  width beam nmemb nbeam  dm_span        event_utc")
    for cl in all_clusters[:args.top]:
        ev = timing.samp_to_utc(cl.peak.samp, utc_start)
        print(f"  {cl.peak.snr:5.1f} {cl.peak.dm:6.1f}  {cl.peak.width:4d} "
              f"{cl.peak.beam:4d} {cl.n_members:5d} {cl.n_beams:5d} "
              f"{cl.dm_lo:6.1f}-{cl.dm_hi:<6.1f} {ev:%H:%M:%S.%f}"[:79])

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["snr", "dm", "width", "beam", "samp", "event_utc",
                        "n_members", "n_beams", "beam_lo", "beam_hi",
                        "dm_lo", "dm_hi"])
            for cl in all_clusters:
                ev = timing.samp_to_utc(cl.peak.samp, utc_start)
                w.writerow([cl.peak.snr, cl.peak.dm, cl.peak.width, cl.peak.beam,
                            cl.peak.samp, ev.isoformat(timespec="milliseconds"),
                            cl.n_members, cl.n_beams, cl.beam_lo, cl.beam_hi,
                            cl.dm_lo, cl.dm_hi])
        print(f"\nwrote {len(all_clusters)} clusters to {args.csv}")


if __name__ == "__main__":
    main()
