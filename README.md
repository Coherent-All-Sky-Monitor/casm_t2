# casm_t2

T2 stage of the CASM fast-transient search. Takes the single-pulse candidate
stream from the casm-hella GPU search (T1), clusters it, cuts the RFI, and
decides which events are worth dumping to disk, inside the few seconds the
upstream ring buffer allows. casm_t3 turns those dumps into plots and a
monitoring UI.

Hella emits thousands of candidates a second in bad RFI weather, nearly all of
it junk. T2 gets from there to a few dump decisions per hour, with every
decision recorded so misses can be audited alongside hits.

## How it works

`t2d` owns eight TCP ports, one per hella job. Each gulp it coalesces the
per-job batches, deduplicates, and clusters with DBSCAN over (time, DM, width,
sky position). The sky span of a cluster's beams is the main RFI discriminator,
a real pulse being compact on the sky. Beams enter as degrees on a tangent plane
about the zenith, from the weights live at that moment: the beam index is not
sky-ordered, consecutive indices being a median 16 deg apart against 3.1 deg for
true sky neighbours. The two sky axes carry the beam's own FWHM, which is far
wider East-West than North-South, so one source lights a row of beams rather
than a circle. That ellipse follows the deployed weights: the uploader records
it before live events; `t3-weights-watch` can fill older missing metadata using
the same generator helper. It uses the nonzero CB slot union over all channels,
polarizations and beams, at the saved mean frequency (the lambda/D model, not an
exact per-beam response). Registry readers reload changed product JSON; t2d also
refreshes cached clustering widths when they change. A consistent association
requires all six streams, not just whichever streams have reported so far. The
`cluster.beam_fwhm_x_deg` / `beam_fwhm_y_deg` in `config/t2d.yaml` are the
fallback for a product with no ellipse.

Clusters then run a filter chain: injection match (stored, never dumped), beam
veto, wide-beam RFI cut (too many beams, or too wide on the sky), and S/N
tiers (A >= 30, B >= 14, C >= 12; triggers need A/B plus DM >= 20). Survivors hit the trigger budgets, minimum spacing, daily
caps, one dump per gulp, a storm lockout and a free-disk floor, before a dump
command goes to the owning backend node.

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
daily recovery report), and `t2-inject-slack-preview`.

Injected widths are FWHM, drawn log-uniformly so hella's power-of-two boxcar
trials are exercised evenly, and the amplitude is solved per shot from a
log-uniform injected (true) S/N and the live beam noise. The reported S/N is
predicted from a per-width table and capped, so a bright injection cannot fill
the candidate buffer and blind the search for that gulp. Recovered widths are
quoted as the FWHM of hella's kernel (`casm_t2.hella_kernel`), about two thirds
of the `2**ibox` trial label.

The injection daemon posts one Slack message per shot: the sent line when the
pulse goes in, completed in place a couple of minutes later with a coloured bar
carrying the outcome and the replay plot; the same beam before hella flagging
follows as a reply in that thread. `t2-inject-slack-preview` renders the
same messages and figures to a directory with no token and no network. Columns,
the outcome enum and the flags are in `docs/architecture.md` and
`docs/operations.md`.

`casm-voltage-dump` commands the antenna-side voltage daemons by hand. It talks
to them directly, so it works with t2d stopped, and it is the only way to get
raw voltages while automatic voltage triggering is disabled:

    casm-voltage-dump --next 2     # 2 s of all six streams, from 5 s hence

See `docs/architecture.md` for the data path and database schema, and
`docs/operations.md` for deployment, config reference, and runbooks.

GPL-3.0 license.
