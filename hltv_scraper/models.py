"""Typed dataclasses describing every entity the scraper persists.

These mirror the SQLite schema in :mod:`hltv_scraper.db`. Parsers return these
objects; the DB layer knows how to upsert them. Keeping the schema expressed
twice (here as types, there as DDL) is a deliberate trade for clarity — the
``to_row`` helpers keep them in sync.

Field coverage is intentionally broad: rating, fragging, KAST/ADR/impact,
opening duels (entries), multi-kills, clutches, and utility (flashes, utility
damage) — the full surface needed to model a player's contribution and to
study weapon/economy metas across time periods.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


# --------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------


@dataclass
class Team:
    id: int
    name: str
    country: Optional[str] = None

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class Player:
    id: int
    nick: str
    real_name: Optional[str] = None
    country: Optional[str] = None

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class TeamRanking:
    """One team's position in a weekly HLTV world ranking snapshot."""

    snapshot_date: date
    rank: int
    team_id: int
    team_name: str
    points: Optional[int] = None
    country: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["snapshot_date"] = self.snapshot_date.isoformat()
        return d


@dataclass
class RosterMembership:
    """A player's presence on a team's roster at a ranking snapshot."""

    snapshot_date: date
    team_id: int
    player_id: int
    player_nick: str

    def to_row(self) -> dict:
        d = asdict(self)
        d["snapshot_date"] = self.snapshot_date.isoformat()
        return d


# --------------------------------------------------------------------------
# Player statistics — time-sliced so profiles can be built per period.
# --------------------------------------------------------------------------


@dataclass
class PlayerStatPeriod:
    """Aggregated player stats over a (start_date, end_date) window.

    Combines the HLTV "overview", "individual" and utility numbers into one
    flat record keyed by player + period + filter. Every metric is optional so
    partial scrapes still persist cleanly.
    """

    player_id: int
    period_start: date
    period_end: date
    ranking_filter: str = "ALL"

    # Volume
    maps_played: Optional[int] = None
    rounds_played: Optional[int] = None

    # Core overview metrics
    rating: Optional[float] = None          # Rating 2.0/2.1
    rating_version: Optional[str] = None    # "2.0" | "2.1"
    kpr: Optional[float] = None             # kills per round
    dpr: Optional[float] = None             # deaths per round
    apr: Optional[float] = None             # assists per round
    kast: Optional[float] = None            # fraction in [0,1]
    impact: Optional[float] = None
    adr: Optional[float] = None             # average damage per round
    kddiff: Optional[int] = None

    # Fragging totals
    total_kills: Optional[int] = None
    total_deaths: Optional[int] = None
    headshot_pct: Optional[float] = None    # fraction in [0,1]
    kd_ratio: Optional[float] = None

    # Opening duels / entries
    opening_kills: Optional[int] = None
    opening_deaths: Optional[int] = None
    opening_kpr: Optional[float] = None
    opening_dpr: Optional[float] = None
    opening_attempts_pct: Optional[float] = None   # rounds with an opening duel
    opening_success_pct: Optional[float] = None    # win% of opening duels
    opening_rating: Optional[float] = None
    team_win_pct_after_first_kill: Optional[float] = None

    # Multi-kill rounds
    rounds_with_kills_0: Optional[int] = None
    rounds_with_kills_1: Optional[int] = None
    rounds_with_kills_2: Optional[int] = None
    rounds_with_kills_3: Optional[int] = None
    rounds_with_kills_4: Optional[int] = None
    rounds_with_kills_5: Optional[int] = None

    # Clutches won (1vX)
    clutches_1v1: Optional[int] = None
    clutches_1v2: Optional[int] = None
    clutches_1v3: Optional[int] = None
    clutches_1v4: Optional[int] = None
    clutches_1v5: Optional[int] = None

    # Utility
    saved_by_teammate_pr: Optional[float] = None
    saved_teammates_pr: Optional[float] = None
    utility_damage_pr: Optional[float] = None
    flash_assists: Optional[int] = None
    flashes_thrown: Optional[int] = None
    enemies_flashed: Optional[int] = None
    flashed_per_thrown: Optional[float] = None
    grenade_dmg_pr: Optional[float] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d


# --------------------------------------------------------------------------
# Matches & per-map scoreboards (the granular source for everything else)
# --------------------------------------------------------------------------


@dataclass
class Event:
    id: int
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    prize_pool: Optional[str] = None
    location: Optional[str] = None
    tier: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat() if self.start_date else None
        d["end_date"] = self.end_date.isoformat() if self.end_date else None
        return d


@dataclass
class Match:
    id: int
    match_date: Optional[date]
    event_id: Optional[int]
    event_name: Optional[str]
    team1_id: Optional[int]
    team2_id: Optional[int]
    team1_name: Optional[str]
    team2_name: Optional[str]
    team1_score: Optional[int] = None    # maps won (bo3 etc.)
    team2_score: Optional[int] = None
    best_of: Optional[int] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["match_date"] = self.match_date.isoformat() if self.match_date else None
        return d


@dataclass
class MapStat:
    """A single played map within a match (one row of HLTV map stats)."""

    id: int                  # HLTV mapstatsid
    match_id: Optional[int]
    map_name: Optional[str]
    match_date: Optional[date]
    event_id: Optional[int]
    team1_id: Optional[int]
    team2_id: Optional[int]
    team1_score: Optional[int] = None    # rounds won
    team2_score: Optional[int] = None
    team1_ct: Optional[int] = None
    team1_t: Optional[int] = None
    team2_ct: Optional[int] = None
    team2_t: Optional[int] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["match_date"] = self.match_date.isoformat() if self.match_date else None
        return d


@dataclass
class PlayerMapPerformance:
    """One player's line on a single map's scoreboard."""

    map_id: int
    player_id: int
    player_nick: str
    team_id: Optional[int] = None
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None
    kddiff: Optional[int] = None
    adr: Optional[float] = None
    kast: Optional[float] = None
    rating: Optional[float] = None
    first_kills: Optional[int] = None
    first_deaths: Optional[int] = None
    headshot_pct: Optional[float] = None

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class HeadToHeadDuel:
    """Aggregated kills of one player vs another, scoped to a context.

    ``context`` is e.g. a map id, match id or 'overall' so the same pair can be
    tracked over time. Symmetric pairs are stored as two directed rows.
    """

    player_id: int
    opponent_id: int
    context: str
    kills: int = 0
    deaths: int = 0

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class MapDuel:
    """Directed per-map kill counts from HLTV's kill matrix.

    For an ordered pair (killer -> victim) on one map: total kills, opening
    (first) kills, and AWP kills. Summing a killer's rows yields that player's
    per-map kills / opening kills / AWP kills; the directed form preserves the
    full head-to-head matrix (the real version of :class:`HeadToHeadDuel`,
    which elsewhere is only approximated from scoreboard totals).
    """

    map_id: int
    killer_id: int
    victim_id: int
    kills: int = 0
    first_kills: int = 0
    awp_kills: int = 0

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class WeaponKills:
    """Per-player weapon usage, the basis for weapon-meta analysis."""

    player_id: int
    period_start: date
    period_end: date
    weapon: str
    kills: int = 0

    def to_row(self) -> dict:
        d = asdict(self)
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d
