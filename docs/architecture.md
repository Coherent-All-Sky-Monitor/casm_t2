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

Survivors get a `YYMMDDxxxx` name, an intensity dump on the owning node,
and a trigger card in the T3 spool. Voltage dumps are wired (tier A only)
but ship disabled.

`trigger.fast_path` picks between two trigger styles. Strict
cluster-first (`false`) waits for DBSCAN and the full chain before any
dump, DSA-110 style; ring-window misses are then deliberate, audited, and
are the data that sizes the ring buffer. The hybrid fast path (`true`)
fires on bright single candidates immediately and reconciles with the
cluster afterwards — for when the latency budget is tighter than the
ring.

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
(the dump daemon said no — usually the ring window), `failed`, `shadow`.

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
