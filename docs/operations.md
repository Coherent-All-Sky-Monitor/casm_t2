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

## Voltage dumps (manual)

`casm-voltage-dump` commands the antenna-side casm_cand_dump daemons
(ports 27000-27005, streams 0-2 on corr1 and 3-5 on corr2). They belong to
the Fourier Space stack and know nothing about T2, so this works with t2d
stopped — and it is the only way to get voltages while `trigger.voltage`
stays `enabled: false`.

    casm-voltage-dump --next 2                  # 2 s starting 5 s from now
    casm-voltage-dump --last 5                  # the 5 s ending 2 s ago
    casm-voltage-dump --streams 3,4 --next 10   # corr2 only
    casm-voltage-dump --start 2026-07-31-18:00:00 --stop 2026-07-31-18:00:02

Use `--next` for anything long: the window is in the future, so ring depth
stops mattering. `--last` and explicit windows are limited by the daemons'
`-d 28`; the CLI refuses a `--last` that starts more than 26 s back rather
than let you watch it fail, and re-checks that after the confirmation
prompt in case you took your time answering.

How long a dump can actually be is set by disk, not by the ring. Voltage
data is 2.0625 GB/s per stream and the casm_t3 janitor holds each
`stream_N` tree to 150 GB, deleting oldest-first — about 72 s per stream of
retention. Ask for more than that and the tail of your own dump starts
eating the head of it. The CLI refuses such a request; `--force` overrides,
and if you use it, move or label the files the moment they land.

Before sending it prints the window and the disk arithmetic, then asks for
confirmation (`-y` skips it, `--dry-run` prints and sends nothing). 2 s
across all six streams is 12.4 GB on each node. The disk guard is stricter
than the raw size: casm_cand_dump_disk wants room for all three streams a
node hosts whether or not you commanded them, so a 10 s dump of stream 3
alone needs 62 GB free on corr2, not 21 GB. Under the guard it drops the
dump — silently as far as this client is concerned; the daemon does log the
refusal, so look at the casm_cand_dump_disk log on the owning node. The CLI
also refuses outright if the dump would leave `/mnt/nvme4` under 200 GB
free, the headroom the live recorders need (`--force` overrides that too).

So "OK" means the daemon took the command, not that the data reached the
disk. Check `/mnt/nvme4/data/casm/cand_dumps/stream_N/` on the owning node
afterwards.

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
