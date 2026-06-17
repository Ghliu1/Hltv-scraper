"""SQLite persistence layer.

A single-file SQLite database keeps the whole dataset portable and queryable
with plain SQL (or pandas) for downstream modelling. All writes are idempotent
upserts keyed on natural keys, so re-running a scrape never duplicates rows and
can resume safely after interruption.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import models

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS teams (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    country TEXT
);

CREATE TABLE IF NOT EXISTS players (
    id        INTEGER PRIMARY KEY,
    nick      TEXT NOT NULL,
    real_name TEXT,
    country   TEXT
);

CREATE TABLE IF NOT EXISTS team_rankings (
    snapshot_date TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    team_id       INTEGER NOT NULL,
    team_name     TEXT,
    points        INTEGER,
    country       TEXT,
    PRIMARY KEY (snapshot_date, team_id)
);
CREATE INDEX IF NOT EXISTS idx_rank_date ON team_rankings(snapshot_date, rank);

CREATE TABLE IF NOT EXISTS roster_memberships (
    snapshot_date TEXT NOT NULL,
    team_id       INTEGER NOT NULL,
    player_id     INTEGER NOT NULL,
    player_nick   TEXT,
    PRIMARY KEY (snapshot_date, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS player_stat_periods (
    player_id      INTEGER NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    ranking_filter TEXT NOT NULL DEFAULT 'ALL',
    maps_played    INTEGER,
    rounds_played  INTEGER,
    rating         REAL,
    rating_version TEXT,
    kpr REAL, dpr REAL, apr REAL, kast REAL, impact REAL, adr REAL,
    kddiff INTEGER,
    total_kills INTEGER, total_deaths INTEGER, headshot_pct REAL, kd_ratio REAL,
    opening_kills INTEGER, opening_deaths INTEGER,
    opening_kpr REAL, opening_dpr REAL,
    opening_attempts_pct REAL, opening_success_pct REAL,
    opening_rating REAL, team_win_pct_after_first_kill REAL,
    rounds_with_kills_0 INTEGER, rounds_with_kills_1 INTEGER,
    rounds_with_kills_2 INTEGER, rounds_with_kills_3 INTEGER,
    rounds_with_kills_4 INTEGER, rounds_with_kills_5 INTEGER,
    clutches_1v1 INTEGER, clutches_1v2 INTEGER, clutches_1v3 INTEGER,
    clutches_1v4 INTEGER, clutches_1v5 INTEGER,
    saved_by_teammate_pr REAL, saved_teammates_pr REAL,
    utility_damage_pr REAL, flash_assists INTEGER, flashes_thrown INTEGER,
    enemies_flashed INTEGER, flashed_per_thrown REAL, grenade_dmg_pr REAL,
    PRIMARY KEY (player_id, period_start, period_end, ranking_filter)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    start_date TEXT,
    end_date   TEXT,
    prize_pool TEXT,
    location   TEXT,
    tier       TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY,
    match_date  TEXT,
    event_id    INTEGER,
    event_name  TEXT,
    team1_id    INTEGER, team2_id INTEGER,
    team1_name  TEXT, team2_name TEXT,
    team1_score INTEGER, team2_score INTEGER,
    best_of     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(match_date);

CREATE TABLE IF NOT EXISTS map_stats (
    id          INTEGER PRIMARY KEY,
    match_id    INTEGER,
    map_name    TEXT,
    match_date  TEXT,
    event_id    INTEGER,
    team1_id    INTEGER, team2_id INTEGER,
    team1_score INTEGER, team2_score INTEGER,
    team1_ct INTEGER, team1_t INTEGER, team2_ct INTEGER, team2_t INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mapstats_date ON map_stats(match_date);

CREATE TABLE IF NOT EXISTS player_map_performance (
    map_id       INTEGER NOT NULL,
    player_id    INTEGER NOT NULL,
    player_nick  TEXT,
    team_id      INTEGER,
    kills INTEGER, deaths INTEGER, assists INTEGER, kddiff INTEGER,
    adr REAL, kast REAL, rating REAL,
    first_kills INTEGER, first_deaths INTEGER, headshot_pct REAL,
    PRIMARY KEY (map_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pmp_player ON player_map_performance(player_id);

CREATE TABLE IF NOT EXISTS head_to_head (
    player_id   INTEGER NOT NULL,
    opponent_id INTEGER NOT NULL,
    context     TEXT NOT NULL,
    kills  INTEGER DEFAULT 0,
    deaths INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, opponent_id, context)
);

CREATE TABLE IF NOT EXISTS weapon_kills (
    player_id    INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    weapon       TEXT NOT NULL,
    kills        INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, period_start, period_end, weapon)
);

-- Tracks which (url) pages have been fetched+parsed, for resumable scrapes.
CREATE TABLE IF NOT EXISTS scrape_log (
    url        TEXT PRIMARY KEY,
    kind       TEXT,
    status     TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _upsert_sql(table: str, columns: Sequence[str], pk: Sequence[str]) -> str:
    cols = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in pk)
    conflict = ", ".join(pk)
    tail = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) {tail}"
    )


# Map each model table to its primary-key columns (for upsert conflict targets).
_PK = {
    "teams": ["id"],
    "players": ["id"],
    "team_rankings": ["snapshot_date", "team_id"],
    "roster_memberships": ["snapshot_date", "team_id", "player_id"],
    "player_stat_periods": ["player_id", "period_start", "period_end", "ranking_filter"],
    "events": ["id"],
    "matches": ["id"],
    "map_stats": ["id"],
    "player_map_performance": ["map_id", "player_id"],
    "head_to_head": ["player_id", "opponent_id", "context"],
    "weapon_kills": ["player_id", "period_start", "period_end", "weapon"],
}


class Database:
    """Thin wrapper over a sqlite3 connection with typed upsert helpers."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.commit()
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- generic upsert ----------------------------------------------------
    def upsert(self, table: str, row: dict) -> None:
        cols = list(row.keys())
        sql = _upsert_sql(table, cols, _PK[table])
        self.conn.execute(sql, row)

    def upsert_many(self, table: str, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        sql = _upsert_sql(table, cols, _PK[table])
        self.conn.executemany(sql, rows)
        return len(rows)

    # -- typed convenience save() -----------------------------------------
    _TABLE_BY_TYPE = {
        models.Team: "teams",
        models.Player: "players",
        models.TeamRanking: "team_rankings",
        models.RosterMembership: "roster_memberships",
        models.PlayerStatPeriod: "player_stat_periods",
        models.Event: "events",
        models.Match: "matches",
        models.MapStat: "map_stats",
        models.PlayerMapPerformance: "player_map_performance",
        models.HeadToHeadDuel: "head_to_head",
        models.WeaponKills: "weapon_kills",
    }

    def save(self, obj) -> None:
        table = self._TABLE_BY_TYPE[type(obj)]
        self.upsert(table, obj.to_row())

    def save_all(self, objs: Iterable) -> int:
        n = 0
        for obj in objs:
            self.save(obj)
            n += 1
        return n

    # -- scrape log --------------------------------------------------------
    def mark_scraped(self, url: str, kind: str, status: str = "ok") -> None:
        self.conn.execute(
            "INSERT INTO scrape_log(url, kind, status) VALUES(?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET status=excluded.status, "
            "fetched_at=CURRENT_TIMESTAMP",
            (url, kind, status),
        )
        self.conn.commit()

    def is_scraped(self, url: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM scrape_log WHERE url=? AND status='ok'", (url,)
        )
        return cur.fetchone() is not None

    # -- queries -----------------------------------------------------------
    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in _PK:
            try:
                out[table] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = 0
        return out
