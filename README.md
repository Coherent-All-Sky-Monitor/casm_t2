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
width, sky position) — how far apart on the sky a cluster's beams are is
the main RFI discriminator, since a real pulse is compact on the sky and
RFI is not. Beams enter as degrees on a tangent plane about the zenith,
taken from the weights live at that moment, because the beam *index* is
not sky-ordered: consecutive indices are a median 16 deg apart while true
sky neighbours are 3.1 deg. The two sky axes carry the beam's own FWHM —
one source lights up a row of beams, not a circle of them, because the beam
is far wider East-West than North-South. That ellipse follows the deployed
weights: it is computed from the antennas actually beamformed and stored on
the weights-registry product (by `t3-weights-watch`), and t2d takes it from
there per weights id, logging which product it came from. The
`cluster.beam_fwhm_x_deg` / `beam_fwhm_y_deg` in `config/t2d.yaml` are only
the fallback, used when the live product carries no ellipse. Clusters then run a filter chain: injection
match (stored, never dumped), beam veto, wide-beam RFI cut (too many
beams, or too wide on the sky), known-source DM-range match, and S/N
tiers (A >= 30, B >= 15, C >= 12; blind triggers need A/B plus DM >= 20).

Survivors hit the trigger budgets — minimum spacing, daily caps, one dump
per gulp in a storm, and a free-disk floor — before a dump command goes to
the owning backend node.

A gulp is not clustered until all eight jobs have reported it or the
coalescer's maximum wait runs out, so the per-gulp vetoes see the whole
sky rather than whichever jobs happened to be quick.

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
daily recovery report), `t2-inject-slack-preview`, and
`t2-transit-schedule`.

Injected widths are FWHM, drawn log-uniformly so hella's power-of-two
boxcar trials get exercised evenly, and the amplitude is solved per shot
from a log-uniform *injected* (true) S/N and the live beam noise. What
hella will report is predicted from a per-width table and capped, so a
bright injection can never fill the candidate buffer and blind the search
for that gulp. Recovered widths are quoted as the FWHM of hella's kernel
(`casm_t2.hella_kernel`), which is about two thirds of the `2**ibox` trial
label.

The injection daemon can post one Slack message per shot: the sent line when
the pulse goes in, completed in place a couple of minutes later with a
coloured bar carrying the outcome and the replay plot. It ships disabled
(`injection.slack.enabled: false`);
`t2-inject-slack-preview` renders the
same messages and figures to a directory with no token and no network, so
they can be reviewed before it is turned on. Columns, the outcome enum and
the flags are in `docs/architecture.md` and `docs/operations.md`.

`casm-voltage-dump` commands the antenna-side voltage daemons by hand. It
talks to them directly, so it works with t2d stopped, and it is the only
way to get raw voltages while automatic voltage triggering ships disabled:

    casm-voltage-dump --next 2     # 2 s of all six streams, from 5 s hence

See `docs/architecture.md` for the data path and database schema, and
`docs/operations.md` for deployment, config reference, and runbooks.

GPL-3.0 license.
