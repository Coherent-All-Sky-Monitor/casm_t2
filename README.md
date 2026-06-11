# casm_t2

T2 stage of the CASM fast-transient search. It takes the single-pulse
candidate stream from the casm-hella GPU search (T1), clusters it, throws
out the RFI, and decides — inside the few seconds the upstream ring buffer
allows — which events are worth dumping to disk. casm_t3 turns those dumps
into plots and a monitoring UI.

Hella emits thousands of candidates a second in bad RFI weather, and
almost all of it is junk. The job here is to get from that firehose to a
handful of defensible dump decisions per hour, with every decision
recorded so the misses can be audited as honestly as the hits.

## How it works

`t2d` owns eight TCP ports, one per hella job. Each gulp it coalesces the
per-job batches, deduplicates, and clusters with DBSCAN over (time, DM,
width, beam) — beam count is the main RFI discriminator, since a real
pulse is compact in beam and RFI is not. Clusters then run a filter
chain: injection match (stored, never dumped), beam veto, wide-beam RFI
cut, known-source DM-range match, and S/N tiers (A >= 30, B >= 15,
C >= 12; blind triggers need A/B plus DM >= 20). Survivors hit the
trigger budgets — minimum spacing, daily caps, one dump per gulp in a
storm, and a free-disk floor — before a dump command goes to the owning
backend node.

Everything lands in one SQLite database: stored clusters, the full
trigger audit (refusals with reasons, including ring-window misses), the
injection ledger with per-gate recovery, and per-gulp funnel counters.

## Install

    pip install -e .

Python >= 3.10; numpy, scikit-learn, pyyaml.

## Run

    t2d config/t2d.yaml            # the daemon; that YAML is the only config
    t2d config/t2d.yaml --shadow   # cluster and record, fire nothing

Also ships `t2-dump` (manual smoke dump), `t2-replay` (offline replay of
a UTC slice), `t2-inject` / `t2-inject-report` (live injections and the
daily recovery report), and `t2-transit-schedule`.

See `docs/architecture.md` for the data path and database schema, and
`docs/operations.md` for deployment, config reference, and runbooks.

MIT license.
