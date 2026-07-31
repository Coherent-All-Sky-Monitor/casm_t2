"""insert_clusters must lose at most the offending row, never the gulp.

Before 2026-07-31 an intra-batch name collision raised IntegrityError out
of the single wrapping transaction, and every cluster in that gulp was
discarded.
"""

from casm_t2 import db


def stored_names(conn):
    return [r[0] for r in conn.execute("SELECT name FROM clusters ORDER BY id")]


def test_clean_batch_returns_ids_in_input_order(conn, cluster_row):
    rows = [cluster_row(n) for n in ("260731aaaaaa", "260731bbbbbb",
                                     "260731cccccc")]
    ids = db.insert_clusters(conn, rows)

    assert len(ids) == 3
    assert all(i is not None for i in ids)
    assert ids == sorted(ids)
    assert stored_names(conn) == ["260731aaaaaa", "260731bbbbbb", "260731cccccc"]


def test_duplicate_of_an_existing_name_skips_only_that_row(conn, cluster_row):
    db.insert_clusters(conn, [cluster_row("260731bbbbbb")])

    rows = [cluster_row("260731dddddd", gulp=2),
            cluster_row("260731bbbbbb", gulp=2),   # already in the DB
            cluster_row("260731eeeeee", gulp=2)]
    ids = db.insert_clusters(conn, rows)

    assert ids[1] is None
    assert ids[0] is not None and ids[2] is not None
    assert stored_names(conn) == ["260731bbbbbb", "260731dddddd", "260731eeeeee"]


def test_duplicate_within_the_batch_skips_only_the_second(conn, cluster_row):
    """The birthday collision that used to discard the gulp."""
    rows = [cluster_row("260731ffffff"),
            cluster_row("260731gggggg"),
            cluster_row("260731ffffff"),   # collides with row 0
            cluster_row("260731hhhhhh")]
    ids = db.insert_clusters(conn, rows)

    assert len(ids) == len(rows)
    assert ids[2] is None
    assert [i is None for i in ids] == [False, False, True, False]
    assert stored_names(conn) == ["260731ffffff", "260731gggggg", "260731hhhhhh"]


def test_ids_map_back_to_the_right_rows(conn, cluster_row):
    """The id_by_name mapping t2d builds must survive the None holes."""
    rows = [cluster_row("260731iiiiii", snr=11.0),
            cluster_row("260731iiiiii", snr=22.0),   # duplicate, skipped
            cluster_row("260731jjjjjj", snr=33.0)]
    ids = db.insert_clusters(conn, rows)

    id_by_name = {row[6]: cid for row, cid in zip(rows, ids) if cid is not None}
    assert set(id_by_name) == {"260731iiiiii", "260731jjjjjj"}

    for name, cid in id_by_name.items():
        got = conn.execute("SELECT name FROM clusters WHERE id = ?",
                           (cid,)).fetchone()[0]
        assert got == name
    # the surviving row is the first one, with its own SNR
    assert conn.execute("SELECT snr FROM clusters WHERE name = ?",
                        ("260731iiiiii",)).fetchone()[0] == 11.0


def test_empty_batch(conn):
    assert db.insert_clusters(conn, []) == []


def test_transaction_still_commits_after_a_skip(conn, cluster_row):
    """A skipped row must not leave the connection mid-transaction."""
    db.insert_clusters(conn, [cluster_row("260731kkkkkk"),
                              cluster_row("260731kkkkkk")])
    assert not conn.in_transaction
    assert stored_names(conn) == ["260731kkkkkk"]


def test_null_names_do_not_collide(conn, cluster_row):
    """The unique index is partial (WHERE name IS NOT NULL)."""
    ids = db.insert_clusters(conn, [cluster_row(None), cluster_row(None)])
    assert all(i is not None for i in ids)
