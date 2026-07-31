# casm_t2 architecture

## Data path

Eight hella jobs — one per 64-beam stream, jobs 0-3 on the first backend
node and 4-7 on the second — connect per gulp and send their candidate
list. t2d coalesces the batches (with a short wait for stragglers),
deduplicates and clusters, classifies, fires whatever dumps survive the
policy, and writes the whole decision chain to SQLite.

The deadline matters more than the throughput. The intensity ring buffer
upstream holds a limited look-back; a dump command that arrives after the
event has left the ring gets refused by the dump daemon, and t2d records
that as a miss rather than pretending it didn't happen. T1 already spends
13-25 s between pulse and report (gulp fill, ring hand-offs, search
compute), so T2's own latency — well under a second — is spent against a
budget it doesn't control.

## Wire format

One preamble line, then one candidate per line:

    <gulp> <utc_start> <x> <tsamp_us>
    snr samp time_days width dm_idx dm beam

`beam` is global (0-511); a legacy 8-column variant still parses
(wire.py). Event times come from `utc_start` plus `samp` x tsamp, with
tsamp = 1.048576 ms. The `time_days` column is not trusted, and tsamp is
never 1.0 ms, whatever the column headers imply (timing.py).

## Storm defences

DBSCAN cost is superlinear in candidate count, and the coalescer feeds it
all eight jobs at once. During the July 2026 storms that meant 80,000
trials in one gulp and 83-137 s of clustering against an 8.7 s real-time
budget — ingest stalled and the DAQ back-pressured. Two knobs shed load
before clustering, both counted per gulp:

`veto_widths` drops whole boxcar-width indices at parse time, ahead of the
fast path and the trigger-card context. Index 6 (67 ms) is 97-98.6% of
stored rows on a quiet day, red-noise junk at DM >= 200. Empty list
disables it. This throws away real data, so it defaults off in code and is
turned on in the shipped config.

`max_cands_per_gulp` bounds what reaches DBSCAN, in two stages. Stage 1
gives each global beam a quota of its top `ceil(cap/64)` by S/N, which
handles RFI concentrated in a few beams and keeps the shed fair across the
sky. Stage 2 runs only if that still leaves more than the cap: keep the
global top `cap` by S/N, then add back every populated beam's top 4.

Stage 2 is not optional. Stage 1's allowance is quota x populated beams —
313 x 512 = 160k at the shipped cap — so a storm spread evenly over every
beam passes straight through it, which is precisely what the 2026-07-31
storm was (width-0 spikes in all beams, untouched by `veto_widths: [6]`).
The per-beam floor in stage 2 is what keeps the global truncation safe: a
plain global top-N would let a bright storm elsewhere in the sky evict the
single-beam FRB this daemon exists to catch. Worst case kept is
`cap + 4 x 512` ~ 22k.

## Clustering

DBSCAN, cityblock metric, over scaled (samp, dm_idx, log2(width), beam).
Each cluster keeps its peak trial and the membership envelope: time, DM
and beam ranges, member count, distinct-beam count. Noise points survive
as singleton clusters rather than being dropped — a lone bright pulse in
one beam is exactly what an FRB looks like. Beam count is the primary
RFI discriminator.

## Decision chain

Per cluster, in order (apps/t2d.py):

injection match — within the matching windows of a ledger row. Tagged and
stored, never dumped: the dump ring taps the data upstream of the
injection merge, so a dump physically cannot contain the injected pulse.

beam veto, then wide-beam RFI — configured noisy beams, then anything
spanning more than `max_nbeam` distinct beams.

known-source match — beam inside a scheduled transit window and the
cluster's DM *range* overlapping the source DM. Range, not peak: a storm
produces clusters whose peak DM lands anywhere, and matching on peak is
how a pulsar tag ends up on RFI.

tier — S/N bands A >= 30, B >= 15, C >= 12. Blind triggers need tier A or
B and peak DM above the floor (20 pc/cc); known sources carry their own
S/N minimum and may sit below tier C. Tier C is stored for bookkeeping
and never triggers.

budgets — token buckets per dump kind (minimum spacing plus a hard daily
cap), one dump per gulp during storms, and a free-disk floor checked on
the node that would receive the data. Every refusal is recorded with its
reason.

Survivors get a name — `YYMMDD` plus 6 random lowercase letters, 12 chars
(legacy 10-char names from before 2026-07-31 persist in the DB and in
artifact paths) — an intensity dump on the owning node, and a trigger card
in the T3 spool. Automatic voltage dumps are wired (tier A only) but ship
disabled, and `dumps_enabled: false` suppresses intensity dumps too while
the telescope is commissioning.

`trigger.fast_path` picks between two trigger styles. Strict
cluster-first (`false`) waits for DBSCAN and the full chain before any
dump, DSA-110 style; ring-window misses are then deliberate, audited, and
are the data that sizes the ring buffer. The hybrid fast path (`true`)
fires on bright single candidates immediately and reconciles with the
cluster afterwards — for when the latency budget is tighter than the
ring.

## Voltage dumps

A separate path with its own daemons. Raw voltages are tapped on the
antenna side, before beamforming: six antenna streams, three per node,
each with a casm_cand_dump daemon on port 27000 + stream (0-2 on corr1,
3-5 on corr2). Every antenna sees the whole sky, so a voltage dump goes
to all six endpoints. The streams split the band top-down in 15.625 MHz
slices — stream 0 is 468.75-484.375 MHz, stream 5 is 390.625-406.25, and
the 440-465 MHz live band sits in streams 1 and 2, both on corr1.

t2d can command these on tier A but ships with `trigger.voltage`
disabled. `casm-voltage-dump` drives them by hand instead and needs
nothing from T2 — it runs with t2d stopped. At 2.0625 GB/s per stream the
binding constraint is disk rather than the ring, so that CLI does the
window and disk arithmetic itself before sending; see
`docs/operations.md`.

## Database

One SQLite file, WAL mode (db.py):

| table | holds |
|---|---|
| clusters | every stored event: name, tier, tags, peak, envelope |
| triggers | the dump audit: action, detail, bytes, cleanup state |
| injections | the ledger, with per-gate recovery columns |
| gulp_stats | per-gulp funnel counters |
| labels, frbs | human classifications and the promoted catalog |

Trigger actions are `triggered`, `refused` (policy), `refused_daemon`
(the dump daemon said no — usually the ring window), `failed`, `shadow`,
and `suppressed_commissioning` (`dumps_enabled: false`).

`gulp_stats.n_cands` is the raw count in; `n_vetoed` and `n_shed` are what
the width veto and the storm cap dropped before clustering, so DBSCAN saw
`n_cands - n_vetoed - n_shed`.

Timestamps are ISO-8601 UTC with a `T` separator. sqlite's
`datetime('now')` renders with a space, which string-compares wrongly
against them — build cutoffs in Python, never in SQL.

## Injections

The injection daemon synthesises a pulse with parameters drawn from
configured ranges, converts it to DADA, and writes it into a beamformer
injection FIFO. The ledger row goes in *before* the FIFO write, so a
crash can't produce an unaccounted pulse in the data. Reconciliation a
few minutes later fills the gate columns (seen at T1, clustered at T2,
trigger-eligible), and the first failed gate is the failure reason.
