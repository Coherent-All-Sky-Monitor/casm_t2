"""SQLite event store for T2.

One WAL-mode database on corr1 holds the clustered event stream and the
trigger bookkeeping. Raw T1 trials stay in hella's .dat files; this only
records clusters (the objects the trigger logic reasons about), so volume
is a few hundred thousand rows per day at current RFI levels.

Writers open with `connect()`, which applies WAL and a busy timeout so the
daemon and ad-hoc readers (sqlite3 CLI, notebooks) coexist safely.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from casm_t2.cluster import Cluster

DEFAULT_PATH = "/mnt/nvme5/casm_pipeline/db/t2.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
    id            INTEGER PRIMARY KEY,
    obs_utc_start TEXT NOT NULL,     -- PSRDADA UTC of the observation
    gulp          INTEGER,
    event_utc     TEXT NOT NULL,     -- peak trial arrival, ISO 8601
    samp          INTEGER NOT NULL,
    snr           REAL NOT NULL,
    dm            REAL NOT NULL,
    dm_idx        INTEGER NOT NULL,
    width         INTEGER NOT NULL,
    beam          INTEGER NOT NULL,
    n_members     INTEGER NOT NULL,  -- raw T1 trials in the cluster
    n_beams       INTEGER NOT NULL,
    beam_lo       INTEGER NOT NULL,
    beam_hi       INTEGER NOT NULL,
    dm_lo         REAL NOT NULL,
    dm_hi         REAL NOT NULL,
    samp_lo       INTEGER NOT NULL,
    samp_hi       INTEGER NOT NULL,
    tier          TEXT NOT NULL,     -- A/B/C or '-' below tier floor
    tags          TEXT NOT NULL,     -- comma-joined: rfi_wide, veto, would_trigger, ...
    name          TEXT,              -- event name (YYMMDDxxxx), tiered events only
    created_utc   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clusters_name ON clusters(name)
    WHERE name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_clusters_event ON clusters(event_utc);
CREATE INDEX IF NOT EXISTS idx_clusters_snr ON clusters(snr);

CREATE TABLE IF NOT EXISTS gulp_stats (
    id            INTEGER PRIMARY KEY,
    obs_utc_start TEXT NOT NULL,
    gulp          INTEGER,
    gulp_utc      TEXT NOT NULL,     -- arrival UTC of the gulp's earliest trial
    n_jobs        INTEGER NOT NULL,  -- hella jobs heard from (8 = all)
    n_cands       INTEGER NOT NULL,  -- raw T1 trials in
    n_clusters    INTEGER NOT NULL,  -- DBSCAN clusters (incl. noise singletons)
    n_stored      INTEGER NOT NULL,  -- tiered events persisted
    n_would       INTEGER NOT NULL,  -- would-trigger decisions
    clustering_ms REAL NOT NULL,
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gulp_stats_utc ON gulp_stats(gulp_utc);

CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY,
    cluster_id    INTEGER REFERENCES clusters(id),
    candname      TEXT NOT NULL,     -- event name (YYMMDDxxxx)
    stream        INTEGER NOT NULL,  -- -1 for voltage (all-stream fan-out)
    kind          TEXT NOT NULL DEFAULT 'intensity',  -- intensity / voltage
    action        TEXT NOT NULL,     -- triggered / refused / failed / shadow
    detail        TEXT NOT NULL,     -- daemon reply or refusal reason
    dump_utc_start TEXT,
    dump_utc_stop  TEXT,
    bytes_written INTEGER,           -- filled by janitor/plotter when known
    cleaned_utc   TEXT,              -- set when the janitor deletes the dump
    created_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injections (
    id            INTEGER PRIMARY KEY,
    inject_utc    TEXT NOT NULL,     -- FIFO write time
    stream        INTEGER NOT NULL,
    beam          INTEGER NOT NULL,  -- global beam
    dm            REAL NOT NULL,
    amp           REAL NOT NULL,
    sigma_ms      REAL NOT NULL,
    est_snr       REAL,              -- INJECTED_SNR_ESTIMATE from make_noise
    file_id       TEXT NOT NULL,
    -- gate columns filled by t2-inject-report (NULL = not yet reconciled)
    gate_t1       INTEGER,           -- T1 produced a matching candidate
    gate_t2       INTEGER,           -- matched cluster (not noise singleton)
    gate_trigger  INTEGER,           -- would have passed trigger filters
    gate_ml       INTEGER,           -- placeholder for the future classifier
    matched_cluster INTEGER REFERENCES clusters(id),
    rec_snr       REAL,
    rec_dm        REAL,
    fail_reason   TEXT,              -- first failed gate, human-readable
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_injections_utc ON injections(inject_utc);

CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,     -- event name
    label         TEXT NOT NULL,     -- frb / pulsar / rfi / unsure
    who           TEXT NOT NULL DEFAULT 'web',
    notes         TEXT NOT NULL DEFAULT '',
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_name ON labels(name);

CREATE TABLE IF NOT EXISTS frbs (
    id            INTEGER PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,  -- event name; the catalog key
    event_utc     TEXT NOT NULL,
    snr           REAL NOT NULL,
    dm            REAL NOT NULL,
    width         INTEGER NOT NULL,
    beam          INTEGER NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    created_utc   TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created by older schemas."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
    if cols and "name" not in cols:
        conn.execute("ALTER TABLE clusters ADD COLUMN name TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clusters_name"
                     " ON clusters(name) WHERE name IS NOT NULL")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(triggers)")}
    for col, decl in [("kind", "TEXT NOT NULL DEFAULT 'intensity'"),
                      ("dump_utc_start", "TEXT"), ("dump_utc_stop", "TEXT"),
                      ("bytes_written", "INTEGER"), ("cleaned_utc", "TEXT")]:
        if tcols and col not in tcols:
            conn.execute(f"ALTER TABLE triggers ADD COLUMN {col} {decl}")


def connect(path: str | Path = DEFAULT_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    with conn:
        _migrate(conn)
        conn.executescript(SCHEMA)
    return conn


def insert_clusters(conn: sqlite3.Connection,
                    rows: list[tuple[Cluster, str, int | None, str, str, str,
                                     str | None]]) -> list[int]:
    """Insert clusters; each row is
    (cluster, obs_utc_start, gulp, event_utc, tier, tags, name).

    Returns the assigned ids, in input order.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    ids = []
    with conn:
        for cl, obs, gulp, event_utc, tier, tags, name in rows:
            cur = conn.execute(
                "INSERT INTO clusters (obs_utc_start, gulp, event_utc, samp, snr, dm,"
                " dm_idx, width, beam, n_members, n_beams, beam_lo, beam_hi, dm_lo,"
                " dm_hi, samp_lo, samp_hi, tier, tags, name, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs, gulp, event_utc, cl.peak.samp, cl.peak.snr, cl.peak.dm,
                 cl.peak.dm_idx, cl.peak.width, cl.peak.beam, cl.n_members,
                 cl.n_beams, cl.beam_lo, cl.beam_hi, cl.dm_lo, cl.dm_hi,
                 cl.samp_lo, cl.samp_hi, tier, tags, name, now))
            ids.append(cur.lastrowid)
    return ids


def insert_trigger(conn: sqlite3.Connection, cluster_id: int | None, candname: str,
                   stream: int, action: str, detail: str, kind: str = "intensity",
                   dump_utc_start: str | None = None,
                   dump_utc_stop: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with conn:
        conn.execute(
            "INSERT INTO triggers (cluster_id, candname, stream, kind, action,"
            " detail, dump_utc_start, dump_utc_stop, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cluster_id, candname, stream, kind, action, detail,
             dump_utc_start, dump_utc_stop, now))


def insert_gulp_stats(conn: sqlite3.Connection, obs_utc_start: str, gulp: int | None,
                      gulp_utc: str, n_jobs: int, n_cands: int, n_clusters: int,
                      n_stored: int, n_would: int, clustering_ms: float) -> None:
    """One accounting row per coalesced gulp: the T1->T2 survival funnel."""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with conn:
        conn.execute(
            "INSERT INTO gulp_stats (obs_utc_start, gulp, gulp_utc, n_jobs, n_cands,"
            " n_clusters, n_stored, n_would, clustering_ms, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (obs_utc_start, gulp, gulp_utc, n_jobs, n_cands, n_clusters,
             n_stored, n_would, round(clustering_ms, 1), now))
