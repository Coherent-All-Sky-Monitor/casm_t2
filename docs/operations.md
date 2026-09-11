# casm_t2 operations

## Deployment

systemd user units, `deploy/systemd/`. Run `loginctl enable-linger` once
per node or they die at logout.

| unit | host | role |
|---|---|---|
| t2d | corr1 | the trigger daemon; owns the hella candidate ports |
| t2-inject | corr1 | scheduled live injections |
| t2-inject-report.timer | corr1 | daily recovery report |

The second backend node runs no casm_t2 services; t2d commands its dump daemons
over TCP. Config changes need `systemctl --user restart t2d`, which costs the
in-flight gulp and nothing else, budgets rebuilding from the DB.

### Outbound network from the correlator nodes

corr1 and corr2 have no working DNS. All outbound HTTP goes through the zapdos
privoxy at http://10.70.0.1:8118. User units inherit the proxy from
`~/.config/environment.d/proxy.conf` (`https_proxy` and `http_proxy`) on both
nodes. Units that post to Slack also carry explicit `Environment=` lines for the
same proxy. A unit with neither fails with `NameResolutionError`.

## Configuration

`config/t2d.yaml` is the only user config. The blocks that get touched:

`cluster` sets the DBSCAN axis scales. Beams enter clustering as their position
on the sky, not as a beam index, the grid not being sky-ordered. The link scale
on each axis is the beam FWHM on that axis, an alt/az-aligned ellipse from
`compute_beam_fwhm`, taken from the live weights product; the
`beam_fwhm_x_deg` / `beam_fwhm_y_deg` values here are the fallback for a product
with no ellipse. Two trials within one beam width of each other are one event.
`beam_scale` is the fallback for when the weights registry cannot name a
pointing table for the gulp, which is logged, and `sky_extent_deg` is then left
at 0 and nothing is tagged on it.

`coalesce_jobs` (8), `coalesce_max_s` (8.0) and `coalesce_s` (0.25) control when
a gulp flushes: all jobs in, plus a short quiet hold. A gulp that hits
`coalesce_max_s` with jobs still missing is dropped whole, not clustered and not
triggered, and recorded with `skipped: 1`. Do not raise `coalesce_max_s`: the
dump ring's measured successful lag tops out at 50.8 s, retention beyond that is
unmeasured, and hella already spends 13-25 s of it.

Watch the heartbeat's skipped-gulp and late-batch counters, and
`SELECT count(*) FROM gulp_stats WHERE skipped = 1` over the last hour. A rising
skipped count means a hella job is dying, so find the job before assuming the
sky was quiet.

`tiers` and `filters` (S/N thresholds, `beam_veto`, `max_nbeam`,
`max_sky_extent_deg`, `dm_floor`, `dm_floor_veto`) shape what counts as an
event. `max_sky_extent_deg` tags a cluster `rfi_wide` when its member beams span
more than that on the sky: a real source spans about one E-W beam width plus a
beam spacing, so a wider cluster is broadband RFI however few beams it occupies.
The DM-floor veto tags a cluster `dm_floor` when its lowest member DM sits at
hella's first trial (`max_dm_lo`, 20.6 for DM_MIN 20) with boxcar index at or
above `min_width`, which is a zero-DM impulse leaking up the bowtie. The tag
spreads to every cluster in the same gulp within the occupancy window of such a
cluster, DBSCAN splitting one impulse into fragments of which the higher-DM ones
would otherwise trigger. Tagged clusters are stored and never dumped.
`trigger.storm_lockout_s` is the quiet time a blind trigger needs after the
previous blind-eligible event. `trigger` also holds `fast_path` (strict
cluster-first when false), the per-kind budgets, `disk_floor_gb`, and the voltage
block, which ships `enabled: false`. `known_sources` is per-source DM, transit
schedule CSV and `snr_min`. `injection` sets cadence, parameter ranges, FIFO
paths and the scratch quota.

`dumps_enabled` false evaluates every trigger decision and writes it to
`triggers` with action `suppressed_commissioning`, but sends no dump command and
spools no trigger card. True arms the dump path.

`veto_widths` and `max_cands_per_gulp` are the storm defences. Watch what
they cost with

    sqlite3 /mnt/nvme5/casm_pipeline/db/t2.sqlite \
      "SELECT date(gulp_utc), sum(n_cands), sum(n_vetoed), sum(n_shed)
         FROM gulp_stats GROUP BY 1 ORDER BY 1 DESC LIMIT 7;"

A nonzero `n_shed` outside a storm means the cap is too low. The daemon logs
the first shedding gulp and every 50th after it, with the count it shed down
to.

`n_cands` is the raw arrival count: `n_vetoed` is subtracted at parse time
(per batch, aggregated per gulp) and `n_shed` by the cap, so DBSCAN saw
`n_cands - n_vetoed - n_shed`. That figure is bounded by
`max_cands_per_gulp + 4 x 512`, whatever the storm looks like.

## Watchdog

t2d is a `Type=notify` unit: READY only once all eight ingest ports are bound,
and a 60 s heartbeat sending WATCHDOG=1 from the asyncio event loop.
`WatchdogSec=180` kills and restarts it after two missed beats. The failure mode
guarded against is the event loop wedging, which back-pressures the telescope
DAQ, and a restart costs only the in-flight gulp. `systemctl --user show t2d -p
WatchdogTimestamp` shows the last successful ping.

## Runbooks

Smoke-test a dump path:

    t2-dump --stream 2 --last 5

then check the stream's dump directory on the owning node.

Replay a UTC slice offline for tuning or post-mortems. Reads the on-disk T1
files only and never dumps; the CSV loads straight into hiplot:

    t2-replay --from 2026-06-11T03:00:00 --to 2026-06-11T04:00:00 \
              --config config/t2d.yaml --csv /tmp/clusters.csv

One manual injection, then verify the ledger row appears, the cluster a
minute later is tagged `injection`, and no dump fires:

    t2-inject config/t2d.yaml --once

Force one shot's parameters:

    t2-inject config/t2d.yaml --once --beam 90 --dm 300 \
        --fwhm-ms 11.8 --inject-snr 20

`--fwhm-ms` is the width argument. `--sigma-ms` still works, converted as
FWHM = 2.355 sigma, and logs a deprecation line. `--inject-snr` is the injected
(true) S/N, the quantity the daemon samples. `--target-snr` is accepted and
means a hella-reported S/N, converted through the per-width `rec_per_true`
table. Either way the shot is clamped to `injection.reported_snr_cap` for its
width.

The narrowest injectable pulse is FWHM 2.47 ms:
`make_noise_fil_with_frb_snr.py` renders with `sigma_samp = max(1.0, ...)` and
offers no argument to lower it, so a narrower request comes out at one sample of
sigma. Reachable trials are ibox 1 to 5; ibox 0 cannot be targeted (it shares a
1.0 ms kernel FWHM with ibox 1) and ibox 6 is vetoed by t2d. Narrow-end coverage
needs an argument in that script, not a config change here.

### Automated replay

The daemon's truth plot comes from the .fil pushed into the FIFO, so it shows
the pulse as generated. Intensity dumps tap upstream of the injection merge, so
they cannot show the pulse in the live stream. The replay closes that: dump the
injected stream around the shot, add the pulse where it should have arrived, and
render it. On a miss it shows what hella should have found.

    injection:
      reconcile_wait_s: 90
      replay:
        post: always            # always | daily | on_miss | never
        dump_pre_s: 24
        dump_post_s: 10
        dump_timeout_s: 60
        keep_dump: false
        events_root: /mnt/nvme3/T3/EVENTS
        command: t3-replay-injection

`post` decides when the plot is rendered and posted: `always` (the shipped
setting) every shot, `on_miss` only misses, `daily` every miss plus the first
shot of each UTC day, `never` nothing (and then no dump is requested at all).
The dump is taken on every shot whenever posting is possible, because it
cannot be taken retrospectively. In `single` mode a shot with no plot still
gets its message, as text.

The window is `[inject_utc - dump_pre_s, inject_utc - dump_post_s]`. Both
offsets go backwards, the pulse landing before the FIFO write: the sidecar joins
a gulp whose samples are already 5-20 s old. The dump daemon replies only after
writing, and a 14 s window takes about 20 s, hence the timeout.

Timeline per shot, from the FIFO write:

| time | what |
| --- | --- |
| 0 s | ledger row, FIFO write, the sent line posts |
| ~20 s | the intensity dump completes |
| ~90 s | reconcile: the outcome is known |
| ~100-110 s | the replay renders and the card is completed in place |

In `single` mode nothing posts at 0 s and the whole card appears at the end.

Every shot has a display name, `inj_YYYYMMDD_NNNN`, counting from 0001 each UTC
day. It is the ledger's `file_id` and appears in Slack, in the plot title and as
the archive directory name; the integer row id stays the primary key. The
counter reads the day's highest recorded name, so a restart continues the
sequence.

Archive layout, one directory per shot under `events_root`:

    inj_20260910_0002/inj_20260910_0002.png     the replay plot
    inj_20260910_0002/inj_20260910_0002.json    the synthetic card
    inj_20260910_0002/inj_20260910_0002.fil     the beam with the pulse added

Cleanup deletes only this shot's own `.dada` files, selected by the window each
file covers (from its name and size) against the recorded
`dump_utc_start..dump_utc_stop`, and never the directory: `dump_dir` is the
per-stream directory T2's triggered dumps also write to. It deletes nothing if
the selection returns more than four files or if no window was recorded, and
logs every deletion by full path. Set `keep_dump: true` to keep them. The chain
is fail-soft: a failed dump, render or post leaves the injection and its ledger
row alone, and the shot is still reconciled.

### Injection messages in Slack

The daemon posts one Slack message per injection, and completes it in place.

In the default `sent_then_update` mode the sent line goes up the moment the
pulse hits the FIFO, so the channel shows a shot in flight:

    injection inj_20260910_0002 sent: beam 220, DM 300, FWHM 11.8 ms, injected S/N 25
    _awaiting recovery..._

About 100-110 s later that same message is updated: the awaiting tail goes,
and a bar coloured by outcome (green recovered, red missed, grey not fired)
appears under it carrying the result line and the replay plot inline.

    injection inj_20260910_0002 sent: beam 220, DM 300, FWHM 11.8 ms, injected S/N 25
    | recovered -> SNR 24.6 (ratio 0.99) | DM 299.8 (delta -0.2) | beam 220 | width 11.5 ms (ibox 4)
    | [replay plot]

The message `text` stays the plain sent line, so notifications and the channel
list read sensibly. The plot gets inside the bar by being uploaded without a
channel, `files.completeUploadExternal` with no `channel_id`, so the bot owns a
file that appears nowhere, then referenced from an image block by id. Sharing it
to a channel instead gives the picture its own message.

Four forms, most to least shown, with the one used named in the log:

| form | what the card ends up as | when |
| --- | --- | --- |
| inline | bar, result line, image | the intended shape |
| bar | bar and result line, no image | the upload failed |
| bar+thread | bar and result line, plot as a reply | the workspace refused the blocks |
| new message | the card posted fresh | the original could not be edited |

`bar+thread` matters because `slack_file` image blocks need the app permitted
to reference its own files; without that the update returns `invalid_blocks` and
the plot arrives under the same message instead.

Two other modes are selectable. `single` posts nothing at fire time and one
finished card once the outcome and the plot both exist, so the channel says
nothing for the first 100 s. `sent_then_edit` is the original two-step shape,
with the plot threaded rather than inline.

A run of consecutive misses (default 5, and each multiple after) posts one
attention message. The streak is read off the ledger, so restarts cannot
double-count it.

`injection.slack.enabled: false` turns posting off entirely: the daemon then
posts nothing.

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

Render the messages offline, with no token and no network:

    t2-inject-slack-preview --db /mnt/nvme5/casm_pipeline/db/t2.sqlite \
        --out /tmp/inject_preview --ids 657,658,659

That writes, per shot, the sent text, the outcome text and a PNG card of each
(the colour bar is the attachment colour Slack would show), plus the daily
summary text and one figure: injected against recovered S/N with the 1:1 line
and misses hollow at zero, coloured by DM bin. With no `--ids` it takes a whole
UTC day (`--day`). `--web-base` sets the host the links point at.

The messages:

    injection 660 sent: beam 150, DM 300, FWHM 4.7 ms, injected S/N 28
    injection 671 sent: beam 90, DM 300, FWHM 11.8 ms, injected S/N 16 (IB sub off)
    recovered -> SNR 43.0 (ratio 1.54) | DM 299.8 (delta -0.2) | beam 150 | width 3.1 ms (ibox 2)

Incoherent-beam subtraction on is the standing state and says nothing; only the
other state is called out. The link goes to the shot's truth plot on the T3 web
app, or to the event page when the shot's cluster triggered a dump, that page
carrying the dump, the plot and the trigger audit. `injection.slack.web_base`
sets the base URL, defaulting to `http://127.0.0.1:8050`.

`ratio` is recovered over injected S/N. The beam is where it came back: the
injected beam reads as a bare `beam 150`, any other beam carries the sky
separation of the two pointings, `beam 152 (offset 3600 arcsec)`, from the
pointing table live at the injection time, neighbouring indices not being a fixed
angle apart. With no pointing table it reads `beam 152 (offset n/a)`. The width
is the kernel FWHM for that trial, not `2**ibox` samples.

The daily summary is one top-level message with the figure as a reply in its
thread. The outcome counts are in the text, not a chart.

The injected S/N is the value the solver aimed at (`inject_snr`), falling back
to the generator's own matched-filter estimate (`est_snr`). Slack never quotes a
predicted reported S/N; that is `target_snr` in the ledger, where the cap logic
uses it.

A shot that resolves badly names the stage and what the evidence there was:

    NOT recovered: lost at T1: no matching trial in beam 200 (+-2) within the window at DM 500 (+-75)
    NOT recovered: lost at T2: 7 matching T1 trials (best S/N 9.2) but no cluster formed (min 5 members)
    injection not fired: FIFO write failed

Recovered means the search found it, at any S/N. The trigger filters do not
make an outcome; `gate_trigger` still records whether the shot would also have
earned a dump. Separating a T1 miss from a T2 clustering miss reads hella's raw
candidate file, see `docs/architecture.md`.

The last line is injector plumbing rather than a pipeline miss: a grey bar, and
it never counts toward a miss streak.

Rows written before these columns existed still carry `est_snr`, so they show an
injected S/N and appear in the recovery figure.

## Voltage dumps (manual)

`casm-voltage-dump` commands the antenna-side casm_cand_dump daemons (ports
27000-27005, streams 0-2 on corr1 and 3-5 on corr2). They belong to the Fourier
Space stack and know nothing about T2, so this works with t2d stopped, and it is
the only way to get voltages while `trigger.voltage` stays `enabled: false`.

    casm-voltage-dump --next 2                  # 2 s starting 5 s from now
    casm-voltage-dump --last 5                  # the 5 s ending 2 s ago
    casm-voltage-dump --streams 3,4 --next 10   # corr2 only
    casm-voltage-dump --start 2026-07-31-18:00:00 --stop 2026-07-31-18:00:02

Use `--next` for anything long: the window is in the future, so ring depth
stops mattering. `--last` and explicit windows are limited by the daemons'
`-d 28`. The CLI refuses a `--last` starting more than 26 s back, and rechecks
that after the confirmation prompt.

Dump length is limited by disk, not the ring. Voltage data is 2.0625 GB/s per
stream and the casm_t3 janitor holds each `stream_N` tree to 150 GB, oldest
first, about 72 s per stream. Beyond that the tail of a dump eats its own head.
The CLI refuses such a request and `--force` overrides it; move or label the
files as they land. The janitor's voltage patrol lists `cand_dumps/` itself, not
the `stream_N/` subdirectories the files land in, so it currently deletes nothing
there: the 150 GB is the CLI's budget and clearing the trees is manual.

Before sending, the CLI prints the window and the disk arithmetic and asks for
confirmation (`-y` skips it, `--dry-run` prints and sends nothing). 2 s across
all six streams is 12.4 GB per node. The disk guard is stricter than the raw
size: casm_cand_dump_disk wants room for all three streams a node hosts whether
or not they were commanded, so a 10 s dump of stream 3 alone needs 61.9 GB free
on corr2, not the 20.6 GB it writes. Under the guard it drops the dump, telling
the client nothing, so read the casm_cand_dump_disk log on the owning node. The
CLI also refuses outright if the dump would leave `/mnt/nvme4` under 200 GB
free, the headroom the live recorders need (`--force` overrides that too).

"OK" means the daemon took the command, not that the data reached the disk.
Check `/mnt/nvme4/data/casm/cand_dumps/stream_N/` on the owning node afterwards.
A connection error or timeout does not mean the dump did not run, so look before
retrying or you get a second overlapping dump under the same `UTC_START`. Read
the files back with casm_io's `VoltageReader`;
`casm_io/examples/voltage_dumps.py` walks one end to end.

## When misses pile up

`refused_daemon` rows (red on the web UI) mean T2's dump command arrived after
the event left the intensity ring. T2 contributes under a second to that race,
the other 13-25 s being T1's reporting latency. If misses become common the fix
is upstream, a deeper ring or shorter gulps in the backend config, not in this
repo.

## Disk safety

The dump disks run close to full, so scarcity is enforced in the trigger path:
free-space floor on the receiving filesystem, daily caps, minimum spacing, one
dump per gulp. Every refusal lands in the `triggers` table with a reason. Fix
whatever is eating the disk before lifting a cap.
