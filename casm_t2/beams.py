"""Mapping from global beam number to the machine and stream that own it.

The 512 search beams are split into 8 streams of 64 beams. Streams are
numbered globally across the two backend nodes: streams 0-3 live on
casm-corr1, streams 4-7 on casm-corr2. Each stream has a casm_cand_dump
daemon listening on control port 28000 + stream, and its dumps are written
on the owning node under cand_beam_dumps/stream_<stream>/.
"""

from __future__ import annotations

from dataclasses import dataclass

NBEAM_PER_STREAM = 64
NSTREAM = 8

INTENSITY_CONTROL_BASE_PORT = 28000
CAND_BEAM_DUMP_DIR = "/mnt/nvme4/data/casm/cand_beam_dumps"
T2_SPOOL_DIR = "/mnt/nvme4/data/casm/t2_spool"

STREAM_HOSTS = {
    0: "casm-corr1", 1: "casm-corr1", 2: "casm-corr1", 3: "casm-corr1",
    4: "casm-corr2", 5: "casm-corr2", 6: "casm-corr2", 7: "casm-corr2",
}

# Voltage dumps live on the ANTENNA-side processing chain (medusa_antenna.cfg):
# its own casm_cand_dump daemons on the raw voltage ring, 3 streams per node,
# control port 27000 + stream. Every antenna sees every source, so a voltage
# trigger fans out to ALL of these endpoints.
VOLTAGE_CONTROL_BASE_PORT = 27000
VOLTAGE_DUMP_DIR = "/mnt/nvme4/data/casm/cand_dumps"
VOLTAGE_MOUNT = "/mnt/nvme4"

VOLTAGE_ENDPOINTS = [
    ("casm-corr1", 27000), ("casm-corr1", 27001), ("casm-corr1", 27002),
    ("casm-corr2", 27003), ("casm-corr2", 27004), ("casm-corr2", 27005),
]

NVOLTAGE_STREAM = len(VOLTAGE_ENDPOINTS)


def voltage_endpoint(stream: int) -> tuple[str, int]:
    """(host, control port) of an antenna-stream voltage dump daemon."""
    if not 0 <= stream < NVOLTAGE_STREAM:
        raise ValueError(f"voltage stream {stream} out of range 0-{NVOLTAGE_STREAM - 1}")
    return VOLTAGE_ENDPOINTS[stream]


@dataclass(frozen=True, slots=True)
class StreamLocation:
    """Where a stream's dump daemon and output directory live."""

    stream: int
    host: str
    control_port: int
    dump_dir: str
    spool_dir: str


def stream_for_beam(beam: int) -> int:
    """Global stream index owning a global beam number."""
    if not 0 <= beam < NBEAM_PER_STREAM * NSTREAM:
        raise ValueError(f"beam {beam} out of range")
    return beam // NBEAM_PER_STREAM


def local_beam(beam: int) -> int:
    """Beam index within its stream's 64-beam dump (0-63)."""
    return beam % NBEAM_PER_STREAM


def stream_location(stream: int) -> StreamLocation:
    """Dump-control endpoint and directories for a global stream index."""
    if stream not in STREAM_HOSTS:
        raise ValueError(f"stream {stream} out of range")
    return StreamLocation(
        stream=stream,
        host=STREAM_HOSTS[stream],
        control_port=INTENSITY_CONTROL_BASE_PORT + stream,
        dump_dir=f"{CAND_BEAM_DUMP_DIR}/stream_{stream}",
        spool_dir=T2_SPOOL_DIR,
    )
