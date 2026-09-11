# casm_t2 architecture

## Data path

Eight hella jobs, one per 64-beam stream, jobs 0-3 on the first backend node
and 4-7 on the second, connect per gulp and send their candidate list. t2d
coalesces the batches, deduplicates and clusters, classifies, fires whatever
dumps survive the policy, and writes the decision chain to SQLite.

## Coalescing

A gulp is only a gulp once all eight jobs have reported. They finish seconds
apart, so the coalescer holds a (utc_start, gulp) key open until every expected
job has arrived and `coalesce_s` of quiet has passed. The wait is recorded in
`gulp_stats.coalesce_wait_ms` next to `n_jobs`.

If `coalesce_max_s` elapses first with jobs still missing, the gulp is dropped
whole: not clustered, not triggered, recorded with `skipped: 1` and the counts
that did arrive. Every per-gulp veto reasons about the whole sky, and the
fragment holding the bright beam need not hold the evidence against it. The row
still goes in so the gap stays visible in the duty cycle, and the heartbeat
counts skipped gulps; a rising count means a hella job is dying. Completeness is
read when the wait ends, not from which deadline fired, so a gulp whose batches
trickle in is complete rather than skipped.

A batch arriving after its key has flushed is still processed, as its own
fragment, and logged: a job that late means the vetoes did not see the whole sky.

`coalesce_max_s` ships at 8.0 s, one gulp length. The budget is not elastic:
measured successful event-to-request lag is 36.5-50.8 s, retention behind the
ring's read pointer beyond that is unmeasured, and hella's own 13-25 s reporting
delay spends most of it. A dump command arriving after the event has left the
ring is refused by the dump daemon and recorded as a miss. T2's own latency is
well under a second, against a budget it does not control.

## Wire format

One preamble line, then one candidate per line:

    <gulp> <utc_start> <x> <tsamp_us>
    snr samp time_days width dm_idx dm beam

`beam` is global (0-511); a legacy 8-column variant still parses (wire.py).
Event times come from `utc_start` plus `samp` x tsamp, tsamp = 1.048576 ms, never
1.0 ms. The `time_days` column is not used (timing.py).

## Storm defences

DBSCAN cost is superlinear in candidate count and the coalescer feeds it all
eight jobs at once. A storm gulp of 80,000 trials takes 83-137 s of clustering
against an 8.7 s real-time budget, stalling ingest and back-pressuring the DAQ.
Two knobs shed load before clustering, both counted per gulp:

`veto_widths` drops whole boxcar-width indices at parse time, ahead of the fast
path and the trigger-card context. Index 6 (67 ms) is 97-98.6% of stored rows on
a quiet day, red-noise junk at DM >= 200. An empty list disables it. It discards
real data, so it defaults off in code and is turned on in the shipped config.

`max_cands_per_gulp` bounds what reaches DBSCAN, in two stages. Stage 1
gives each global beam a quota of its top `ceil(cap/64)` by S/N, which
handles RFI concentrated in a few beams and keeps the shed fair across the
sky. Stage 2 runs only if that still leaves more than the cap: keep the
global top `cap` by S/N, then add back every populated beam's top 4.

Stage 2 is not optional. Stage 1's allowance is quota x populated beams,
313 x 512 at the shipped cap, so a storm spread evenly over every beam passes
straight through it. The per-beam floor in stage 2 keeps the global truncation
safe: a plain global top-N would let a bright storm elsewhere evict a
single-beam FRB. Worst case kept is `cap + 4 x 512` ~ 22k.

## Clustering

DBSCAN, Euclidean metric, over scaled (samp, dm_idx, log2(width), x, y) where
(x, y) is the beam's position in degrees on a tangent plane about the zenith, x
East-West and y North-South, scaled by `beam_fwhm_x_deg` and `beam_fwhm_y_deg`.
An offset of exactly one scale on a single axis is a distance of exactly 1, so
eps 1.0 keeps its meaning; offsets on several axes add in quadrature.

Each cluster keeps its peak trial and the membership envelope: time, DM and beam
ranges, member count, distinct-beam count, and `sky_extent_deg`, the largest
pairwise great-circle separation of its member beams. Noise points survive as
singleton clusters, a lone bright pulse in one beam being what an FRB looks like.

Clustering is on sky position, not beam index: the deployed 512-beam grid is not
sky-ordered, consecutive indices being a median 16 deg apart against 3.1 deg for
true sky neighbours, and only 11% of a beam's six sky-nearest neighbours lie
within +-4 index. On the index, point sources spanning two adjacent sky beams
split, `n_beams` understated every footprint, and `rfi_wide` (n_beams > 32) could
not fire on broadband RFI lit across the whole sky. The metric is Euclidean
rather than cityblock because the sky pair is two coordinates of one physical
quantity: under L1 a cross-beam link costs `|dx| + |dy|`, up to sqrt(2) times the
real separation and dependent on how the pair lies against the projection axes.

The link scale on each axis is the beam's own FWHM, so two trials are linked
within one beam width of each other. The beam is an alt/az-aligned ellipse from
`bf_weights_generator.compute_beam_fwhm`, about 18 deg E-W by 4 deg N-S,
pointing independent. Being far wider E-W, one source lights a row of beams
rather than a circle, and an isotropic link would either split that row or merge
unrelated sky.

The pointing table and the ellipse come from the weights live at the gulp's own
time (`weights_registry.pointings_for`), fetched once per gulp and cached by
weights id. When the registry cannot name a single product for the time (no
event, a partial deploy, an unregistered payload), clustering falls back to the
beam-index axis and `beam_scale`, warns once, and leaves `sky_extent_deg` at 0 so
nothing is tagged on an unmeasured number.

Sky extent, not beam count, is the primary RFI discriminator: a real source
spans about one E-W beam width plus a beam spacing, so anything wider than
`filters.max_sky_extent_deg` is not one source.

## Decision chain

Per cluster, in order (apps/t2d.py):

injection match: within the matching windows of a ledger row. Tagged and
stored, never dumped, the dump ring tapping the data upstream of the injection
merge so a dump cannot contain the injected pulse.

beam veto, then wide-beam RFI: configured noisy beams, then anything spanning
more than `max_nbeam` distinct beams or more than `max_sky_extent_deg` on the
sky. Both tag `rfi_wide`. The sky test catches a burst in a dozen beams
scattered across the sky, which the count alone cannot.

known-source match: beam inside a scheduled transit window and the cluster's DM
range overlapping the source DM. Range, not peak: a storm produces clusters
whose peak DM lands anywhere, and matching on peak puts a pulsar tag on RFI.

tier: S/N bands A >= 30, B >= 18, C >= 12. Blind triggers need tier A or B and
peak DM above the floor (20 pc/cc); known sources carry their own S/N minimum
and may sit below tier C. Tier C is stored and never triggers.

budgets: per dump kind, a minimum spacing plus a hard daily cap, one dump per
gulp, a storm lockout, and a free-disk floor checked on the node that would
receive the data. Every refusal is recorded with its reason.

Survivors get a name, `YYMMDD` plus 6 random lowercase letters (legacy 10-char
names persist in the DB and in artifact paths), an intensity dump on the owning
node, and a trigger card in the T3 spool. Automatic voltage dumps are wired
(tier A only) but disabled, and `dumps_enabled: false` suppresses intensity
dumps as well.

`trigger.fast_path` picks between two trigger styles. Strict cluster-first
(`false`) waits for DBSCAN and the full chain before any dump; ring-window
misses are then recorded as refusals, which is the data that sizes the ring
buffer. The hybrid fast path (`true`) fires on bright single candidates at once
and reconciles with the cluster afterwards, for when the latency budget is
tighter than the ring.

## Voltage dumps

A separate path with its own daemons. Raw voltages are tapped on the antenna
side, before beamforming: six antenna streams, three per node, each with a
casm_cand_dump daemon on port 27000 + stream (0-2 on corr1, 3-5 on corr2). Every
antenna sees the whole sky, so a voltage dump goes to all six endpoints. The
streams split the band top-down in 15.625 MHz slices: stream 0 is
468.75-484.375 MHz, stream 5 is 390.625-406.25, and the 440-465 MHz live band
sits in streams 1 and 2, both on corr1.

t2d can command these on tier A but ships with `trigger.voltage` disabled.
`casm-voltage-dump` drives them by hand, needs nothing from T2, and runs with
t2d stopped. At 2.0625 GB/s per stream the binding constraint is disk rather
than the ring, so that CLI does the window and disk arithmetic before sending;
see `docs/operations.md`.

## Database

One SQLite file, WAL mode (db.py):

| table | holds |
|---|---|
| clusters | every stored event: name, tier, tags, peak, envelope, sky |
| triggers | the dump audit: action, detail, bytes, cleanup state |
| injections | the ledger, with per-gate recovery columns |
| gulp_stats | per-gulp funnel counters |
| labels, frbs | human classifications and the promoted catalog |

Trigger actions are `triggered`, `refused` (policy), `refused_daemon` (the dump
daemon said no, usually the ring window), `failed`, `shadow`, and
`suppressed_commissioning` (`dumps_enabled: false`).

`gulp_stats.n_cands` is the raw count in; `n_vetoed` and `n_shed` are what the
width veto and the storm cap dropped before clustering, so DBSCAN saw
`n_cands - n_vetoed - n_shed`. `gulp_stats.skipped` is 1 for a gulp dropped
because `coalesce_max_s` expired with jobs missing; such a row carries the
counts that arrived with `n_clusters` 0.

`clusters.sky_extent_deg` is the largest pairwise separation of a cluster's
member beams in degrees: 0 for a single-beam cluster, NULL for legacy rows.
Trigger cards carry the same number alongside `n_beams`.

Timestamps are ISO-8601 UTC with a `T` separator. sqlite's `datetime('now')`
renders with a space, which string-compares wrongly against them, so build
cutoffs in Python, never in SQL.

## Injections

The injection daemon synthesises a pulse with parameters drawn from configured
ranges, converts it to DADA, and writes it into a beamformer injection FIFO. The
ledger row goes in before the FIFO write, so a crash cannot leave an unaccounted
pulse in the data. Reconciliation a few minutes later fills the gate columns
(seen at T1, clustered at T2, trigger-eligible), and the first failed gate is
the failure reason.

Alongside the gates the ledger records what the amplitude solver was
working from and what the matched cluster looked like:

| column | meaning |
| --- | --- |
| inject_snr | injected (true, analytic) S/N the amplitude was solved for |
| target_snr | the reported S/N that injected value is predicted to give |
| sigma_n | live per-channel std of the beam the solver read from Redis |
| nchan_usable | channels the solver assumed were unmasked |
| rec_width | ibox of the matched cluster: log2 of the boxcar in samples |
| rec_beam | peak beam of the matched cluster |
| rec_samp | peak sample of the matched cluster |
| rec_lead_s | cluster event time minus inject_utc, seconds |
| rec_offset_arcsec | sky separation of the injected and recovered beams; shown only when they differ |
| n_t1_trials | raw T1 trials matching the shot, counted only when no cluster did (NULL = not looked at, or the file was unreadable) |
| sub_incoh | incoherent-beam subtraction on (1) / off (0) at fire time, from the beamformer log; NULL unknown |
| dump_dir, dump_utc_start, dump_utc_stop | the intensity dump taken for the replay |
| replay_png, replay_posted | the rendered replay plot, and whether it reached Slack |
| slack_ts | ts of the Slack message for this shot, when posting is on |
| outcome | closed enum, below |

`rec_lead_s` is normally negative, by 10-20 s: the sidecar joins a gulp whose
samples are already seconds old, so the pulse is in the search stream before the
FIFO write that scheduled it. A positive lead means the match is probably not
the injection.

`fail_reason` stays free text naming the first failed gate.  `outcome`
(`casm_t2.inject_outcome`) is the closed set that anything counting over
injections uses, so a new failure string cannot invent a category:

| outcome | when |
| --- | --- |
| recovered | a matching cluster, at ANY S/N |
| missed_t2 | no cluster, but matching raw T1 trials |
| missed_t1 | no cluster and no matching trial |
| fire_failed | the shot never reached the stream (file or FIFO failure) |

An outcome records only whether the search saw the pulse. The trigger gates are
not part of it: whether a recovered injection would also have earned a dump is
policy (tiers, DM floor, beam vetoes, occupancy) and policy changes, so a shot
recorded as a miss because the tier floor moved would make the record
unreadable over time. `gate_trigger` still holds the counterfactual.

The injected beam means that beam and its neighbours on the sky, never a
beam-index window. Beam numbers are not sky-ordered: consecutive indices are a
median 16 degrees apart, and a `+-2` index window can contain no true sky
neighbour at all while the beams within one beam ellipse are scattered across
the index range. `cluster.neighbour_beams(sky, beam, fwhm_x, fwhm_y, scale)`
returns the beam plus every beam satisfying
`(dx/fwhm_x)^2 + (dy/fwhm_y)^2 <= scale^2` on the tangent-plane axes, the same
ellipse the clustering uses, with the FWHMs from the registry product and the
config as fallback, cached per weights product and beam. It serves the cluster
match and the raw-trial scan in `reconcile()` and t2d's `injection` tagging on
both the slow and fast paths, so a shot cannot be tagged on one path and missed
on the other. With no pointing table the callers fall back to the index window
and log that it is not a sky match.

A `missed_t2` is worth a second look: T2 clusters every surviving trial, DBSCAN
noise points becoming singleton clusters, and stores everything at S/N >= 12, so
trials arriving with nothing clustered means the gulp or the trials were dropped
before clustering ran. `t2_miss_reason()` asks `gulp_stats`, one row per
coalesced gulp, and names the cause: skipped incomplete, dropped by the storm
cap, partly shed, or width-vetoed. If none fits, the reason says "intact but no
cluster (unexpected, investigate)" and logs a WARNING.

The gulp index is exactly `samp // 8192`.

Telling `missed_t1` from `missed_t2` needs the raw trials, which T2 never
stores. `reconcile()` therefore reads hella's own candidate file for the
observation and stream,
`/mnt/nvme4/data/casm/hella_cands/cands_<UTC_START>.dat.<stream>`
(`snr samp time_days width dm_idx dm beam`, samp absolute from UTC_START at
1.048576 ms). `obs_utc_start_at()` finds UTC_START from the most recent
cluster at or before the injection. Trials match on the same sky beam set, DM tolerance and time window as the
cluster match, and the count goes into `n_t1_trials`.

Injections only go to streams 0-3, which are corr1-local, so the file is always
readable in principle. When it is not there, `n_t1_trials` stays NULL, the
reason carries `(T1 file unavailable ...)`, and the shot falls back to
`missed_t1`. NULL means not asked, never that hella saw nothing.

`reconcile()` writes a full sentence into `fail_reason` and the Slack line
prints it verbatim:

    lost at T2: 7 matching T1 trials (best S/N 9.2) but no cluster formed (min 5 members)
    lost at T1: no matching trial in beam 200 (+-2) within the window at DM 500 (+-75)
    lost at T1: no cluster in the window in beam 200 (+-2) at DM 500 (+-75) (T1 file unavailable (cands_....dat.3 not found))

### Shot parameters

Every drawn parameter is one entry in `injection.sample`:

```yaml
sample:
  dm:         {dist: uniform,    lo: 100.0, hi: 900.0}
  fwhm_ms:    {dist: loguniform, lo: 2.5,   hi: 30.0}
  inject_snr: {dist: loguniform, lo: 12.0,  hi: 18.0}
```

`dist` is `uniform`, `loguniform`, `choice` (with `values` and optional
`weights`) or `fixed` (with `value`). `casm_t2.inject_calib.draw` validates the
spec and raises on anything malformed, so a bad spec stops the daemon rather
than injecting at the wrong brightness. The legacy `dm_range` / `fwhm_ms_range`
/ `inject_snr_range` keys still work, with a warning. The manual flags `--dm`,
`--fwhm-ms` and `--inject-snr` override any of it.

A calibration run is the same block expressed as a grid:

```yaml
sample:
  fwhm_ms:    {dist: choice, values: [3.0, 8.0, 20.0]}
  inject_snr: {dist: fixed,  value: 15.0}
  dm:         {dist: fixed,  value: 300.0}
```

Two clamps are applied after the draw, both logged: FWHM at
`MIN_RENDERABLE_FWHM_MS` (2.469 ms, the generator's own floor) and DM to hella's
searched grid [0, 1000].

`injection.summary_dm_bins` gives the edges of the daily summary's per-DM tally
and of the recovery figure's colour/marker bins. It defaults to
`[100, 300, 500, 700, 900]`, matching the sampled DM range, with an under- and
an over-flow bin so a DM outside the range still lands somewhere.

### Width

Widths are FWHM everywhere a human reads them. The ledger column is still
`sigma_ms` (Gaussian sigma, FWHM/2.355) so old rows and queries keep working,
and the generator takes a sigma.

FWHM is sampled log-uniformly because the search trials are spaced in powers of
two: a uniform draw puts two thirds of the shots on the two widest trials.

Recovered widths are not `2**ibox` samples. hella smooths with a Gaussian-like
kernel (`smooth.cpp`) whose FWHM is about 0.67 of the trial label, 11.5 ms at
ibox 4 rather than 16.8 ms, so `casm_t2.hella_kernel.kernel_fwhm_ms` computes it
from the same polynomial:

| ibox | trial | kernel FWHM | equivalent boxcar |
| --- | --- | --- | --- |
| 0 | 1 samp | 1.0 ms | 1.1 ms |
| 1 | 2 samp | 1.0 ms | 1.8 ms |
| 2 | 4 samp | 3.1 ms | 3.9 ms |
| 3 | 8 samp | 5.2 ms | 7.9 ms |
| 4 | 16 samp | 11.5 ms | 15.7 ms |
| 5 | 32 samp | 22.0 ms | 31.4 ms |
| 6 | 64 samp | 45.1 ms | 62.7 ms |

ibox 6 is dropped by t2d's width veto, so 30 ms is the sampling ceiling, still
nearest ibox 5.

### Amplitude

The sampled quantity is the injected S/N, the true analytic matched-filter S/N
of the pulse being written, independent of how hella reports it. The amplitude
solver takes it directly, with no calibration table in the loop:

    amp = inject_snr * sigma_n / (sqrt(nchan_usable) * sqrt(sigma_t * sqrt(pi)))

Injected S/N is sampled log-uniformly. The formula is the analytical Gaussian
matched filter, agreeing with the offline injector's numerical matched filter
(`casm_offline_frb_injector.SNRCalibrator`) to 1-3% above FWHM 4.7 ms and
running about 10% high at 2.5 ms.

The table is needed only to predict what hella will report:

    predicted_reported = inject_snr * rec_per_true(FWHM)

`rec_per_true` is hella's reported S/N divided by the analytical value, a
function of width rather than a constant: hella's matched trial is the smoothing
kernel above, over unit-variance rows, so the ratio falls as the pulse widens
(2.24 at FWHM 4.7 ms, 2.08 at 11.8, 1.22 at 23.5, with T1 subtraction off).
`rec_per_true_table` holds those pairs and the daemon interpolates linearly in
log FWHM, holding the end values flat.

### The live std, and what makes it stale

The amplitude is solved from the beam's per-channel std read live from Redis
(`bf_proc_stat`). A beamformer restart makes that value untrustworthy: Redis
keeps whatever was last published and it can sit unchanged for minutes. A shot
fired on a stale std comes out a factor low.

Redis carries no timestamp for these keys (`ts_hi`/`ts_lo` are PNG timeseries
plots, not times) and the `age_s` from `query_live_noise_std` is the local disk
cache age, always 0.0 on the `force_refresh` path. Freshness is decided from the
beamformer log instead: no `START casm_bfcorr` within `max_std_age_s` and the
value is trusted at once, one Redis read; after a recent restart the daemon
polls every `std_poll_s` until the value changes, which is proof the publisher is
back, giving up after `std_wait_s` and recording the shot as `fire_failed` with
`live std stale (age N s)`.

The same log gives `sub_incoh`, the incoherent-beam subtraction state at fire
time, from the flag on the most recent start. The log interleaves the antenna
nodes, so most recent means the largest timestamp, not the last line. The
reported/true S/N ratio differs between the two states, so a Slack summary over
a day that mixes them splits its ratio line by state.

### The saturation cap

Only the *reported* S/N saturates hella, so the prediction is what gets
capped. When `predicted_reported` exceeds `reported_snr_cap(FWHM)`, the
injected S/N is scaled down to land exactly on the cap and the clamp is
logged.

Above the ceiling the injected pulse fills hella's 10000-peak per-gulp
candidate buffer and the gulp stops being searched across the whole beam set:
reported 78 left 31/64 beams searched and reported 132 left 43/64, while
reported 43 at FWHM 4.7 ms and 35.5 at 11.8 ms left the gulp intact. A
candidate's footprint grows with width, so a wide pulse saturates at a lower
reported S/N:

| FWHM | cap |
| --- | --- |
| below 6 ms | 50 |
| 6 to 15 ms | 40 |
| above 15 ms | 40 * sqrt(12 / FWHM), so 25 at 30 ms |

The wide branch is extrapolated from two points rather than measured, and errs
low. A config carrying only the flat `target_rec_snr_max` is honoured as a
width-independent cap.

The sampled injected S/N range is chosen so the cap does not bite inside it: at
18, the top of the range, the predicted reported S/N stays under the cap at
every width from 2.5 to 30 ms. The tightest point is FWHM 6.0 ms, where the cap
steps from 50 to 40 and the prediction is 39.6, about 1% of headroom. The clamp
is a safety net for manual shots and for a change to either number. A test
sweeps the width range and fails if that stops being true.
