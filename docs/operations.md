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

`cluster` sets the DBSCAN axis scales. Beams enter clustering as their
position on the sky, not as a beam index, because the beam grid is not
sky-ordered. The link scale on each axis is the beam FWHM on that axis:
`beam_fwhm_x_deg` 18.1 (East-West) and `beam_fwhm_y_deg` 3.9
(North-South), the standard alt/az-aligned ellipse from
`compute_beam_fwhm`. Two trials within one beam width of each other are one
event. These are a description of the beam, not tuning knobs — change them
when the weights change the beam. `beam_scale` is the fallback used only
when the weights registry cannot name a pointing table for the gulp — the log says so when that happens,
and `sky_extent_deg` is then left at 0 and nothing is tagged on it.

`coalesce_jobs` (8), `coalesce_max_s` (8.0) and `coalesce_s` (0.25)
control when a gulp flushes: all jobs in, plus a short quiet hold. A gulp
that hits `coalesce_max_s` with jobs still missing is **dropped whole** —
not clustered, not triggered — and recorded with `skipped: 1`. Do not raise
`coalesce_max_s` on the assumption that there is slack: the dump ring's
observed successful lag tops out at 50.8 s and retention beyond that is
unmeasured, while hella already spends 13-25 s of it.

The two numbers to watch are the heartbeat's skipped-gulp and late-batch
counters, and `SELECT count(*) FROM gulp_stats WHERE skipped = 1` over the
last hour. A rising skipped count is a hella job dying, not T2 being
cautious — find the job before assuming the sky was quiet.

`tiers` and `filters` (S/N thresholds, `beam_veto`, `max_nbeam`,
`max_sky_extent_deg`, `dm_floor`, `dm_floor_veto`) shape what counts as an
event. `max_sky_extent_deg` (25.0) tags a cluster `rfi_wide` when its
member beams span more than that on the sky; a real source spans about one
E-W beam width plus a beam spacing, so a wider cluster is broadband RFI no
matter how few beams it occupies. The DM-floor
veto tags a cluster `dm_floor` when its lowest member DM sits at hella's
first trial (`max_dm_lo`, 20.6 for DM_MIN 20) with boxcar index at or
above `min_width`; that is a zero-DM impulse leaking up the bowtie. The
tag spreads to every cluster in the same gulp within the occupancy window
of such a cluster, because DBSCAN splits one impulse into fragments and the
fragment starting at DM 26 would otherwise trigger. Tagged clusters are
stored and never dumped. `trigger.storm_lockout_s` (300 since 2026-09-09)
is the quiet time a blind trigger needs after the previous blind-eligible
event. `trigger` holds `fast_path`
(strict cluster-first when false), the per-kind budgets, `disk_floor_gb`,
and the voltage block, which ships `enabled: false`. `known_sources` is
per-source DM, transit schedule CSV, and `snr_min`. `injections` sets
cadence, parameter ranges, FIFO paths, and the scratch quota.

`dumps_enabled` ships **false** while the telescope is commissioning:
every trigger decision is still evaluated and written to `triggers` with
action `suppressed_commissioning`, but no dump command goes out and no
trigger card is spooled. Flip it to true to arm the dump path.

`veto_widths` and `max_cands_per_gulp` are the storm defences. Watch what
they cost with

    sqlite3 /mnt/nvme5/casm_pipeline/db/t2.sqlite \
      "SELECT date(gulp_utc), sum(n_cands), sum(n_vetoed), sum(n_shed)
         FROM gulp_stats GROUP BY 1 ORDER BY 1 DESC LIMIT 7;"

A `n_shed` that is nonzero outside a storm means the cap is too low. The
daemon logs the first shedding gulp and every 50th after it, with the
count it shed down to.

`n_cands` is the raw arrival count: `n_vetoed` is subtracted at parse time
(per batch, aggregated per gulp) and `n_shed` by the cap, so DBSCAN saw
`n_cands - n_vetoed - n_shed`. That figure is bounded by
`max_cands_per_gulp + 4 x 512`, whatever the storm looks like.

## Watchdog

t2d is a `Type=notify` unit: it reports READY only once all eight ingest
ports are bound, and its 60 s heartbeat sends WATCHDOG=1 from the asyncio
event loop. `WatchdogSec=180` means two missed beats gets it killed and
restarted. That is deliberate — the failure mode this guards against is
the event loop wedging, which back-pressures the whole telescope DAQ, and
a restart costs only the in-flight gulp. `systemctl --user show t2d -p
WatchdogTimestamp` shows the last successful ping.

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

Force one shot's parameters:

    t2-inject config/t2d.yaml --once --beam 90 --dm 300 \
        --fwhm-ms 11.8 --inject-snr 20

`--fwhm-ms` is the width argument. `--sigma-ms` still works and is
converted (FWHM = 2.355 sigma), but it logs a deprecation line.
`--inject-snr` is the injected (true) S/N, the same quantity the daemon
samples. `--target-snr` is still accepted and means a hella-*reported* S/N;
it is converted through the per-width `rec_per_true` table. Either way the
shot is clamped to `injection.reported_snr_cap` for its width: a test shot
is not a reason to blind the search for a gulp.

The narrowest pulse that can be injected is **FWHM 2.47 ms**. That is not a
policy choice: `make_noise_fil_with_frb_snr.py` renders with
`sigma_samp = max(1.0, ...)` and offers no argument to lower it, so a
narrower request silently comes out at one sample of sigma. Reachable
trials are therefore ibox 1 to 5; ibox 0 cannot be targeted (it shares a
1.0 ms kernel FWHM with ibox 1 in any case), and ibox 6 is vetoed by t2d.
If narrow-end coverage matters, the fix is an argument in that script, not
a config change here.

### Automated replay

The truth plot the daemon renders comes from the .fil that was pushed into the
FIFO, so it shows the pulse as generated. It cannot show what the pulse looked
like in the live stream, because intensity dumps tap upstream of the injection
merge - which is exactly what you want when a shot comes back at half the
expected S/N, or not at all.

The replay closes that: dump the injected stream around the shot, add the
pulse to the dump where it should have arrived, render it, and thread the PNG
under the shot's own Slack message. On a miss it shows what hella should have
found and did not.

    injection:
      reconcile_wait_s: 90
      replay:
        post: daily             # daily | on_miss | always | never
        dump_pre_s: 24
        dump_post_s: 10
        dump_timeout_s: 60
        keep_dump: false
        events_root: /mnt/nvme3/T3/EVENTS
        command: t3-replay-injection

`post` decides when the PNG is posted: `always` every shot, `on_miss` only
misses, `daily` every miss plus the first shot of each UTC day, `never` nothing
(and then no dump is requested at all). The dump is taken on every shot
whenever posting is possible, because it cannot be taken retrospectively.

The window is `[inject_utc - dump_pre_s, inject_utc - dump_post_s]` - both
offsets go backwards, because the pulse lands before the FIFO write: the
sidecar joins a gulp whose samples are already 5-20 s old. The dump daemon
replies only after writing, and a 14 s window took about 20 s on 2026-09-09,
hence the timeout.

Timeline per shot, from the FIFO write:

| time | what |
| --- | --- |
| 0 s | ledger row, FIFO write, Slack "injection sent" |
| ~20 s | the intensity dump completes |
| ~100 s | reconcile; the outcome edits the Slack message |
| ~110 s | the replay PNG lands as a thread reply under it |

Archive layout, one directory per shot under `events_root`:

    inj667/inj667.png     the replay plot
    inj667/inj667.json    the synthetic card
    inj667/inj667.fil     the beam with the pulse added, float32 single-beam

The dump itself is deleted after the plot unless `keep_dump: true`. Everything
in the chain is fail-soft: a failed dump, render or post leaves the injection
and its ledger row alone, and the shot is still reconciled normally.

### Injection messages in Slack

The daemon can post one Slack message per injection: a "sent" line when
the pulse hits the FIFO, edited in place ~3 minutes later with a green or
red bar carrying the outcome. A run of consecutive misses (default 5, and
each multiple after that) posts one attention message; the streak is read
straight off the ledger, so restarts cannot double-count it.

**This ships disabled** — `injection.slack.enabled: false` in t2d.yaml.
With it false the daemon posts nothing and behaves exactly as before.

    injection:
      slack:
        enabled: false          # the only switch that turns posting on
        dry_run_dir: null       # set a path: write .txt files, no network
        channel: null           # overrides the dotfiles
        streak_every: 5         # attention message at each multiple

Token and channel come from the same dotfiles casm_t3 uses,
`~/.config/slack_api` and `~/.config/slack_channel`, plus an optional
`~/.config/slack_channel_injections` that overrides the channel when it
exists, so injection chatter can be kept out of the candidate channel.
Every Slack failure is logged and swallowed.

Before enabling it, render the messages offline — no token, no network:

    t2-inject-slack-preview --db /mnt/nvme5/casm_pipeline/db/t2.sqlite \
        --out /tmp/inject_preview --ids 657,658,659

That writes, per shot, the sent text, the outcome text and a PNG card of
each (the colour bar is the attachment colour Slack would show), plus the
daily summary text and two untitled figures: recovered vs injected S/N
with the 1:1 line and misses hollow at zero, and outcome counts. With no `--ids` it takes
a whole UTC day (`--day`). `--web-base` sets the host the links point at.

The messages:

    injection 660 sent: beam 150, DM 300, FWHM 4.7 ms, injected S/N 28
    injection 671 sent: beam 90, DM 300, FWHM 11.8 ms, injected S/N 16 (IB sub off)
    recovered -> <.../injections/plot/inj_..._b150|inj_..._b150> | SNR 43.0 (ratio 1.54) | DM 299.8 (delta -0.2) | beam 150 | width 3.1 ms (ibox 2)

Incoherent-beam subtraction is normally on and says nothing; only the
unusual state is called out. The link goes to the shot's truth plot on the
T3 web app; a shot whose
cluster triggered a dump links to its event page instead, which carries the
dump, the plot and the trigger audit. `injection.slack.web_base` sets the
base URL, defaulting to `http://127.0.0.1:8050` like t3-collect's
`--web-base`.

`ratio` is recovered over injected S/N. The beam is where it came back:
recovered in the injected beam it is a bare `beam 150`, and in any other
beam it carries the sky separation of the two pointings, `beam 152 (offset
3600 arcsec)`, taken from the pointing table live at the injection time
(neighbouring beam indices are not a fixed angle apart, so the index alone
would not say how far). With no pointing table it reads `beam 152 (offset
n/a)`. The width is last so it is easy to drop, and is the kernel FWHM for
that trial, not `2**ibox` samples.

The daily summary is one top-level message with the two figures as replies
in its thread.

The injected S/N is the generator's own matched-filter estimate of the pulse
that was actually written (`est_snr`, its `INJECTED_SNR_ESTIMATE`), falling
back to the value the solver aimed at (`inject_snr`) when the generator
printed nothing. Slack never quotes a predicted reported S/N: that lives in
`target_snr` in the ledger, where the cap logic uses it.

A shot that resolves badly names the stage and what the evidence there was:

    NOT recovered: lost at T1: no matching trial in beam 200 (+-2) within the window at DM 500 (+-75)
    NOT recovered: lost at T2: 7 matching T1 trials (best S/N 9.2) but no cluster formed (min 5 members)
    injection not fired: FIFO write failed

Recovered means the search found it, at any S/N: an injection recovered at
S/N 15.8 reads like any other. The trigger filters no longer make an
outcome - `gate_trigger` still records whether it would also have earned a
dump. Separating a T1 miss from a T2 clustering miss reads hella's raw
candidate file; see `docs/architecture.md`.

The last is injector plumbing, not a pipeline miss: it gets a grey bar
rather than a red one and never counts toward a miss streak.

Rows written before these columns existed still have `est_snr`, so they
show a real injected S/N and do appear in the recovery figure.

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
and if you use it, move or label the files the moment they land. One caveat
in the other direction: the janitor's voltage patrol lists `cand_dumps/`
itself, not the `stream_N/` subdirectories the files actually land in, so
today it deletes nothing there. The 150 GB is the CLI's budget; clearing the
trees is manual.

Before sending it prints the window and the disk arithmetic, then asks for
confirmation (`-y` skips it, `--dry-run` prints and sends nothing). 2 s
across all six streams is 12.4 GB on each node. The disk guard is stricter
than the raw size: casm_cand_dump_disk wants room for all three streams a
node hosts whether or not you commanded them, so a 10 s dump of stream 3
alone needs 61.9 GB free on corr2, not the 20.6 GB it writes. Under the
guard it drops the dump — silently as far as this client is concerned; the
daemon does log the refusal, so look at the casm_cand_dump_disk log on the
owning node. The CLI also refuses outright if the dump would leave
`/mnt/nvme4` under 200 GB free, the headroom the live recorders need
(`--force` overrides that too).

So "OK" means the daemon took the command, not that the data reached the
disk. Check `/mnt/nvme4/data/casm/cand_dumps/stream_N/` on the owning node
afterwards. The same goes the other way: a connection error or timeout does
not mean the dump didn't run, so look before retrying, or you get a second
overlapping dump under the same `UTC_START`. Read the files back with
casm_io's `VoltageReader` — `casm_io/examples/voltage_dumps.py` walks one
end to end.

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
