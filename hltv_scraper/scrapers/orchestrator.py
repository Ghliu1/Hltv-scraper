"""The Orchestrator ties together fetching, parsing and persistence.

It exposes high-level operations the CLI calls:

    scrape_rankings(start, end, step_days)   build ranking + roster history
    scrape_players(period_months, ...)       per-player, per-period stat profiles
    scrape_matches(start, end, max_pages)    map-stats scoreboards + duels
    discover_player_ids()                    players seen across roster snapshots

Each operation is incremental and resumable thanks to the scrape log, and each
is wrapped in defensive error handling so one bad page never aborts a long run.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Iterator, List, Optional, Sequence

from .. import urls, models
from ..config import Settings
from ..db import Database
from ..http_client import HltvClient, FetchError
from ..parsers import rankings as p_rankings
from ..parsers import player_stats as p_player
from ..parsers import matches as p_matches
from ..parsers import events as p_events
from ..utils import iter_periods, slugify

log = logging.getLogger("hltv")


class Orchestrator:
    def __init__(self, settings: Optional[Settings] = None,
                 client: Optional[HltvClient] = None,
                 db: Optional[Database] = None):
        self.settings = settings or Settings.from_env()
        self.settings.ensure_dirs()
        self.client = client or HltvClient(self.settings)
        self.db = db or Database(self.settings.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _fetch(self, url: str, kind: str, *, skip_if_done: bool = True
               ) -> Optional[str]:
        if skip_if_done and self.db.is_scraped(url):
            log.debug("skip (already scraped): %s", url)
            return None
        try:
            html = self.client.get(url)
        except FetchError as exc:
            log.warning("fetch failed: %s (%s)", url, exc)
            self.db.mark_scraped(url, kind, status="error")
            return None
        return html

    # ------------------------------------------------------------------
    # Rankings + rosters
    # ------------------------------------------------------------------
    def ranking_snapshot_dates(self, start: date, end: date,
                               step_days: int = 7) -> Iterator[date]:
        """Mondays (HLTV ranking cadence) between start and end."""
        # Align start to a Monday.
        d = start - timedelta(days=start.weekday())
        while d <= end:
            yield d
            d += timedelta(days=step_days)

    def scrape_rankings(self, start: Optional[date] = None,
                        end: Optional[date] = None, step_days: int = 7) -> int:
        start = start or self.settings.start_date
        end = end or date.today()
        n_snapshots = 0
        for snap in self.ranking_snapshot_dates(start, end, step_days):
            url = urls.ranking(snap, self.settings.base_url)
            html = self._fetch(url, "ranking")
            if html is None:
                continue
            try:
                rk, rosters, players, teams = p_rankings.parse_ranking(html, snap)
            except Exception as exc:  # parser robustness
                log.warning("parse ranking failed %s: %s", url, exc)
                self.db.mark_scraped(url, "ranking", "error")
                continue
            with self.db.transaction():
                self.db.save_all(teams)
                self.db.save_all(players)
                self.db.save_all(rk)
                self.db.save_all(rosters)
            self.db.mark_scraped(url, "ranking", "ok")
            n_snapshots += 1
            log.info("ranking %s: %d teams", snap, len(rk))
        return n_snapshots

    # ------------------------------------------------------------------
    # Player discovery + per-period stats
    # ------------------------------------------------------------------
    def discover_player_ids(self, top_n: Optional[int] = None
                            ) -> List[tuple[int, str]]:
        """(player_id, nick) for everyone who appeared on a top-N roster."""
        top_n = top_n or self.settings.top_n_teams
        rows = self.db.query(
            """
            SELECT DISTINCT rm.player_id, rm.player_nick
            FROM roster_memberships rm
            JOIN team_rankings tr
              ON tr.snapshot_date = rm.snapshot_date
             AND tr.team_id = rm.team_id
            WHERE tr.rank <= ?
            ORDER BY rm.player_nick
            """,
            (top_n,),
        )
        return [(r["player_id"], r["player_nick"]) for r in rows]

    def discover_players_from_stats(self, start: date, end: date,
                                    ranking_filter: Optional[str] = None,
                                    period_months: int = 6,
                                    max_pages_per_period: int = 40,
                                    min_map_count: int = 0
                                    ) -> List[tuple[int, str]]:
        """Discover players via the stats players index, per time window.

        This is the path to broad ("top ~100", tier 1-3) coverage from 2012:
        for each period it pages through ``/stats/players`` filtered to top
        teams, persisting every player it finds. Returns the unique players.
        """
        ranking_filter = ranking_filter or self.settings.ranking_filter
        found: dict[int, str] = {}
        for ps, pe in iter_periods(start, end, period_months):
            for page in range(max_pages_per_period):
                url = urls.players_index(ps, pe, ranking_filter,
                                         offset=page * 50,
                                         min_map_count=min_map_count or None,
                                         base=self.settings.base_url)
                html = self._fetch(url, "players_index", skip_if_done=False)
                if html is None:
                    break
                try:
                    players = p_player.parse_players_index(html)
                except Exception as exc:
                    log.warning("parse players index failed %s: %s", url, exc)
                    break
                if not players:
                    break
                with self.db.transaction():
                    self.db.save_all(players)
                for pl in players:
                    found.setdefault(pl.id, pl.nick)
                # A short page means we've reached the end of this period.
                if len(players) < 30:
                    break
            log.info("discovered %d players through %s..%s", len(found), ps, pe)
        return list(found.items())

    def scrape_player_periods(self, player_id: int, nick: str,
                              start: date, end: date,
                              period_months: int = 6,
                              ranking_filter: Optional[str] = None,
                              fetch_individual: bool = True) -> int:
        ranking_filter = ranking_filter or self.settings.ranking_filter
        slug = slugify(nick)
        n = 0
        for ps, pe in iter_periods(start, end, period_months):
            ov_url = urls.player_overview(player_id, slug, ps, pe,
                                          ranking_filter, self.settings.base_url)
            overview = self._fetch(ov_url, "player_overview")
            individual = None
            if fetch_individual:
                iv_url = urls.player_individual(player_id, slug, ps, pe,
                                                ranking_filter,
                                                self.settings.base_url)
                individual = self._fetch(iv_url, "player_individual")

            if overview is None and individual is None:
                continue
            try:
                rec = p_player.parse_player_stats(
                    player_id, ps, pe,
                    overview_html=overview, individual_html=individual,
                    ranking_filter=ranking_filter,
                )
            except Exception as exc:
                log.warning("parse player %s %s-%s failed: %s",
                            player_id, ps, pe, exc)
                continue
            with self.db.transaction():
                self.db.save(rec)
            if overview is not None:
                self.db.mark_scraped(ov_url, "player_overview", "ok")
            n += 1
        return n

    def scrape_players(self, player_ids: Optional[Sequence[int]] = None,
                       start: Optional[date] = None, end: Optional[date] = None,
                       period_months: int = 6,
                       ranking_filter: Optional[str] = None,
                       fetch_individual: bool = True,
                       discover: str = "stats",
                       min_map_count: int = 0) -> int:
        """Scrape per-period stats for a set of players.

        ``discover`` controls how the player pool is chosen when ``player_ids``
        is not given:
          * ``stats``    — page the stats players index per period (broad,
            tier 1-3 / "top ~100", works from 2012). Recommended.
          * ``rankings`` — only players on top-N ranking rosters (top ~30,
            from 2015). Narrower but exact.
        """
        start = start or self.settings.start_date
        end = end or date.today()
        if player_ids is not None:
            nick_by_id = dict(self.discover_player_ids())
            targets = [(pid, nick_by_id.get(pid, str(pid))) for pid in player_ids]
        elif discover == "rankings":
            targets = self.discover_player_ids()
        else:
            targets = self.discover_players_from_stats(
                start, end, ranking_filter, period_months,
                min_map_count=min_map_count,
            )
        total = 0
        for pid, nick in targets:
            total += self.scrape_player_periods(
                pid, nick, start, end, period_months, ranking_filter,
                fetch_individual,
            )
        return total

    # ------------------------------------------------------------------
    # Matches + per-map scoreboards + head-to-head
    # ------------------------------------------------------------------
    def scrape_matches(self, start: Optional[date] = None,
                       end: Optional[date] = None, max_pages: int = 50,
                       ranking_filter: Optional[str] = None,
                       with_scoreboards: bool = True) -> int:
        start = start or self.settings.start_date
        end = end or date.today()
        ranking_filter = ranking_filter or self.settings.ranking_filter
        seen_maps = 0
        for page in range(max_pages):
            list_url = urls.matches_list(start, end, ranking_filter,
                                         offset=page * 50,
                                         base=self.settings.base_url)
            html = self._fetch(list_url, "matches_list", skip_if_done=False)
            if html is None:
                break
            try:
                rows = p_matches.parse_matches_list(html)
            except Exception as exc:
                log.warning("parse matches list failed %s: %s", list_url, exc)
                break
            if not rows:
                break
            for row in rows:
                self._persist_match_row(row)
                if with_scoreboards and row["mapstats_id"] is not None:
                    self.scrape_map_scoreboard(row["mapstats_id"],
                                               row.get("map_name") or "x")
                seen_maps += 1
        return seen_maps

    def _persist_match_row(self, row: dict) -> None:
        mapstat = models.MapStat(
            id=row["mapstats_id"],
            match_id=None,
            map_name=row.get("map_name"),
            match_date=row.get("date"),
            event_id=None,
            team1_id=row.get("team1_id"),
            team2_id=row.get("team2_id"),
            team1_score=row.get("team1_score"),
            team2_score=row.get("team2_score"),
        )
        with self.db.transaction():
            self.db.save(mapstat)

    def scrape_map_scoreboard(self, mapstats_id: int, slug: str = "x") -> bool:
        url = urls.map_stats(mapstats_id, slug, self.settings.base_url)
        html = self._fetch(url, "map_stats")
        if html is None:
            return False
        try:
            mapstat, perfs = p_matches.parse_map_stats(html, mapstats_id)
        except Exception as exc:
            log.warning("parse map stats failed %s: %s", url, exc)
            self.db.mark_scraped(url, "map_stats", "error")
            return False
        with self.db.transaction():
            self.db.save(mapstat)
            self.db.save_all(perfs)
            self._derive_head_to_head(mapstats_id, perfs)
        self.db.mark_scraped(url, "map_stats", "ok")
        return True

    def _derive_head_to_head(self, map_id: int,
                             perfs: Sequence[models.PlayerMapPerformance]) -> None:
        """Approximate H2H duels from a map scoreboard.

        Without the per-duel data (a separate richer endpoint), we attribute a
        player's map kills proportionally against the opposing five. This gives
        a usable, additive H2H matrix scoped to the map; summing across maps
        reconstructs player-vs-player tendencies over a period. The ``context``
        is the map id so the rows can be re-aggregated however the analyst wants.
        """
        by_team: dict[Optional[int], list[models.PlayerMapPerformance]] = {}
        for p in perfs:
            by_team.setdefault(p.team_id, []).append(p)
        if len(by_team) != 2:
            return
        teams = list(by_team.values())
        for a_side, b_side in ((teams[0], teams[1]), (teams[1], teams[0])):
            opp_count = len(b_side) or 1
            for a in a_side:
                if not a.kills:
                    continue
                share = a.kills / opp_count
                for b in b_side:
                    self.db.save(models.HeadToHeadDuel(
                        player_id=a.player_id,
                        opponent_id=b.player_id,
                        context=f"map:{map_id}",
                        kills=round(share),
                    ))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def scrape_events(self, max_pages: int = 20) -> int:
        n = 0
        for page in range(max_pages):
            url = urls.events_archive(offset=page * 50, base=self.settings.base_url)
            html = self._fetch(url, "events", skip_if_done=False)
            if html is None:
                break
            try:
                events = p_events.parse_events_archive(html)
            except Exception as exc:
                log.warning("parse events failed %s: %s", url, exc)
                break
            if not events:
                break
            with self.db.transaction():
                self.db.save_all(events)
            n += len(events)
        return n

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        self.db.close()
