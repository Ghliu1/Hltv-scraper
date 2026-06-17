"""Meta trend analysis across time periods.

"Take into account the metas of the time period" — this module aggregates the
datastore along the time axis to surface how the game changed:

* weapon meta  — kill share per weapon per period (from ``weapon_kills``)
* fragging meta — league-wide averages of rating components per period
* map meta      — which maps were played most in each period

All functions return plain dict rows so they're trivial to dump to CSV/JSON or
load into pandas for plotting.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..db import Database


def weapon_meta(db: Database) -> List[dict]:
    """Kill share per weapon per period, ordered by period then share."""
    rows = db.query(
        """
        SELECT period_start, period_end, weapon, SUM(kills) AS kills
        FROM weapon_kills
        GROUP BY period_start, period_end, weapon
        ORDER BY period_start, kills DESC
        """
    )
    # Compute share within each period.
    totals: Dict[tuple, int] = {}
    for r in rows:
        key = (r["period_start"], r["period_end"])
        totals[key] = totals.get(key, 0) + (r["kills"] or 0)
    out = []
    for r in rows:
        key = (r["period_start"], r["period_end"])
        total = totals[key] or 1
        out.append({
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "weapon": r["weapon"],
            "kills": r["kills"],
            "share": round((r["kills"] or 0) / total, 4),
        })
    return out


def fragging_meta(db: Database, ranking_filter: Optional[str] = None
                  ) -> List[dict]:
    """League-wide average rating components per period.

    Establishes the baseline a period should be normalised against — e.g. if
    ADR inflates across CS2, contribution comparisons can be era-adjusted.
    """
    sql = """
        SELECT period_start, period_end,
               COUNT(*) AS players,
               AVG(rating) AS avg_rating,
               AVG(adr)    AS avg_adr,
               AVG(kast)   AS avg_kast,
               AVG(kpr)    AS avg_kpr,
               AVG(opening_kills) AS avg_opening_kills
        FROM player_stat_periods
        WHERE rating IS NOT NULL
    """
    params: list = []
    if ranking_filter:
        sql += " AND ranking_filter = ?"
        params.append(ranking_filter)
    sql += " GROUP BY period_start, period_end ORDER BY period_start"
    rows = db.query(sql, params)
    return [dict(r) for r in rows]


def map_meta(db: Database) -> List[dict]:
    """How often each map was played, by year (from map_stats.match_date)."""
    rows = db.query(
        """
        SELECT substr(match_date, 1, 4) AS year, map_name,
               COUNT(*) AS times_played
        FROM map_stats
        WHERE map_name IS NOT NULL AND match_date IS NOT NULL
        GROUP BY year, map_name
        ORDER BY year, times_played DESC
        """
    )
    return [dict(r) for r in rows]


def era_adjusted_baseline(db: Database) -> Dict[str, dict]:
    """Return per-period league baselines keyed by 'start..end' for reuse."""
    out = {}
    for r in fragging_meta(db):
        key = f"{r['period_start']}..{r['period_end']}"
        out[key] = r
    return out
