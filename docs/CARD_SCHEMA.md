# Trigger card: the `gulp` block

Agreed 2026-09-18 for the T2 v2 backtest (originally
`/mnt/nvme3/vishnu/t2_backtest/CARD_SCHEMA.md`); written by the live t2d since
2026-09-22 (`casm_t2/gulp_card.py`). casm_t2 writes it, casm_t3's candidate
figure reads it for its bottom row. The other card keys (candname, event_utc,
beam, snr, dm, width, samp, sky, pointings, dump paths) are unchanged. The old
`context` block stays for older readers; T3 ignores it when `gulp` is present.
Cards without a `gulp` block are pre-v2 and T3 draws the old bottom row.

t2d builds the block from its own clustering of the coalesced gulp window: the
same clusters, in the same order, with the same decisions it recorded in the
DB. It is reporting only and changes no trigger decision. Fast-path cards
(`trigger.fast_path: true`) carry no block.

```
"gulp": {
  "gulp": 28467,                      # hella preamble gulp number, = gulp_stats.gulp / clusters.gulp
  "utc_start": "2026-09-19-05:01:25", # DADA UTC_START of the observation
  "samp_lo": 233200704, "samp_hi": 233208895,  # hella samples, inclusive
  "tsamp_s": 0.001048576,
  "weights_id": "7f00805b81f13763",   # weights product live for the gulp, null without one
  "n_raw": 1041,            # candidates that arrived from all 8 jobs
  "n_width_dropped": 340,   # dropped at parse by veto_widths (the widest boxcar, index 6)
  "n_shed": 0,              # dropped by the storm cap before DBSCAN
  "n_trials_total": 701,    # trials DBSCAN saw = n_raw - n_width_dropped - n_shed
  "trials_truncated": false,
  "n_clusters_total": 14,
  "clusters_truncated": false,
  "trials": [               # every trial DBSCAN saw, time ordered, up to the cap
    [dt_s, beam, dm, snr, width_idx, cluster_id], ...
  ],
  "clusters": [
    {"id": 0,
     "peak": [dt_s, beam, dm, snr, width_idx],
     "n_trials": 40,
     "beams": [[beam, max_snr], ...],   # one entry per distinct member beam
     "sky_extent_deg": 0.0,             # largest pairwise separation of member beams
     "dm_lo": 247.597, "dm_hi": 263.438,
     "outcome": "triggered",
     "detail": "tier B, peak S/N 20.7 at DM 247.6; tier_B"}
  ],
  "rules": {...},           # the thresholds t2d applied, see below
  "storm": {"raw_recent": [31, 12, 4404, 88, 701],  # post-width-drop counts, oldest first, this gulp last
            "stormy": false}
}
```

Units and conventions:

- `dt_s` is seconds relative to the card's `samp`, which is the triggered
  cluster's peak sample, to 4 decimals. The triggered cluster's peak is
  therefore at `dt_s` 0.0, and the card-level `snr`, `dm`, `beam` and `width`
  are that peak's.
- `samp_lo`/`samp_hi` are hella samples. hella's gulp g covers
  `g*8192 - 960 .. g*8192 + 7231` (`gulp_card.HELLA_GULP_LEAD_SAMP`, measured
  over the T2 DB); the span is widened to cover the trials if a hella
  configuration change ever moves that. `samp // 8192` is NOT the gulp number.
- `dm` in pc cm^-3 to 3 decimals, `snr` hella's reported S/N to 2 decimals,
  `width_idx` hella's log2 boxcar index.
- `cluster_id` is the index of the cluster in t2d's list, which is sorted by
  descending peak S/N. `n_trials` counts raw trials, so the trials of cluster k
  number `clusters[k].n_trials` when nothing is truncated. `dm_lo`/`dm_hi` are
  the envelope t2d decided on (as stored in the `clusters` table), taken over
  the dedup-grid representatives.
- `storm.raw_recent` is the post-width-drop count of the last five gulps t2d
  flushed, quiet and skipped gulps included. `storm.stormy` is true when the
  storm cap shed candidates from this gulp. The live config drops such a gulp
  whole (`storm_skip_gulp: true`), so a live card has `stormy: false`.

## Size caps

A gulp reaches DBSCAN with up to `max_cands_per_gulp` (20000) trials. The card
carries at most `MAX_CARD_TRIALS` = 10000 trials and `MAX_CARD_CLUSTERS` = 1000
clusters. Over a cap the triggered cluster's trials are kept first, then the
rest by descending S/N, still written in time order; clusters keep the
triggered one plus the brightest. `trials_truncated` / `clusters_truncated`
say so and `n_trials_total` / `n_clusters_total` give the full counts, so a
figure title can read "10000 of 18234 T1 candidates". With clusters truncated
a trial's `cluster_id` can name a cluster that is not listed. A capped
20000-trial card measures about 750 kB including the pointing table.

## Outcomes

One outcome per cluster, exactly one `triggered` per card (an injection gulp
block can have none; see below). t2d's chain order:

| outcome | meaning |
|---|---|
| triggered | the dumped cluster, this card |
| also_triggerable | passed every rule; a brighter cluster in this gulp dumped first (one dump per gulp, recorded `suppressed`, `gulp_dup`) |
| fast_triggered | already dumped by the fast path |
| injection | matched the injection ledger, never dumped |
| veto | peak beam on `filters.beam_veto` |
| too_many_beams | more than `max_beams` distinct beams (tag `rfi_wide`) |
| too_wide | member beams span more than `max_extent_deg` on the sky (tag `rfi_wide`) |
| dm_floor_leak | zero-DM impulse at hella's lowest DM trials, or within the occupancy window of one (tag `dm_floor`) |
| occupancy | at least `occupancy.min_beams` distinct beams within +-`occupancy.window_samp` of the peak in the raw candidates |
| snr_below | peak S/N under tier B |
| dm_below | tier A or B but peak DM under `dm_floor` |

`detail` is free text naming the tier, peak S/N and DM and the failing rule.

## Injection gulp blocks

An injection is never dumped, so it never gets a card. For the replay figure
(`t3-replay-injection`) t2d writes the same block for every gulp with an
`injection` cluster, one file per injection cluster, to `injection.gulp_dir`
(`/mnt/nvme5/casm_pipeline/injection_gulps` on corr1), named
`<utc_start>_g<gulp>_c<cluster_id>.json` (`_s<samp>_b<beam>` instead of
`_c<cluster_id>` if the clusters row was not stored). The file is the bare block,
not a card. It differs from a card's block in three ways:

- `dt_s` is relative to the injection cluster's peak sample.
- The injection cluster keeps outcome `injection`, and every other cluster keeps
  the outcome t2d gave it, so a real event dumped in the same gulp reads
  `triggered` here too.
- A `reference` names the injection cluster:

```
"reference": {"id": 0,                 # index in clusters / trial cluster_id
              "outcome": "injection",
              "cluster_id": 51234,     # clusters.id, = injections.matched_cluster
              "candname": "2609221234",
              "samp": 57010, "beam": 100,              # clusters.samp / .beam
              "event_utc": "2026-09-22T20:31:04.117+00:00"}   # clusters.event_utc, verbatim
```

t2d writes after the gulp's decisions are recorded, in a thread, atomically
(temp file and rename), and prunes files older than `injection.gulp_keep_days`
(14) in the same directory on each write. A failure is logged and changes
nothing else. Unset `gulp_dir` turns it off.

## Rules as t2d writes them

The v2 schema names come first, where t2d has the same rule, then t2d's own
settings. Values from `config/t2d.yaml` as shipped 2026-10-13:

```
"rules": {"snr": 12.0,              # tier B, the lowest tier that triggers
          "dm_floor": 20.0,
          "max_beams": 32,          # filters.max_nbeam
          "max_extent_deg": 25.0,
          "storm_skip": 20000,      # max_cands_per_gulp when storm_skip_gulp, else 0
          "injections_dump": false,
          "tiers": {"A": 30.0, "B": 12.0, "C": 12.0},
          "beam_veto": [], "veto_widths": [6],
          "dm_floor_veto": {"max_dm_lo": 20.6, "min_width": 4},
          "occupancy": {"min_beams": 8, "window_samp": 256},
          "max_cands_per_gulp": 20000, "storm_skip_gulp": true,
          "storm_lockout_s": 300.0,
          "storm_lockout_reset": "triggered", "min_spacing_s": 60.0}
```

## The v2 chain (backtest only)

`casm_t2/chain_v2.py` and `apps/backtest_v2.py` on branch `t2-v2-chain` write the
same block from a different decision chain: DBSCAN over (samp, dm_idx, log2
width) only, no sky axis, one S/N threshold and five rules in the order
injection, snr_below, too_many_beams, too_wide, dm_below, storm, then triggered
/ also_triggerable. Its rules block is

```
"rules": {"snr": 14.0, "dm_floor": 30.0, "max_beams": 8,
          "max_extent_deg": 25.0, "storm_skip": 20000,
          "quiet_sum": 2000, "quiet_gulps": 5, "injections_dump": false}
```

with `storm.stormy` true while the last `quiet_gulps` counts sum above
`quiet_sum`. That chain is not live. Its backtest cards number gulps
`samp // 8192`, which is one off hella's number for events early in a gulp, and
its demo cards can force a cluster to `triggered` so the card-level dm/snr need
not match it.
