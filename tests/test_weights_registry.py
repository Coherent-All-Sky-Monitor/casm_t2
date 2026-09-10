"""Registry: products, live events, time lookup, partial/unknown states."""
from datetime import datetime, timezone, timedelta

from casm_t2 import weights_registry as wr


def _alt_az(offset=0.0):
    return [30.0 + (i % 60) + offset for i in range(512)], [float(i * 360 / 512) for i in range(512)]


def _t(s):
    return datetime(2026, 9, 2, 20, 0, s, tzinfo=timezone.utc)


def test_product_and_events_resolve_by_time(tmp_path):
    reg = wr.Registry(tmp_path)
    alt, az = _alt_az()
    md5a = {s: f"a{s}" for s in range(6)}
    pid_a = reg.record_product(h5_path="/a.h5", stream_md5=md5a, alt_deg=alt, az_deg=az)
    for s in range(6):
        reg.record_live_event(utc=_t(0), stream=s, payload_md5=f"a{s}", source="upload")
    alt2, az2 = _alt_az(5.0)
    md5b = {s: f"b{s}" for s in range(6)}
    pid_b = reg.record_product(h5_path="/b.h5", stream_md5=md5b, alt_deg=alt2, az_deg=az2)
    for s in range(6):
        reg.record_live_event(utc=_t(30), stream=s, payload_md5=f"b{s}", source="upload")
    assert pid_a != pid_b
    prod, status = reg.product_at(_t(10))
    assert status == "ok" and prod["product_id"] == pid_a
    prod, status = reg.product_at(_t(40))
    assert status == "ok" and prod["product_id"] == pid_b
    sky = reg.sky_for(_t(40), 7)
    assert sky["weights_id"] == pid_b and abs(sky["alt_deg"] - alt2[7]) < 1e-3 and abs(sky["az_deg"] - az2[7]) < 1e-3
    assert reg.product_at(_t(0) - timedelta(seconds=1))[1] == "none"


def test_partial_and_unknown_states(tmp_path):
    reg = wr.Registry(tmp_path)
    alt, az = _alt_az()
    reg.record_product(h5_path="/a.h5", stream_md5={s: f"a{s}" for s in range(6)}, alt_deg=alt, az_deg=az)
    reg.record_product(h5_path="/b.h5", stream_md5={s: f"b{s}" for s in range(6)}, alt_deg=alt, az_deg=az)
    for s in range(6):
        reg.record_live_event(utc=_t(0), stream=s, payload_md5=f"a{s}", source="upload")
    # one stream reverts to product b: partial deploy, no single pointing
    reg.record_live_event(utc=_t(5), stream=3, payload_md5="b3", source="defaults")
    assert reg.product_at(_t(6))[1] == "partial"
    assert reg.sky_for(_t(6), 100) is None
    # unregistered payload on a stream: unknown
    reg.record_live_event(utc=_t(9), stream=3, payload_md5="zzz", source="unknown")
    assert reg.product_at(_t(10))[1] == "unknown"


def test_payload_md5_skips_header(tmp_path):
    p = tmp_path / "direct.dada.0"
    p.write_bytes(b"H" * wr.HDR_SIZE + b"payload-bytes")
    assert wr.payload_md5_of_file(p) == wr.payload_md5_of_bytes(b"payload-bytes")


def test_radec_roundtrip_is_sane():
    ra, dec = wr.altaz_to_radec(90.0, 0.0, datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc))
    assert abs(dec - wr.OVRO_LAT_DEG) < 0.5   # zenith declination equals latitude


def test_product_roundtrip_with_and_without_fwhm(tmp_path):
    """The beam ellipse is optional and rides along with the pointings."""
    reg = wr.Registry(tmp_path)
    alt, az = _alt_az()
    pid_a = reg.record_product(h5_path="/a.h5", stream_md5={s: f"a{s}" for s in range(6)},
                               alt_deg=alt, az_deg=az,
                               beam_fwhm_x_deg=19.3264, beam_fwhm_y_deg=3.9210)
    pid_b = reg.record_product(h5_path="/b.h5", stream_md5={s: f"b{s}" for s in range(6)},
                               alt_deg=alt, az_deg=az)
    for s in range(6):
        reg.record_live_event(utc=_t(0), stream=s, payload_md5=f"a{s}", source="upload")
    p = reg.pointings_for(_t(1))
    assert p["weights_id"] == pid_a
    assert p["beam_fwhm_x_deg"] == 19.326 and p["beam_fwhm_y_deg"] == 3.921
    for s in range(6):
        reg.record_live_event(utc=_t(10), stream=s, payload_md5=f"b{s}", source="upload")
    p = reg.pointings_for(_t(11))
    assert p["weights_id"] == pid_b
    assert p["beam_fwhm_x_deg"] is None and p["beam_fwhm_y_deg"] is None


def test_old_product_without_fwhm_keys_reads_and_backfills(tmp_path):
    """Products written before 2026-09-09 have no FWHM keys at all."""
    import json
    reg = wr.Registry(tmp_path)
    alt, az = _alt_az()
    pid = reg.record_product(h5_path="/a.h5", stream_md5={s: f"a{s}" for s in range(6)},
                             alt_deg=alt, az_deg=az)
    path = reg.products_dir / f"{pid}.json"
    rec = json.loads(path.read_text())
    rec.pop("beam_fwhm_x_deg"); rec.pop("beam_fwhm_y_deg")
    path.write_text(json.dumps(rec))
    reg._product_cache.clear()
    for s in range(6):
        reg.record_live_event(utc=_t(0), stream=s, payload_md5=f"a{s}", source="upload")
    assert reg.pointings_for(_t(1))["beam_fwhm_x_deg"] is None
    assert reg.product_ids() == [pid]

    assert reg.set_product_fwhm(pid, 18.14, 3.88) is True
    assert reg.pointings_for(_t(1))["beam_fwhm_x_deg"] == 18.14
    # already set: kept unless overwrite
    assert reg.set_product_fwhm(pid, 1.0, 2.0) is False
    assert reg.set_product_fwhm(pid, 1.0, 2.0, overwrite=True) is True
    assert reg.pointings_for(_t(1))["beam_fwhm_y_deg"] == 2.0
    # re-registering the same payloads with an ellipse fills a missing one in
    assert reg.set_product_fwhm(pid, None, None, overwrite=True) is True
    reg.record_product(h5_path="/a.h5", stream_md5={s: f"a{s}" for s in range(6)},
                       alt_deg=alt, az_deg=az, beam_fwhm_x_deg=19.0, beam_fwhm_y_deg=3.9)
    assert reg.pointings_for(_t(1))["beam_fwhm_x_deg"] == 19.0
