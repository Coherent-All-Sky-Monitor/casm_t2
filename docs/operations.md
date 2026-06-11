# casm_t2 operations

## Deployment

systemd user units, `deploy/systemd/`. Run `loginctl enable-linger` once
per node or they die at logout.

| unit | host | role |
|---|---|---|
| t2d | corr1 | the trigger daemon; owns the hella candidate ports |
| t2-inject | corr1 | scheduled live injections |
| t2-inject-report.timer | corr1 | daily recovery report |

The second backend node runs no casm_t2 services — t2d commands its dump
daemons over TCP. Config changes need `systemctl --user restart t2d`,
which costs the in-flight gulp and nothing else; budgets rebuild from the
DB.

## Configuration

`config/t2d.yaml` is the only user config. The blocks you'll actually
touch:

`tiers` and `filters` (S/N thresholds, `beam_veto`, `max_nbeam`,
`dm_floor`) shape what counts as an event. `trigger` holds `fast_path`
(strict cluster-first when false), the per-kind budgets, `disk_floor_gb`,
and the voltage block, which ships `enabled: false`. `known_sources` is
per-source DM, transit schedule CSV, and `snr_min`. `injections` sets
cadence, parameter ranges, FIFO paths, and the scratch quota.

## Runbooks

Smoke-test a dump path:

    t2-dump --stream 2 --last 5

then check the stream's dump directory on the owning node.

Replay a UTC slice offline (tuning, post-mortems) — reads the on-disk T1
files only, never dumps; the CSV loads straight into hiplot:

    t2-replay --from 2026-06-11T03:00:00 --to 2026-06-11T04:00:00 \
              --config config/t2d.yaml --csv /tmp/clusters.csv

One manual injection, then verify the ledger row appears, the cluster a
minute later is tagged `injection`, and no dump fires:

    t2-inject config/t2d.yaml --once

## When misses pile up

`refused_daemon` rows (red on the web UI) mean T2's dump command arrived
after the event left the intensity ring. T2 contributes under a second to
that race; the other 13-25 s is T1's reporting latency. If misses become
common the fix is upstream — a deeper ring or shorter gulps in the
backend config — not in this repo.

## Disk safety

The dump disks run close to full, so scarcity is enforced in the trigger
path itself: free-space floor on the receiving filesystem, daily caps,
minimum spacing, one dump per gulp in a storm. Every refusal lands in the
`triggers` table with a reason. Don't lift the caps to "catch up" — fix
whatever is eating the disk first.
