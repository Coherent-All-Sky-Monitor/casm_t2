# casm_t2

Real-time T2 stage of the CASM fast-transient search. It ingests
single-pulse candidates from the casm-hella GPU search (T1) over TCP,
clusters them, filters RFI and known sources, and fires ring-buffer
intensity dumps for the events worth keeping — all within the few seconds
the upstream ring buffer allows.

Pipeline position:

    T1  casm-hella GPU single-pulse search  (8 jobs, 512 beams)
    T2  this repo: clustering, filtering, trigger policy
    T3  casm_t3: dump plotting, monitoring web UI, disk janitor

## What it does

- Listens on eight TCP ports for per-gulp candidate batches
  (~10^3-10^4 candidates/s in typical RFI weather).
- Coalesces the per-job batches, deduplicates, and clusters with DBSCAN
  over (time, DM, width, beam).
- Classifies every cluster: injection match, beam veto, wide-beam RFI cut,
  known-source DM-range match, S/N tier (A >= 30, B >= 15, C >= 12).
- Applies trigger policy — token-bucket rate budgets, per-gulp storm rule,
  daily caps, and a hard free-disk floor — then commands intensity dumps
  on the owning backend node.
- Records the full decision chain (stored clusters, trigger audit,
  injection ledger, per-gulp funnel stats) in a single SQLite database.
- Runs a scheduled live injection program with per-gate recovery
  accounting.

## Install

    python -m venv env && source env/bin/activate
    pip install -e .

Python >= 3.10. Runtime dependencies are numpy, scikit-learn, and pyyaml.

## Run

    t2d config/t2d.yaml          # the daemon (one YAML is the only config)
    t2d config/t2d.yaml --shadow # full dry-run: cluster + record, no dumps

Utilities: `t2-dump` (manual smoke dump), `t2-replay` (offline replay of a
UTC slice for parameter tuning), `t2-inject` (injection daemon),
`t2-inject-report` (daily recovery report), `t2-transit-schedule`.

## Documentation

- [docs/architecture.md](docs/architecture.md) — data path, wire format,
  clustering, decision chain, database schema.
- [docs/operations.md](docs/operations.md) — deployment, configuration
  reference, runbooks, latency budget.

## License

MIT — see [LICENSE](LICENSE).
