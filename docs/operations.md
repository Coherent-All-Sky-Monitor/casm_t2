# casm_t2 operations

## Deployment

Everything runs as systemd user units (`deploy/systemd/`, requires
`loginctl enable-linger`):

| unit | host | role |
|---|---|---|
| `t2d` | corr1 | the trigger daemon (owns the hella candidate ports) |
| `t2-inject` | corr1 | scheduled live injections |
| `t2-inject-report.timer` | corr1 | daily recovery report |

Install with `pip install -e .` into the shared venv, copy units to
`~/.config/systemd/user/`, then `systemctl --user enable --now <unit>`.
The second backend node needs no casm_t2 services; t2d commands its dump
daemons over TCP.

## Configuration

One YAML (`config/t2d.yaml`) is the only user config. Key blocks:

- `ports` / `listen_host` — the eight hella candidate ports.
- `cluster` — DBSCAN scales (`eps`, `min_samples`, per-axis scales).
- `tiers` — S/N thresholds for A/B/C.
- `filters` — `beam_veto`, `max_nbeam`, `dm_floor`.
- `known_sources` — per-source DM, transit schedule CSV, `snr_min`.
- `trigger` — `fast_path` (strict cluster-first when false), per-kind
  budgets (`min_spacing_s`, `daily_max`), `disk_floor_gb`, voltage block
  (`enabled: false` by default), dump pre/post seconds.
- `injections` — cadence, parameter ranges, FIFO paths, scratch quota.
- `db` — SQLite path.

Config changes take effect on `systemctl --user restart t2d`. Restarting
loses only the in-flight gulp; the budgets are rebuilt from the DB.

## Runbooks

**Smoke-test a dump path**

    t2-dump --stream 2 --last 5

Requests a 5 s dump on stream 2's owning node; check the dump directory
and the daemon reply.

**Replay a UTC slice offline** (parameter tuning, post-mortems)

    t2-replay --from 2026-06-11T03:00:00 --to 2026-06-11T04:00:00 \
              --config config/t2d.yaml --csv /tmp/clusters.csv

Reads the on-disk T1 `.dat` files only; never dumps. The CSV loads
directly into hiplot for interactive exploration.

**One manual injection**

    t2-inject config/t2d.yaml --once

Verify: ledger row appears, the cluster a minute later is tagged
`injection`, and no dump fires.

**Latency budget** (why misses happen)

T1 reports a pulse 13-25 s after it happened (gulp fill + ring hand-offs
+ search compute). T2 adds well under a second (coalesce wait + DBSCAN +
dump RTT). The dump daemon can only reach back as far as its ring holds.
If misses (`refused_daemon` in the audit, red on the web) become common,
the fix is a deeper ring or shorter gulps upstream — both backend config
changes, not T2 code.

## Disk safety

Hard constraints, all enforced in the trigger path: free-space floor on
the receiving filesystem, per-kind daily caps, minimum spacing, one dump
per gulp during storms. Every refusal is recorded with its reason in the
`triggers` table.
