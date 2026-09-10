"""Registry of beamformer weights that have been live, and the beam pointings that go with them.

Why this exists (2026-09-02): T3 printed beam coordinates from a static table that
was never refreshed after weights uploads, so every candidate coordinate posted from
2026-08-19 to 2026-09-02 was wrong by a median 45 deg. Nothing in the fourier-space
pipeline (medusa, bfcorr, redis, the rings) may be changed to fix this, so the truth
is reconstructed from what our own tools do and what can be observed from outside:

* ``deploy_bf_weights`` (the only sanctioned upload path) records every product it
  serialises (per-stream payload md5s and the 512-beam alt/az table) and a live
  event for every stream it pushes.
* ``t3-weights-watch`` tails the medusa weights-daemon log; a transfer that no
  upload accounts for is bfcorr reloading its defaults after a restart, and is
  identified by hashing the defaults files bfcorr read.
* t2d resolves (beam, event UTC) -> pointings through the live-event timeline and
  stamps alt/az/RA/Dec into every cluster row and trigger card at insert time.

Layout of ``REGISTRY_DIR`` (append-only, plain files, corr1 disk):

    products/<product_id>.json    one per weights product
    payload_index.json            {payload_md5: product_id}
    live_events.jsonl             one line per weights load on one bfcorr stream
    alerts.jsonl                  pipeline faults seen by the watcher

bfcorr runs six streams per array, one per 512-channel sub-band, each forming all
512 beams, so a product's pointings apply to every stream; streams that disagree
mean a partial deploy and no single pointing exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_DIR = Path(os.environ.get("CASM_WEIGHTS_REGISTRY",
                                   "/mnt/nvme5/casm_pipeline/weights/registry"))
HDR_SIZE = 4096            # DADA header in front of every payload / defaults file
NSTREAM = 6                # bfcorr streams (sub-bands), 0-2 corr1, 3-5 corr2
STREAM_HOST = {0: "casm-corr1", 1: "casm-corr1", 2: "casm-corr1",
               3: "casm-corr2", 4: "casm-corr2", 5: "casm-corr2"}
DEFAULTS_FILE_FMT = "/data/casm/default_weights_64ant_512beam/direct.dada.{stream}"
NBEAM = 512

OVRO_LAT_DEG = 37.2339
OVRO_LON_DEG = -118.2817
OVRO_ALT_M = 1222.0


# --------------------------------------------------------------------- hashing
def payload_md5_of_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def payload_md5_of_file(path: str | Path, hdr_size: int = HDR_SIZE) -> str:
    """md5 of a DADA file's payload, skipping the header (which carries the upload UTC)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        f.seek(hdr_size)
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def product_id_from_payloads(stream_md5: dict[int, str]) -> str:
    """Deterministic product id: md5 over the per-stream payload md5s in stream order."""
    parts = [f"{s}:{stream_md5[s]}" for s in sorted(stream_md5)]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------- storage
class Registry:
    def __init__(self, root: str | Path = REGISTRY_DIR):
        self.root = Path(root)
        self.products_dir = self.root / "products"
        self.index_path = self.root / "payload_index.json"
        self.events_path = self.root / "live_events.jsonl"
        self.alerts_path = self.root / "alerts.jsonl"
        self._events_cache: tuple[float, list[dict]] | None = None
        self._product_cache: dict[str, dict] = {}

    # -- writing ---------------------------------------------------------
    def _ensure(self) -> None:
        self.products_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write(self, path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def record_product(self, *, h5_path: str, stream_md5: dict[int, str],
                       alt_deg: list[float], az_deg: list[float],
                       beam_fwhm_x_deg: float | None = None,
                       beam_fwhm_y_deg: float | None = None,
                       meta: dict | None = None) -> str:
        """Register a weights product. Idempotent: re-registering the same payloads
        returns the existing id and only refreshes the payload index.

        ``beam_fwhm_x_deg`` / ``beam_fwhm_y_deg`` are the synthesised beam's E-W
        and N-S FWHM in degrees for THIS product's enabled antennas
        (``bf_weights_generator.config.compute_beam_fwhm``); both optional, both
        None when the geometry was not available. A product registered without
        them can be filled in later by :meth:`set_product_fwhm` (t3-weights-watch
        --backfill-fwhm), which is also what re-registering with them does.
        """
        if len(alt_deg) != NBEAM or len(az_deg) != NBEAM:
            raise ValueError(f"pointing table must have {NBEAM} beams")
        self._ensure()
        pid = product_id_from_payloads(stream_md5)
        path = self.products_dir / f"{pid}.json"
        if not path.exists():
            rec = {
                "product_id": pid,
                "h5_path": str(h5_path),
                "stream_payload_md5": {str(k): v for k, v in sorted(stream_md5.items())},
                "alt_deg": [round(float(a), 4) for a in alt_deg],
                "az_deg": [round(float(a), 4) for a in az_deg],
                "beam_fwhm_x_deg": _round_fwhm(beam_fwhm_x_deg),
                "beam_fwhm_y_deg": _round_fwhm(beam_fwhm_y_deg),
                "recorded_utc": _now_iso(),
                "meta": meta or {},
            }
            self._atomic_write(path, json.dumps(rec))
        elif beam_fwhm_x_deg is not None and beam_fwhm_y_deg is not None:
            self.set_product_fwhm(pid, beam_fwhm_x_deg, beam_fwhm_y_deg)
        index = self._load_index()
        changed = False
        for md5 in stream_md5.values():
            if index.get(md5) != pid:
                index[md5] = pid
                changed = True
        if changed:
            self._atomic_write(self.index_path, json.dumps(index, indent=0, sort_keys=True))
        self._product_cache.pop(pid, None)
        return pid

    def set_product_fwhm(self, product_id: str, beam_fwhm_x_deg: float | None,
                         beam_fwhm_y_deg: float | None, *, overwrite: bool = False) -> bool:
        """Store the beam ellipse on an existing product record.

        Returns True when the record was rewritten. Products registered before
        the ellipse existed (2026-09-09) carry no FWHM keys at all; this is how
        they get one without re-uploading anything. Existing values are kept
        unless ``overwrite``."""
        path = self.products_dir / f"{product_id}.json"
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if not overwrite and rec.get("beam_fwhm_x_deg") is not None:
            return False
        rec["beam_fwhm_x_deg"] = _round_fwhm(beam_fwhm_x_deg)
        rec["beam_fwhm_y_deg"] = _round_fwhm(beam_fwhm_y_deg)
        self._atomic_write(path, json.dumps(rec))
        self._product_cache.pop(product_id, None)
        return True

    def product_ids(self) -> list[str]:
        """Every registered product id, sorted (the backfill walks these)."""
        try:
            return sorted(p.stem for p in self.products_dir.glob("*.json"))
        except OSError:
            return []

    def record_live_event(self, *, utc: datetime | str, stream: int, payload_md5: str | None,
                          source: str, node: str | None = None,
                          evidence: str = "") -> dict:
        """Append a weights-load event for one bfcorr stream. ``source`` is
        'upload', 'defaults' or 'unknown'. Returns the event as stored."""
        self._ensure()
        pid = self.lookup_payload(payload_md5) if payload_md5 else None
        ev = {
            "utc": utc if isinstance(utc, str) else utc.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "stream": int(stream),
            "node": node or STREAM_HOST.get(int(stream), ""),
            "payload_md5": payload_md5,
            "product_id": pid,
            "source": source,
            "evidence": evidence,
            "recorded_utc": _now_iso(),
        }
        with open(self.events_path, "a") as f:
            f.write(json.dumps(ev) + "\n")
        self._events_cache = None
        return ev

    def record_alert(self, kind: str, text: str, **fields) -> None:
        self._ensure()
        rec = {"utc": _now_iso(), "kind": kind, "text": text, **fields}
        with open(self.alerts_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # -- reading ---------------------------------------------------------
    def _load_index(self) -> dict[str, str]:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            return {}

    def lookup_payload(self, payload_md5: str) -> str | None:
        return self._load_index().get(payload_md5)

    def product(self, product_id: str | None) -> dict | None:
        if not product_id:
            return None
        if product_id in self._product_cache:
            return self._product_cache[product_id]
        try:
            rec = json.loads((self.products_dir / f"{product_id}.json").read_text())
        except (OSError, ValueError):
            return None
        self._product_cache[product_id] = rec
        return rec

    def events(self) -> list[dict]:
        """All live events, oldest first (mtime-cached)."""
        try:
            mtime = self.events_path.stat().st_mtime
        except OSError:
            return []
        if self._events_cache and self._events_cache[0] == mtime:
            return self._events_cache[1]
        out = []
        with open(self.events_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        out.sort(key=lambda e: e["utc"])
        self._events_cache = (mtime, out)
        return out

    def live_at(self, utc: datetime | str) -> dict[int, dict]:
        """Latest event per stream with event utc <= the given utc."""
        t = utc if isinstance(utc, str) else utc.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        state: dict[int, dict] = {}
        for ev in self.events():
            if ev["utc"] <= t:
                state[int(ev["stream"])] = ev
        return state

    def product_at(self, utc: datetime | str) -> tuple[dict | None, str]:
        """(product record, status). status: 'ok' when every known stream carries the
        same identified product; 'partial' when streams disagree; 'unknown' when the
        live payload is not a registered product; 'none' when no event exists."""
        state = self.live_at(utc)
        if not state:
            return None, "none"
        pids = {ev.get("product_id") for ev in state.values()}
        if None in pids:
            return None, "unknown"
        if len(pids) != 1:
            return None, "partial"
        return self.product(pids.pop()), "ok"

    def pointings_for(self, utc: datetime) -> dict | None:
        """{'weights_id', 'alt_deg'[512], 'az_deg'[512], 'beam_fwhm_x_deg', 'beam_fwhm_y_deg'}
        live at utc, for self-contained cards. The two FWHMs are the beam ellipse of
        this very product (E-W, N-S, degrees) and are None for products registered
        before 2026-09-09 that no backfill has reached."""
        prod, status = self.product_at(utc)
        if prod is None:
            return None
        return {"weights_id": prod["product_id"], "alt_deg": prod["alt_deg"], "az_deg": prod["az_deg"],
                "beam_fwhm_x_deg": prod.get("beam_fwhm_x_deg"),
                "beam_fwhm_y_deg": prod.get("beam_fwhm_y_deg")}

    def sky_for(self, utc: datetime, beam: int, radec: bool = True, sun: bool = False) -> dict | None:
        """alt/az (and RA/Dec, sun) of a beam at an event time, from the weights live then.
        None when no single trustworthy pointing exists (callers store NULLs).
        ``radec=False`` skips the astropy transform; use ``fill_radec`` to do many at once."""
        prod, status = self.product_at(utc)
        if prod is None or not 0 <= beam < NBEAM:
            return None
        out = {"weights_id": prod["product_id"], "alt_deg": prod["alt_deg"][beam],
               "az_deg": prod["az_deg"][beam], "ra_deg": None, "dec_deg": None}
        if radec:
            fill_radec([out], [utc])
        if sun:
            try:
                srcs = source_altaz(utc)
                out["sun_alt_deg"], out["sun_az_deg"] = srcs["Sun"]
                out["sources"] = {k: list(v) for k, v in srcs.items()}
            except Exception as exc:
                logger.debug("sun/source positions unavailable: %s", exc)
        return out


def fill_radec(skies: list[dict | None], utcs: list[datetime]) -> None:
    """Fill ra_deg/dec_deg in place for every non-None sky dict, one vectorised
    astropy transform for the whole batch (a storm gulp stores hundreds of rows)."""
    idx = [i for i, sk in enumerate(skies) if sk and sk.get("alt_deg") is not None]
    if not idx:
        return
    try:
        from astropy.coordinates import AltAz, EarthLocation, SkyCoord
        from astropy.time import Time
        import astropy.units as u
        loc = EarthLocation(lat=OVRO_LAT_DEG * u.deg, lon=OVRO_LON_DEG * u.deg, height=OVRO_ALT_M * u.m)
        t = Time([utcs[i].astimezone(timezone.utc).replace(tzinfo=None) for i in idx])
        c = SkyCoord(alt=[skies[i]["alt_deg"] for i in idx] * u.deg,
                     az=[skies[i]["az_deg"] for i in idx] * u.deg,
                     frame=AltAz(obstime=t, location=loc)).icrs
        for k, i in enumerate(idx):
            skies[i]["ra_deg"] = round(float(c.ra.deg[k]), 5)
            skies[i]["dec_deg"] = round(float(c.dec.deg[k]), 5)
    except Exception as exc:
        logger.warning("RA/Dec conversion failed, alt/az kept: %s", exc)


# Bright calibrator sources marked on the candidate sky panel (ICRS J2000, deg).
SKY_SOURCES = {"Cas A": (350.850, 58.815), "Cyg A": (299.868, 40.734), "Tau A": (83.633, 22.014)}


def source_altaz(utc: datetime) -> dict[str, tuple[float, float]]:
    """alt/az (deg) of the sun and the SKY_SOURCES at ``utc``; all of them, the
    caller decides what to draw (below-horizon ones are not plotted)."""
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
    from astropy.time import Time
    import astropy.units as u
    loc = EarthLocation(lat=OVRO_LAT_DEG * u.deg, lon=OVRO_LON_DEG * u.deg, height=OVRO_ALT_M * u.m)
    t = Time(utc.astimezone(timezone.utc).replace(tzinfo=None))
    frame = AltAz(obstime=t, location=loc)
    out = {}
    s = get_sun(t).transform_to(frame)
    out["Sun"] = (round(float(s.alt.deg), 2), round(float(s.az.deg), 2))
    for name, (ra, dec) in SKY_SOURCES.items():
        c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg).transform_to(frame)
        out[name] = (round(float(c.alt.deg), 2), round(float(c.az.deg), 2))
    return out


def sun_altaz(utc: datetime) -> tuple[float, float]:
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time
    import astropy.units as u
    loc = EarthLocation(lat=OVRO_LAT_DEG * u.deg, lon=OVRO_LON_DEG * u.deg, height=OVRO_ALT_M * u.m)
    t = Time(utc.astimezone(timezone.utc).replace(tzinfo=None))
    s = get_sun(t).transform_to(AltAz(obstime=t, location=loc))
    return round(float(s.alt.deg), 2), round(float(s.az.deg), 2)


def altaz_to_radec(alt_deg: float, az_deg: float, utc: datetime) -> tuple[float, float]:
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    loc = EarthLocation(lat=OVRO_LAT_DEG * u.deg, lon=OVRO_LON_DEG * u.deg, height=OVRO_ALT_M * u.m)
    t = Time(utc.astimezone(timezone.utc).replace(tzinfo=None))
    c = SkyCoord(alt=alt_deg * u.deg, az=az_deg * u.deg, frame=AltAz(obstime=t, location=loc))
    icrs = c.icrs
    return float(icrs.ra.deg), float(icrs.dec.deg)


def hms_dms(ra_deg: float, dec_deg: float) -> tuple[str, str]:
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    c = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    return (c.ra.to_string(unit=u.hourangle, sep="hms", precision=1, pad=True),
            c.dec.to_string(unit=u.deg, sep="dms", precision=0, alwayssign=True, pad=True))


def _round_fwhm(v: float | None) -> float | None:
    return None if v is None else round(float(v), 3)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_default: Registry | None = None


def default_registry() -> Registry:
    global _default
    if _default is None:
        _default = Registry()
    return _default

def beam_separation_arcsec(pointings: dict | None, beam_a: int, beam_b: int) -> float | None:
    """Great-circle separation between two beams' pointings, in arcseconds.

    `pointings` is what `Registry.pointings_for` returns. Alt/az of the two
    beams at the same instant, so the separation is the angle on the sky
    between where the injection was put and where it came back. Returns 0.0
    for the same beam and None when there is no usable pointing table.
    """
    import math
    if not pointings:
        return None
    if beam_a == beam_b:
        return 0.0
    alt, az = pointings.get("alt_deg"), pointings.get("az_deg")
    if alt is None or az is None:
        return None
    try:
        a1, z1 = math.radians(alt[beam_a]), math.radians(az[beam_a])
        a2, z2 = math.radians(alt[beam_b]), math.radians(az[beam_b])
    except (IndexError, TypeError, ValueError):
        return None
    cos_sep = (math.sin(a1) * math.sin(a2)
               + math.cos(a1) * math.cos(a2) * math.cos(z1 - z2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep)))) * 3600.0
