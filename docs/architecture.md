# casm_t2 architecture

## Data path

Eight casm-hella jobs (one per 64-beam stream; jobs 0-3 on the first
backend node, 4-7 on the second) each open a TCP connection per gulp and
send their candidate list. `t2d` owns these ports. Per gulp it:

1. coalesces the eight per-job batches (short wait for stragglers),
2. deduplicates on a half-scale grid and clusters with DBSCAN,
3. classifies each cluster and decides whether to trigger,
4. commands intensity dumps, writes trigger cards for T3,
5. records everything in SQLite.

The upstream intensity ring buffer holds only a limited look-back, so the
whole chain runs against a hard deadline: a dump command that arrives
after the event has left the ring is refused by the dump daemon and is
recorded as a miss.

## Wire format (`wire.py`)

One preamble line, then one candidate per line:

    <gulp> <utc_start> <x> <tsamp_us>
    snr samp time_days width dm_idx dm beam

`beam` is global (0-511). A legacy 8-column variant still parses.
`time_days` is not trusted; event times are derived from `utc_start`
plus `samp` x tsamp (`timing.py`, tsamp = 1.048576 ms — never 1.0 ms).

## Clustering (`cluster.py`)

DBSCAN with cityblock metric over scaled (samp, dm_idx, log2(width),
beam). One `Cluster` per event carrying the peak trial and the membership
envelope (beam/DM/time ranges, member count, beam count). Noise points
survive as singletons. The number of distinct beams is the primary RFI
discriminator: a real pulse is compact in beam, RFI lights up many.

## Decision chain (`apps/t2d.py`)

Per cluster, in order:

- **injection match** — within the matching windows of a ledger row:
  tagged `injection`, stored, never dumped (the dump ring taps data
  upstream of the injection merge, so a dump could not contain the pulse).
- **beam veto** — configured noisy beams.
- **wide-beam RFI** — more than `max_nbeam` distinct beams.
- **known-source match** — beam inside a scheduled transit window AND the
  cluster's DM *range* overlaps the source DM (range, not peak: storms
  produce clusters whose peak lands anywhere).
- **tier** — S/N bands: A >= 30, B >= 15, C >= 12. Blind triggers require
  tier A or B and peak DM >= the DM floor; known sources use their own
  S/N minimum. Tier C is stored for bookkeeping only.
- **budgets** — per-kind token buckets (minimum spacing + daily cap), one
  dump per gulp in a storm, and a free-disk floor checked on the node
  that would receive the dump.

Events that pass get a `YYMMDDxxxx` name (`events.py`), an intensity dump
on the owning node, and a trigger card in the T3 spool. Voltage dumps are
wired (tier A only) but disabled by default.

### Trigger paths

`trigger.fast_path` in the YAML selects between:

- **strict cluster-first** (`false`, current): every dump decision waits
  for DBSCAN and the full chain. Ring-window misses are recorded
  (`refused_daemon`) and displayed, not dodged — they are the data that
  sizes the ring buffer.
- **hybrid fast path** (`true`): bright single candidates fire a dump
  immediately on arrival, reconciled with their cluster afterwards. Used
  when the latency budget is tighter than the ring.

## Database (`db.py`)

Single SQLite file (WAL). Tables:

- `clusters` — every stored event with tier, tags, name, envelope.
- `triggers` — full dump audit: action (triggered / refused /
  refused_daemon / failed / shadow), detail, bytes, cleanup state.
- `injections` — the injection ledger with per-gate recovery columns
  (gate_t1, gate_t2, gate_trigger, recovered S/N and DM).
- `gulp_stats` — per-gulp funnel counters (candidates in, clusters,
  stored, would-trigger, clustering time).
- `labels`, `frbs` — human classifications from the web UI and the
  promoted FRB catalog.

All timestamps are ISO-8601 UTC with a `T` separator. Do not compare them
against sqlite's `datetime('now')` strings (space-separated): build
cutoffs in Python.

## Injections (`apps/inject_daemon.py`)

On a schedule, synthesises a single pulse (DM, amplitude, width drawn from
configured ranges), converts it to DADA, and writes it into a beamformer
injection FIFO. The ledger row is written *before* the FIFO write, so a
crash cannot produce an unaccounted pulse. A reconciliation pass a few
minutes later fills the per-gate recovery columns; `t2-inject-report`
aggregates daily.
