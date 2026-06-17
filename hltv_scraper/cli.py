"""Command-line interface for the HLTV scraper.

Run ``python -m hltv_scraper.cli --help`` for the full list. Typical full-history
workflow (slow by design — HLTV will block aggressive scraping):

    python -m hltv_scraper.cli init-db
    python -m hltv_scraper.cli rankings        # ranking + roster history
    python -m hltv_scraper.cli players         # per-player, per-period stats
    python -m hltv_scraper.cli matches         # map scoreboards + head-to-head
    python -m hltv_scraper.cli analyze contribution
    python -m hltv_scraper.cli export player_stat_periods stats.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime
from typing import Optional

from .config import Settings
from .db import Database
from .analysis import contribution as contrib
from .analysis import meta as meta_mod


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def _orch(args):
    # Imported lazily so `init-db`/`analyze`/`export` work without network deps.
    from .scrapers import Orchestrator
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings.db_path = args.db
    if getattr(args, "backend", None):
        settings.backend = args.backend
    return Orchestrator(settings)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init_db(args) -> int:
    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db
    settings.ensure_dirs()
    db = Database(settings.db_path)
    print(f"Initialised database at {settings.db_path}")
    print(json.dumps(db.counts(), indent=2))
    db.close()
    return 0


def cmd_rankings(args) -> int:
    orch = _orch(args)
    n = orch.scrape_rankings(_parse_date(args.start), _parse_date(args.end),
                             step_days=args.step_days)
    print(f"Scraped {n} ranking snapshots")
    orch.close()
    return 0


def cmd_players(args) -> int:
    orch = _orch(args)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    n = orch.scrape_players(
        player_ids=ids,
        start=_parse_date(args.start), end=_parse_date(args.end),
        period_months=args.period_months,
        fetch_individual=not args.no_individual,
    )
    print(f"Saved {n} player-period stat records")
    orch.close()
    return 0


def cmd_matches(args) -> int:
    orch = _orch(args)
    n = orch.scrape_matches(
        start=_parse_date(args.start), end=_parse_date(args.end),
        max_pages=args.max_pages,
        with_scoreboards=not args.no_scoreboards,
    )
    print(f"Processed {n} maps")
    orch.close()
    return 0


def cmd_events(args) -> int:
    orch = _orch(args)
    n = orch.scrape_events(max_pages=args.max_pages)
    print(f"Saved {n} events")
    orch.close()
    return 0


def cmd_full(args) -> int:
    """End-to-end: rankings -> players -> matches -> events."""
    orch = _orch(args)
    start, end = _parse_date(args.start), _parse_date(args.end)
    print("[1/4] rankings...")
    orch.scrape_rankings(start, end, step_days=args.step_days)
    print("[2/4] players...")
    orch.scrape_players(start=start, end=end, period_months=args.period_months)
    print("[3/4] matches...")
    orch.scrape_matches(start=start, end=end, max_pages=args.max_pages)
    print("[4/4] events...")
    orch.scrape_events(max_pages=args.max_pages)
    print(json.dumps(orch.db.counts(), indent=2))
    orch.close()
    return 0


def cmd_status(args) -> int:
    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db
    db = Database(settings.db_path)
    print(json.dumps(db.counts(), indent=2))
    db.close()
    return 0


def cmd_analyze(args) -> int:
    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db
    db = Database(settings.db_path)
    if args.what == "contribution":
        results = contrib.compute_contributions(db, min_maps=args.min_maps)
        rows = [r.as_dict() for r in results]
        rows.sort(key=lambda r: r["contribution"], reverse=True)
        _emit(rows[: args.limit], args.output)
    elif args.what == "weapon-meta":
        _emit(meta_mod.weapon_meta(db), args.output)
    elif args.what == "fragging-meta":
        _emit(meta_mod.fragging_meta(db), args.output)
    elif args.what == "map-meta":
        _emit(meta_mod.map_meta(db), args.output)
    elif args.what == "timeline":
        rows = [r.as_dict() for r in contrib.player_timeline(db, args.player_id)]
        _emit(rows, args.output)
    db.close()
    return 0


def cmd_export(args) -> int:
    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db
    db = Database(settings.db_path)
    rows = db.query(f"SELECT * FROM {args.table}")
    dicts = [dict(r) for r in rows]
    _write_csv(dicts, args.output)
    print(f"Wrote {len(dicts)} rows from {args.table} to {args.output}")
    db.close()
    return 0


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _emit(rows: list, output: Optional[str]) -> None:
    if output and output.endswith(".csv"):
        _write_csv(rows, output)
        print(f"Wrote {len(rows)} rows to {output}")
    elif output:
        with open(output, "w") as fh:
            json.dump(rows, fh, indent=2, default=str)
        print(f"Wrote {len(rows)} rows to {output}")
    else:
        print(json.dumps(rows, indent=2, default=str))


def _write_csv(rows: list, path: str) -> None:
    if not rows:
        open(path, "w").close()
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hltv_scraper",
        description="Scrape and analyse professional CS:GO/CS2 stats from HLTV.",
    )
    p.add_argument("--db", help="override database path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_scrape(sp):
        sp.add_argument("--start", help="YYYY-MM-DD (default: CS:GO release)")
        sp.add_argument("--end", help="YYYY-MM-DD (default: today)")
        sp.add_argument("--backend", choices=["auto", "curl_cffi",
                                              "cloudscraper", "requests"])

    sp = sub.add_parser("init-db", help="create the SQLite schema")
    sp.set_defaults(func=cmd_init_db)

    sp = sub.add_parser("rankings", help="scrape weekly ranking + roster history")
    add_common_scrape(sp)
    sp.add_argument("--step-days", type=int, default=28,
                    help="days between ranking snapshots (default 28 = monthly)")
    sp.set_defaults(func=cmd_rankings)

    sp = sub.add_parser("players", help="scrape per-player, per-period stats")
    add_common_scrape(sp)
    sp.add_argument("--ids", help="comma-separated player ids (default: discovered)")
    sp.add_argument("--period-months", type=int, default=6)
    sp.add_argument("--no-individual", action="store_true",
                    help="skip the entries/multikill/clutch sub-page")
    sp.set_defaults(func=cmd_players)

    sp = sub.add_parser("matches", help="scrape map scoreboards + head-to-head")
    add_common_scrape(sp)
    sp.add_argument("--max-pages", type=int, default=50)
    sp.add_argument("--no-scoreboards", action="store_true")
    sp.set_defaults(func=cmd_matches)

    sp = sub.add_parser("events", help="scrape the events archive")
    add_common_scrape(sp)
    sp.add_argument("--max-pages", type=int, default=20)
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("full", help="run rankings -> players -> matches -> events")
    add_common_scrape(sp)
    sp.add_argument("--step-days", type=int, default=28)
    sp.add_argument("--period-months", type=int, default=6)
    sp.add_argument("--max-pages", type=int, default=50)
    sp.set_defaults(func=cmd_full)

    sp = sub.add_parser("status", help="show row counts per table")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("analyze", help="run an analysis over the datastore")
    sp.add_argument("what", choices=["contribution", "weapon-meta",
                                     "fragging-meta", "map-meta", "timeline"])
    sp.add_argument("--player-id", type=int, help="for `timeline`")
    sp.add_argument("--min-maps", type=int, default=0)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("-o", "--output", help="write to .csv or .json (else stdout)")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("export", help="dump a table to CSV")
    sp.add_argument("table")
    sp.add_argument("output")
    sp.set_defaults(func=cmd_export)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
