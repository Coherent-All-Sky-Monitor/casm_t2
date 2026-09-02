"""insert_clusters stores the sky block when present and NULLs when absent."""
from casm_t2 import db


def test_sky_columns(conn, cluster_row):
    sky = {"weights_id": "abc", "alt_deg": 40.0, "az_deg": 216.5, "ra_deg": 187.9, "dec_deg": -5.66}
    with_sky = tuple(cluster_row("260902aaaaaa")) + (sky,)
    without = cluster_row("260902bbbbbb")
    ids = db.insert_clusters(conn, [with_sky, without])
    assert all(i is not None for i in ids)
    rows = conn.execute("select name, weights_id, alt_deg, az_deg, ra_deg, dec_deg"
                        " from clusters order by id").fetchall()
    assert rows[0] == ("260902aaaaaa", "abc", 40.0, 216.5, 187.9, -5.66)
    assert rows[1][1:] == (None, None, None, None, None)
