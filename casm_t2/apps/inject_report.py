"""Daily injection recovery report.

Aggregates the injections ledger over a window (default 24 h) into the
per-gate funnel the commissioning review needs: how many injections were
made, how many T1 detected, how many T2 clustered, how many would have
triggered, and the named reason for every miss. Prints to stdout and writes
a dated text report; the web UI renders the same numbers live from the DB.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from casm_t2 import db


def report(conn, hours: float) -> str:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="milliseconds")
    rows = conn.execute(
        "SELECT id, inject_utc, beam, dm, amp, sigma_ms, est_snr, gate_t1, gate_t2,"
        " gate_trigger, rec_snr, rec_dm, fail_reason FROM injections"
        " WHERE inject_utc >= ? ORDER BY inject_utc", (since,)).fetchall()
    lines = [f"CASM injection report — last {hours:.0f} h "
             f"(generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)", ""]
    n = len(rows)
    done = [r for r in rows if r[7] is not None]
    t1 = sum(r[7] or 0 for r in done)
    t2 = sum(r[8] or 0 for r in done)
    tr = sum(r[9] or 0 for r in done)
    lines.append(f"injected: {n}   reconciled: {len(done)}")
    if done:
        lines.append(f"gate T1 (detected):      {t1}/{len(done)}  ({t1/len(done)*100:.0f}%)")
        lines.append(f"gate T2 (clustered):     {t2}/{len(done)}  ({t2/len(done)*100:.0f}%)")
        lines.append(f"gate trigger (eligible): {tr}/{len(done)}  ({tr/len(done)*100:.0f}%)")
    lines.append("")
    lines.append(" id  inject_utc           beam    dm   est_snr rec_snr  gates  reason")
    for r in rows:
        gates = "".join("?" if g is None else str(g) for g in (r[7], r[8], r[9]))
        lines.append(f"{r[0]:3d}  {r[1][:19]}  {r[2]:4d} {r[3]:6.1f}  "
                     f"{r[6] or float('nan'):6.1f}  {r[10] or float('nan'):6.1f}  "
                     f"{gates:5s}  {r[12] or 'ok'}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Injection recovery report")
    p.add_argument("--db", default=db.DEFAULT_PATH)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--out-dir", default="/mnt/nvme5/casm_pipeline/reports")
    args = p.parse_args()
    conn = db.connect(args.db)
    text = report(conn, args.hours)
    print(text)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"injections_{datetime.now(timezone.utc):%Y%m%d}.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
