"""Assemble a complete player profile for a time window.

The stated goal includes "create profiles for certain players during periods"
and study "head to head duels". This module stitches the scraped datastore into
one coherent profile object:

    identity        nick / real name / country
    team_history    which teams the player was on, by ranking snapshot
    stat_periods    the per-period statistical record (rating, entries, ...)
    contribution    the explainable contribution timeline (facet breakdown)
    head_to_head    top opponents by duel volume + net duel differential
    role_signals    derived role indicators (entry/awp/support/anchor leanings)

Everything reads from SQLite, so a profile is reproducible from the database
alone, whether populated by a live scrape or by fixtures in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..db import Database
from . import contribution as contrib


@dataclass
class HeadToHead:
    opponent_id: int
    opponent_nick: Optional[str]
    kills: int
    deaths: int

    @property
    def diff(self) -> int:
        return self.kills - self.deaths

    def as_dict(self) -> dict:
        return {
            "opponent_id": self.opponent_id,
            "opponent_nick": self.opponent_nick,
            "kills": self.kills,
            "deaths": self.deaths,
            "diff": self.diff,
        }


@dataclass
class PlayerProfile:
    player_id: int
    nick: Optional[str]
    real_name: Optional[str]
    country: Optional[str]
    period_start: Optional[str]
    period_end: Optional[str]
    team_history: List[dict] = field(default_factory=list)
    stat_periods: List[dict] = field(default_factory=list)
    contribution: List[dict] = field(default_factory=list)
    head_to_head: List[dict] = field(default_factory=list)
    role_signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "nick": self.nick,
            "real_name": self.real_name,
            "country": self.country,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "role_signals": self.role_signals,
            "team_history": self.team_history,
            "contribution": self.contribution,
            "head_to_head": self.head_to_head,
            "stat_periods": self.stat_periods,
        }


def top_head_to_head(db: Database, player_id: int, limit: int = 15
                     ) -> List[HeadToHead]:
    """Aggregate the per-map H2H rows into the player's biggest rivalries.

    Sums kills *for* and *against* across every stored context, joins the
    opponent's nick, and orders by total duel volume.
    """
    rows = db.query(
        """
        SELECT opp AS opponent_id,
               SUM(kills_for)   AS kills,
               SUM(kills_against) AS deaths
        FROM (
            SELECT opponent_id AS opp, kills AS kills_for, 0 AS kills_against
            FROM head_to_head WHERE player_id = :pid
            UNION ALL
            SELECT player_id AS opp, 0 AS kills_for, kills AS kills_against
            FROM head_to_head WHERE opponent_id = :pid
        )
        WHERE opp != :pid
        GROUP BY opp
        ORDER BY (SUM(kills_for) + SUM(kills_against)) DESC
        LIMIT :lim
        """,
        {"pid": player_id, "lim": limit},
    )
    out: List[HeadToHead] = []
    for r in rows:
        nick_row = db.query("SELECT nick FROM players WHERE id = ?",
                            (r["opponent_id"],))
        nick = nick_row[0]["nick"] if nick_row else None
        out.append(HeadToHead(
            opponent_id=r["opponent_id"],
            opponent_nick=nick,
            kills=int(r["kills"] or 0),
            deaths=int(r["deaths"] or 0),
        ))
    return out


def _role_signals(stat_rows: List[dict]) -> dict:
    """Infer rough role leanings from aggregated stats over the window.

    These are *indicators*, not labels — averaged across the period's records:
      * entry leaning  — high opening-kill volume + opening rating
      * awp/impact     — high impact relative to KPR
      * support        — high utility damage / flash assists, lower opening
      * anchor/clutch  — clutch volume per map, lower death rate
    """
    if not stat_rows:
        return {}

    def avg(key):
        vals = [r[key] for r in stat_rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    opening_kills = avg("opening_kills") or 0
    maps = avg("maps_played") or 0
    opening_per_map = (opening_kills / maps) if maps else 0
    # ~2.5 opening kills/map is a typical entry-fragger workload; index ~1.0
    # means "carries an average entry role", >1 leans dedicated entry/opener.
    return {
        "entry_index": round(opening_per_map / 2.5, 3) if maps else None,
        "impact_index": round((avg("impact") or 0) / max(avg("kpr") or 1e-9, 1e-9), 3),
        "utility_index": round((avg("utility_damage_pr") or 0) / 5.0, 3),
        "consistency_kast": round(avg("kast"), 3) if avg("kast") else None,
        "avg_rating": round(avg("rating"), 3) if avg("rating") else None,
    }


def build_profile(db: Database, player_id: int,
                  period_start: Optional[str] = None,
                  period_end: Optional[str] = None,
                  h2h_limit: int = 15) -> PlayerProfile:
    ident = db.query("SELECT * FROM players WHERE id = ?", (player_id,))
    ident = ident[0] if ident else None

    # Time-window filter on stored stat periods (string ISO dates sort lexically).
    sql = "SELECT * FROM player_stat_periods WHERE player_id = ?"
    params: list = [player_id]
    if period_start:
        sql += " AND period_end >= ?"
        params.append(period_start)
    if period_end:
        sql += " AND period_start <= ?"
        params.append(period_end)
    sql += " ORDER BY period_start"
    stat_rows = [dict(r) for r in db.query(sql, params)]

    contributions = [
        contrib.score_period(r).as_dict()
        for r in db.query(sql, params)
    ]

    team_history = [dict(r) for r in db.query(
        """
        SELECT rm.snapshot_date, rm.team_id, t.name AS team_name
        FROM roster_memberships rm
        LEFT JOIN teams t ON t.id = rm.team_id
        WHERE rm.player_id = ?
        ORDER BY rm.snapshot_date
        """,
        (player_id,),
    )]

    h2h = [h.as_dict() for h in top_head_to_head(db, player_id, h2h_limit)]

    return PlayerProfile(
        player_id=player_id,
        nick=ident["nick"] if ident else None,
        real_name=ident["real_name"] if ident else None,
        country=ident["country"] if ident else None,
        period_start=period_start,
        period_end=period_end,
        team_history=team_history,
        stat_periods=stat_rows,
        contribution=contributions,
        head_to_head=h2h,
        role_signals=_role_signals(stat_rows),
    )
