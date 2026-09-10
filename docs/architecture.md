# casm_t2 architecture

## Data path

Eight hella jobs — one per 64-beam stream, jobs 0-3 on the first backend
node and 4-7 on the second — connect per gulp and send their candidate
list. t2d coalesces the batches, deduplicates and clusters, classifies,
fires whatever dumps survive the policy, and writes the whole decision
chain to SQLite.

## Coalescing

A gulp is only a gulp once all eight jobs have reported. They finish
seconds apart, so the coalescer holds a (utc_start, gulp) key open until
every expected job has arrived and `coalesce_s` of quiet has passed. The
wait is recorded in `gulp_stats.coalesce_wait_ms` next to `n_jobs`.

If `coalesce_max_s` elapses first with jobs still missing, the gulp is
**dropped whole**: not clustered, not triggered, recorded with `skipped: 1`
and the counts that did arrive. Half a sky is not worth a dump decision —
every per-gulp veto reasons about the whole sky, and the fragment holding
the bright beam is generally not the one holding the evidence against it.
The row still goes in so the gap stays visible in the duty cycle, and the
heartbeat counts skipped gulps; a rising count means a hella job is dying.
Completeness is read when the wait ends, not from which deadline fired, so
a gulp whose batches merely trickle in is complete, not skipped.

Before 2026-09-09 the key flushed a fixed `coalesce_s` after the *first*
batch, which split gulps into fragments — gulp 1218 of obs
2026-09-09-21:12:15 arrived 3+2+1+1+1 — and every per-gulp veto (the
occupancy footprint, the dm_floor coincidence spread, one-dump-per-gulp)
saw only its own fragment. That is how 260910fkmpyt dumped while the
DM-floor fragment of the same impulse sat in a different fragment.

A batch that turns up after its key has flushed still gets processed, as
its own fragment, but it is logged as a late batch and counted in the
heartbeat: a job running that late means the vetoes did not see the whole
sky. `coalesce_max_s` ships at 8.0 s, one gulp length. A job that has not
reported within a whole gulp of the first one is stuck, not slow. The
budget is not elastic either: the observed successful event-to-request lag
is 36.5-50.8 s (207 dumps since 2026-09-01, mean 43.7; the one failure was
at 206 s), retention behind the ring's read pointer beyond that 51 s is
unmeasured, and hella's own 13-25 s reporting delay already spends most of
it.

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

DBSCAN, Euclidean metric, over scaled (samp, dm_idx, log2(width), x, y)
where (x, y) is the beam's position in degrees on a tangent plane about
the zenith, x East-West and y North-South, scaled by `beam_fwhm_x_deg`
and `beam_fwhm_y_deg`. An offset of exactly one scale on
any single axis is a distance of exactly 1, so eps 1.0 keeps its meaning;
offsets on several axes now add in quadrature rather than linearly.

Each cluster keeps its peak trial and the membership envelope: time, DM and beam ranges, member count,
distinct-beam count, and `sky_extent_deg` — the largest pairwise
great-circle separation of its member beams. Noise points survive as
singleton clusters rather than being dropped — a lone bright pulse in one
beam is exactly what an FRB looks like.

The sky axis replaced a raw `beam / beam_scale` axis on 2026-09-09. The
deployed 512-beam grid is not sky-ordered: consecutive beam indices are a
median 16 deg apart while true sky neighbours are 3.1 deg, and only 11% of
a beam's six sky-nearest neighbours lie within +-4 index. Clustering on the
index therefore split point sources that spanned two adjacent sky beams,
understated every footprint in `n_beams`, and left `rfi_wide` (n_beams >
32) unable to fire on broadband RFI that was lit across the whole sky.

The metric was cityblock until 2026-09-09. The sky pair is two
coordinates of one physical quantity, so under L1 a cross-beam link cost
`|dx| + |dy|` — up to sqrt(2) times the real separation, and dependent on
how the pair happened to lie against the projection axes. Under Euclidean
the sky term is the tangent-plane separation itself.

The link scale on each axis is the beam's own FWHM on that axis, so two
trials are linked when they fall within one beam width of each other. The
beam is taken as a standard ellipse aligned with alt/az, from
`bf_weights_generator.compute_beam_fwhm`: 18.1 deg E-W by 3.9 deg N-S,
pointing independent. Because it is far wider E-W than N-S, one source
lights up a row of beams rather than a circle of them, and an isotropic
link either splits that row or merges unrelated sky. Change the two config
values when the weights change the beam.

The pointing table comes from the weights live at the gulp's own time
(`weights_registry.pointings_for`), fetched once per gulp and cached by
weights id. When the registry cannot name a single product for the time —
no event, a partial deploy, an unregistered payload — clustering falls back
to the beam-index axis and `beam_scale`, warns once, and leaves
`sky_extent_deg` at 0 so nothing is ever tagged on a number that was not
measured. Fail-safe, never fail-shut.

Sky extent, not beam count, is now the primary RFI discriminator: a real
source spans about one E-W beam width plus a beam spacing, so anything
wider than `filters.max_sky_extent_deg` (25 deg) is not one source.

## Decision chain

Per cluster, in order (apps/t2d.py):

injection match — within the matching windows of a ledger row. Tagged and
stored, never dumped: the dump ring taps the data upstream of the
injection merge, so a dump physically cannot contain the injected pulse.

beam veto, then wide-beam RFI — configured noisy beams, then anything
spanning more than `max_nbeam` distinct beams **or** more than
`max_sky_extent_deg` on the sky. Both tag `rfi_wide`; the sky test is the
one that catches a burst in a dozen beams scattered right across the sky,
which the count alone never could.

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
| clusters | every stored event: name, tier, tags, peak, envelope, sky |
| triggers | the dump audit: action, detail, bytes, cleanup state |
| injections | the ledger, with per-gate recovery columns |
| gulp_stats | per-gulp funnel counters |
| labels, frbs | human classifications and the promoted catalog |

Trigger actions are `triggered`, `refused` (policy), `refused_daemon`
(the dump daemon said no — usually the ring window), `failed`, `shadow`,
and `suppressed_commissioning` (`dumps_enabled: false`).

`gulp_stats.n_cands` is the raw count in; `n_vetoed` and `n_shed` are what
the width veto and the storm cap dropped before clustering, so DBSCAN saw
`n_cands - n_vetoed - n_shed`. `gulp_stats.skipped` is 1 for a gulp dropped because
`coalesce_max_s` expired with jobs missing; such a row carries the counts
that arrived but `n_clusters` 0, and nothing downstream ran for it.

`clusters.sky_extent_deg` is the largest pairwise separation of a
cluster's member beams in degrees; 0 for a single-beam cluster, and NULL
for rows written before 2026-09-09. Trigger cards carry the same number as
`sky_extent_deg` alongside `n_beams`.

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
| slack_ts | ts of the Slack message for this shot, when posting is on |
| outcome | closed enum, below |

`rec_lead_s` is normally **negative**, by 10-20 s: the sidecar joins a
gulp whose samples are already seconds old, so the pulse is in the search
stream before the FIFO write that scheduled it. A positive lead means the
match is probably not the injection.

`fail_reason` stays free text naming the first failed gate.  `outcome`
(`casm_t2.inject_outcome`) is the closed set that anything counting over
injections uses, so a new failure string cannot invent a category:

| outcome | when |
| --- | --- |
| recovered | a matching cluster, at ANY S/N |
| missed_t2 | no cluster, but matching raw T1 trials |
| missed_t1 | no cluster and no matching trial |
| fire_failed | the shot never reached the stream (file or FIFO failure) |

An outcome answers one question: did the search see the pulse? The trigger
gates are deliberately not part of it. Whether a recovered injection would
also have earned a dump is policy - tiers, DM floor, beam vetoes, occupancy
- and policy changes week to week; a shot recorded as a miss because the
tier floor moved would make the injection record unreadable over time.
`gate_trigger` still records the counterfactual for anyone who wants it, so
a shot found at S/N 15.8 is recovered and reads like any other.

"The injected beam" means the injected beam **and its neighbours on the
sky**, never a beam-index window. Beam numbers are not sky-ordered: on the
deployed grid consecutive indices are a median 16 degrees apart, and for
beam 150 on the 2026-09-09 pointing table the old `+-2` index window
contained no true sky neighbour at all - only the beam itself - while the
twelve beams actually within one beam ellipse are scattered from 43 to 322.
`cluster.neighbour_beams(sky, beam, fwhm_x, fwhm_y, scale)` returns the beam
plus every beam satisfying `(dx/fwhm_x)^2 + (dy/fwhm_y)^2 <= scale^2` on the
tangent-plane axes - the same ellipse the clustering uses, with the FWHMs
from the registry product and the config as fallback - cached per weights
product and beam. It is used for the cluster match and the raw-trial scan in
`reconcile()`, and for t2d's `injection` tagging on both the slow and fast
paths, so a shot can never be tagged on one path and missed on the other.
With no pointing table the callers fall back to the index window and say so
in the log, because that is a guess about hardware ordering rather than a
statement about the sky.

Telling `missed_t1` from `missed_t2` needs the raw trials, which T2 never
stores. `reconcile()` therefore reads hella's own candidate file for the
observation and stream,
`/mnt/nvme4/data/casm/hella_cands/cands_<UTC_START>.dat.<stream>`
(`snr samp time_days width dm_idx dm beam`, samp absolute from UTC_START at
1.048576 ms). `obs_utc_start_at()` finds UTC_START from the most recent
cluster at or before the injection. Trials match on the same beam window
(+-2), the same DM tolerance and the same time window as the cluster match,
and the count goes into `n_t1_trials`.

Injections only ever go to streams 0-3, which are corr1-local, so the file
is always readable in principle. When it is not there, that is recorded as
such - `n_t1_trials` stays NULL and the reason carries
`(T1 file unavailable ...)` - and the shot falls back to `missed_t1`. NULL
means "not asked", never "hella saw nothing".

`reconcile()` writes a full sentence into `fail_reason` and the Slack line
prints it verbatim:

    lost at T2: 7 matching T1 trials (best S/N 9.2) but no cluster formed (min 5 members)
    lost at T1: no matching trial in beam 200 (+-2) within the window at DM 500 (+-75)
    lost at T1: no cluster in the window in beam 200 (+-2) at DM 500 (+-75) (T1 file unavailable (cands_....dat.3 not found))

### Width

Widths are FWHM everywhere a human reads them. The ledger column is still
`sigma_ms` (Gaussian sigma, FWHM/2.355) so old rows and old queries keep
working, and the generator takes a sigma, but nothing else does.

`fwhm_ms_range` is sampled log-uniformly, because the search trials are
spaced in powers of two: a uniform draw would pile two thirds of the shots
onto the two widest trials.

Recovered widths are **not** `2**ibox` samples. hella smooths with a
Gaussian-ish kernel (`smooth.cpp` lines 47-55), and that kernel's FWHM is
about 0.67 of the trial label — 11.5 ms at ibox 4, not 16.8 ms. Quoting the
label overstates the pulse by half, so `casm_t2.hella_kernel.kernel_fwhm_ms`
computes the real thing from the same polynomial:

| ibox | trial | kernel FWHM | equivalent boxcar |
| --- | --- | --- | --- |
| 0 | 1 samp | 1.0 ms | 1.1 ms |
| 1 | 2 samp | 1.0 ms | 1.8 ms |
| 2 | 4 samp | 3.1 ms | 3.9 ms |
| 3 | 8 samp | 5.2 ms | 7.9 ms |
| 4 | 16 samp | 11.5 ms | 15.7 ms |
| 5 | 32 samp | 22.0 ms | 31.4 ms |
| 6 | 64 samp | 45.1 ms | 62.7 ms |

ibox 6 is dropped by t2d's width veto, so 30 ms is the sampling ceiling: it
still sits nearest ibox 5.

### Amplitude

The sampled quantity is the **injected** S/N: the true, analytic
matched-filter S/N of the pulse being written. That is what the pulse is,
independent of how hella chooses to report it, so it is what the amplitude
solver takes directly, with no calibration table in the loop:

    amp = inject_snr * sigma_n / (sqrt(nchan_usable) * sqrt(sigma_t * sqrt(pi)))

`inject_snr_range` is sampled log-uniformly. The formula is the analytical
Gaussian matched filter; it agrees with the offline injector's numerically
computed matched filter (`casm_offline_frb_injector.SNRCalibrator`) to 1-3%
above FWHM 4.7 ms, and runs about 10% high at 2.5 ms.

The table is still needed, but only to *predict* what hella will report:

    predicted_reported = inject_snr * rec_per_true(FWHM)

`rec_per_true` is hella's reported S/N divided by the analytical value, and
it is a function of width, not a constant: hella's matched trial is the
smoothing kernel above, over rows normalised to unit variance, so the ratio
falls as the pulse widens (2.24 at FWHM 4.7 ms, 2.08 at 11.8, 1.22 at 23.5,
measured 2026-09-09 with T1 subtraction off). `rec_per_true_table` holds
those pairs and the daemon interpolates linearly in log FWHM, holding the
end values flat.

### The saturation cap

Only the *reported* S/N saturates hella, so the prediction is what gets
capped. When `predicted_reported` exceeds `reported_snr_cap(FWHM)`, the
injected S/N is scaled down to land exactly on the cap and the clamp is
logged.

Above the ceiling the injected pulse fills hella's 10000-peak per-gulp
candidate buffer and the gulp stops being searched across the whole beam
set (2026-09-09: reported 78 left 31/64 beams searched, reported 132 left
43/64; reported 43 at FWHM 4.7 ms and 35.5 at 11.8 ms left the gulp
intact). A candidate's footprint grows with width, so a wide pulse
saturates at a lower reported S/N:

| FWHM | cap |
| --- | --- |
| below 6 ms | 50 |
| 6 to 15 ms | 40 |
| above 15 ms | 40 * sqrt(12 / FWHM), so 25 at 30 ms |

The wide branch is an extrapolation from two clean points, not a measured
curve; it errs low on purpose. A config carrying only the old flat
`target_rec_snr_max` is still honoured as a width-independent cap.

`inject_snr_range` is chosen so the cap never bites inside it: at 18, the
top of the range, the predicted reported S/N stays under the cap at every
width from 2.5 to 30 ms. The tightest point is FWHM 6.0 ms, where the cap
steps from 50 to 40 and the prediction is 39.6 - about 1% of headroom. So
the clamp is a safety net for manual shots and for a future change to
either number, not something scheduled shots run into. A test sweeps the
whole width range and fails if that stops being true.
