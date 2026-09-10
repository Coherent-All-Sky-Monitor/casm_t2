"""Render every injection Slack message offline, for review before go-live.

Nothing here touches Slack. It reads the ledger, builds the same text the
poster would send through the same pure functions, and writes it out as
.txt plus a PNG "card" per message (the colour bar down the left is the
attachment colour Slack would show), then the daily summary text and the
three summary figures.

    t2-inject-slack-preview --db /path/t2.sqlite --out /tmp/preview \\
        --ids 657,658,659 --day 2026-09-09

With no --ids the day's shots are used; with no --day, today (UTC).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from casm_t2 import db, inject_slack
from casm_t2.apps.inject_daemon import LEDGER_SELECT


def _rows(conn, ids: list[int] | None, day: str) -> list[dict]:
    if ids:
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(
            LEDGER_SELECT + f" WHERE i.id IN ({marks}) ORDER BY i.id", ids)
    else:
        cur = conn.execute(
            LEDGER_SELECT + " WHERE i.inject_utc >= ? AND i.inject_utc < ?"
            " ORDER BY i.id", (f"{day}T00:00", f"{day}T23:59:59.999"))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def preview(conn, ids: list[int] | None, day: str, out_dir: Path,
            web_base: str = inject_slack.DEFAULT_WEB_BASE,
            icfg: dict | None = None) -> list[Path]:
    """Write every message and figure into out_dir; returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Rows reconciled before the outcome column existed carry outcome NULL;
    # classify them here so the preview shows what the bot would have said.
    rows = [r if r.get("outcome") else dict(r, outcome=_backfill_outcome(r))
            for r in _rows(conn, ids, day)]
    written: list[Path] = []
    # The poster runs too, in dry-run mode, so the preview exercises the same
    # code path the daemon would take. Its own copies land in a subdirectory
    # to keep the reviewable files at the top level.
    poster = inject_slack.SlackPoster(enabled=True,
                                      dry_run_dir=out_dir / "dry_run",
                                      web_base=web_base, icfg=icfg)

    for row in rows:
        rid = row.get("id")
        poster.post_sent(row)
        sent = inject_slack.sent_text(row)
        written.append(_write(out_dir / f"inj{rid}_sent.txt", sent))
        written.append(inject_slack.render_card(
            sent, inject_slack.COLOR_NEUTRAL, out_dir / f"inj{rid}_sent.png"))

        poster.post_outcome(row)
        line = inject_slack.outcome_text(row, web_base)
        color = inject_slack.outcome_color(row)
        written.append(_write(out_dir / f"inj{rid}_outcome.txt",
                              f"[{color}] {line}"))
        written.append(inject_slack.render_card(
            line, color, out_dir / f"inj{rid}_outcome.png"))

    written.append(_write(out_dir / "summary.txt",
                          inject_slack.summary_text(rows, day, icfg)))
    written.extend(inject_slack.render_summary_figures(rows, out_dir, icfg))

    n, sids, why = _streak(rows)
    if n:
        written.append(_write(out_dir / "streak.txt",
                              inject_slack.streak_text(n, sids, why)))
    return written


def _backfill_outcome(row: dict) -> str:
    from casm_t2 import inject_outcome
    return inject_outcome.classify(row.get("gate_t1"), row.get("gate_t2"),
                                   row.get("gate_trigger"),
                                   row.get("fail_reason"))


def _streak(rows: list[dict]):
    from casm_t2 import inject_outcome
    ids, latest = [], None
    for r in reversed(rows):
        if r.get("outcome") not in inject_outcome.MISSES:
            break
        latest = latest or r.get("outcome")
        ids.append(r.get("id"))
    return len(ids), ids[:10], latest


def _write(path: Path, text: str) -> Path:
    path.write_text(text + "\n")
    return path


def main() -> None:
    p = argparse.ArgumentParser(
        description="Render the injection Slack messages offline (no network)")
    p.add_argument("--db", default=db.DEFAULT_PATH)
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--ids", help="comma-separated injection ids")
    p.add_argument("--day", help="UTC day YYYY-MM-DD (default: today)")
    p.add_argument("--web-base", default=inject_slack.DEFAULT_WEB_BASE,
                   help="base URL of the t3 web app, for the recovered link")
    p.add_argument("--config", help="t2d.yaml, for the summary DM bins")
    args = p.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    day = args.day or inject_slack.utc_day()
    conn = db.connect(args.db)
    try:
        icfg = None
        if args.config:
            import yaml
            with open(args.config) as fh:
                icfg = (yaml.safe_load(fh) or {}).get("injection")
        paths = preview(conn, ids, day, Path(args.out), args.web_base, icfg)
    finally:
        conn.close()
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
