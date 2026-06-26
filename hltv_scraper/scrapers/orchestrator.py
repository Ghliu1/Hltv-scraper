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
    def _fetch(self, url: str, kind: str, *, skip_if_done: bool = True,
               use_cache: bool = True) -> Optional[str]:
        if skip_if_done and self.db.is_scraped(url):
            log.debug("skip (already scraped): %s", url)
            return None
        try:
            html = self.client.get(url, use_cache=use_cache)
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

    def scrape_player_single_period(self, player_id: int, nick: str,
                                    ps: date, pe: date,
                                    ranking_filter: Optional[str] = None,
                                    fetch_individual: bool = True) -> int:
        """Scrape one (player, period) cell. Returns 1 if a record was saved."""
        ranking_filter = ranking_filter or self.settings.ranking_filter
        slug = slugify(nick)
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
            return 0
        try:
            rec = p_player.parse_player_stats(
                player_id, ps, pe,
                overview_html=overview, individual_html=individual,
                ranking_filter=ranking_filter,
            )
        except Exception as exc:
            log.warning("parse player %s %s-%s failed: %s",
                        player_id, ps, pe, exc)
            return 0
        with self.db.transaction():
            self.db.save(rec)
        if overview is not None:
            self.db.mark_scraped(ov_url, "player_overview", "ok")
        return 1

    def scrape_player_periods(self, player_id: int, nick: str,
                              start: date, end: date,
                              period_months: int = 6,
                              ranking_filter: Optional[str] = None,
                              fetch_individual: bool = True) -> int:
        n = 0
        for ps, pe in iter_periods(start, end, period_months):
            n += self.scrape_player_single_period(
                player_id, nick, ps, pe, ranking_filter, fetch_individual)
        return n

    def scrape_players_interleaved(self, start: date, end: date,
                                   ranking_filter: Optional[str] = None,
                                   period_months: int = 6,
                                   max_pages_per_period: int = 40,
                                   min_map_count: int = 0,
                                   fetch_individual: bool = True) -> int:
        """Discover and scrape players period-by-period.

        For each time window we page the stats index to learn exactly which
        players were active *in that window*, then scrape only those
        (player, period) cells. This avoids the huge waste of the naive
        "every player × every period" cross-product, where most cells are
        blank (the player hadn't debuted yet / had retired). Same dataset,
        a fraction of the requests.
        """
        ranking_filter = ranking_filter or self.settings.ranking_filter
        total = 0
        for ps, pe in iter_periods(start, end, period_months):
            active: dict[int, str] = {}
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
                    active.setdefault(pl.id, pl.nick)
                if len(players) < 30:  # short page => end of this period
                    break
            for pid, nick in active.items():
                total += self.scrape_player_single_period(
                    pid, nick, ps, pe, ranking_filter, fetch_individual)
            log.info("period %s..%s: %d active players (%d records total)",
                     ps, pe, len(active), total)
        return total

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
        # Default (stats) path: discover + scrape interleaved per period, so we
        # only ever fetch (player, period) cells that actually hold data.
        if player_ids is None and discover != "rankings":
            return self.scrape_players_interleaved(
                start, end, ranking_filter, period_months,
                min_map_count=min_map_count, fetch_individual=fetch_individual,
            )
        # Explicit ids or the rankings-roster pool: scrape each over the range.
        if player_ids is not None:
            nick_by_id = dict(self.discover_player_ids())
            targets = [(pid, nick_by_id.get(pid, str(pid))) for pid in player_ids]
        else:
            targets = self.discover_player_ids()
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

    def top_teams(self, top_n: int = 100) -> List[tuple[int, str]]:
        """(team_id, name) for every team ever ranked in the top-N.

        This is the team set the match crawl is driven from. It comes straight
        from the ranking snapshots already scraped — no extra fetching.
        """
        rows = self.db.query(
            """
            SELECT tr.team_id, COALESCE(t.name, '') AS name
            FROM (SELECT DISTINCT team_id FROM team_rankings WHERE rank <= ?) tr
            LEFT JOIN teams t ON t.id = tr.team_id
            ORDER BY tr.team_id
            """,
            (top_n,),
        )
        return [(r["team_id"], r["name"]) for r in rows]

    def top_teams_for_year(self, year: int, top_n: int = 100
                           ) -> List[tuple[int, str]]:
        """(team_id, name) for teams ranked in the top-N during a given year."""
        rows = self.db.query(
            """
            SELECT DISTINCT tr.team_id, COALESCE(t.name, '') AS name
            FROM team_rankings tr
            LEFT JOIN teams t ON t.id = tr.team_id
            WHERE tr.rank <= ? AND substr(tr.snapshot_date, 1, 4) = ?
            ORDER BY tr.team_id
            """,
            (top_n, str(year)),
        )
        return [(r["team_id"], r["name"]) for r in rows]

    def _fallback_year(self, top_n: int) -> Optional[int]:
        """Earliest year whose top-N set is well-populated (>= top_n teams).

        Used as the team set for pre-ranking years (CS:GO 2012 -> Sep-2015).
        The first ranked year (late 2015) has only a handful of snapshots, so we
        skip it in favour of the first full year (2016) as a better proxy for
        the early scene.
        """
        rows = self.db.query(
            "SELECT substr(snapshot_date,1,4) AS y, COUNT(DISTINCT team_id) AS c "
            "FROM team_rankings WHERE rank <= ? GROUP BY y ORDER BY y",
            (top_n,))
        if not rows:
            return None
        for r in rows:
            if r["c"] >= top_n:
                return int(r["y"])
        return int(rows[0]["y"])

    def _scrape_team_window(self, team_id: int, name: str, ws: date, we: date,
                            max_pages_per_team: int, with_scoreboards: bool,
                            ranking_filter: Optional[str]) -> int:
        """Page one team's maps within [ws, we], scraping each scoreboard."""
        slug = slugify(name) if name else "x"
        team_seen: set[int] = set()
        n = 0
        for page in range(max_pages_per_team):
            url = urls.team_matches(team_id, slug, ws, we, ranking_filter,
                                    offset=page * 50, base=self.settings.base_url)
            html = self._fetch(url, "team_matches", skip_if_done=False)
            if html is None:
                break
            try:
                rows = p_matches.parse_matches_list(html)
            except Exception as exc:
                log.warning("parse team matches failed %s: %s", url, exc)
                break
            if not rows:
                break
            fresh = [r for r in rows if r["mapstats_id"] not in team_seen]
            # The endpoint can return the whole window on one page; if a page
            # adds no new map ids, paging further is pointless.
            if not fresh:
                break
            for row in fresh:
                team_seen.add(row["mapstats_id"])
                self._persist_match_row(row)
                if with_scoreboards and row["mapstats_id"] is not None:
                    self.scrape_map_scoreboard(row["mapstats_id"],
                                               row.get("map_name") or "x")
                n += 1
            if len(rows) < 50:
                break
        return n

    def scrape_matches_by_team(self, start: Optional[date] = None,
                               end: Optional[date] = None,
                               top_n: int = 100,
                               max_pages_per_team: int = 40,
                               with_scoreboards: bool = True,
                               ranking_filter: Optional[str] = None) -> int:
        """Crawl matches year-by-year for each year's top-N teams.

        For every calendar year in [start, end] we take *that year's* top-N
        teams (from the ranking snapshots) and scrape only *that year's* maps
        for them — so a team contributes the years it was actually top-N, not
        its whole history. Years before HLTV's ranking existed (pre Sep-2015)
        fall back to the earliest ranked year's team set as a proxy. Maps shared
        by two top teams are fetched once (scrape log + HTML cache dedupe).
        rankingFilter is intentionally omitted: we want all of a top team's
        maps, not only those vs other top teams.
        """
        start = start or self.settings.start_date
        end = end or date.today()
        fallback_year = self._fallback_year(top_n)
        seen_maps = 0
        for year in range(start.year, end.year + 1):
            teams = self.top_teams_for_year(year, top_n)
            note = ""
            if not teams and fallback_year:
                teams = self.top_teams_for_year(fallback_year, top_n)
                note = f" (pre-ranking; using {fallback_year} top-{top_n} set)"
            ws = max(date(year, 1, 1), start)
            we = min(date(year, 12, 31), end)
            log.info("year %d: %d top-%d teams%s", year, len(teams), top_n, note)
            for idx, (team_id, name) in enumerate(teams, 1):
                seen_maps += self._scrape_team_window(
                    team_id, name, ws, we, max_pages_per_team,
                    with_scoreboards, ranking_filter)
                log.info("  year %d: team %d/%d (id=%s) -> %d maps total",
                         year, idx, len(teams), team_id, seen_maps)
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
        ok = self._scrape_map_overview(mapstats_id, slug)
        if self.settings.scrape_performance:
            self._scrape_map_performance(mapstats_id, slug)
        return ok

    def _scrape_map_overview(self, mapstats_id: int, slug: str = "x") -> bool:
        url = urls.map_stats(mapstats_id, slug, self.settings.base_url)
        html = self._fetch(url, "map_stats")
        if html is None:
            return False
        try:
            mapstat, perfs = p_matches.parse_map_stats(html, mapstats_id)
            sides = p_matches.parse_map_sides(html, mapstats_id)
        except Exception as exc:
            log.warning("parse map stats failed %s: %s", url, exc)
            self.db.mark_scraped(url, "map_stats", "error")
            return False
        with self.db.transaction():
            self.db.save(mapstat)
            self.db.save_all(perfs)
            self.db.save_all(sides)
            self._derive_head_to_head(mapstats_id, perfs)
            self._assign_opponent_ranks(mapstat)
        self.db.mark_scraped(url, "map_stats", "ok")
        return True

    def _assign_opponent_ranks(self, mapstat: models.MapStat) -> None:
        """Stamp each player's line with their team's and the opponent's world
        rank at the map's date (so stats can be split vs Top5/10/20/rest)."""
        t1, t2 = mapstat.team1_id, mapstat.team2_id
        d = mapstat.match_date
        if d is None or (t1 is None and t2 is None):
            return
        r1 = self.db.team_rank_at(t1, d)
        r2 = self.db.team_rank_at(t2, d)
        self.db.set_map_ranks(mapstat.id, t1, r1, r2)
        self.db.set_map_ranks(mapstat.id, t2, r2, r1)

    def _scrape_map_performance(self, mapstats_id: int, slug: str = "x") -> bool:
        """Fetch the map's kill matrix: real H2H + per-map opening & AWP kills.

        Tracked under its own scrape-log key so it backfills maps whose
        scoreboard was already done. Not cached on disk — these pages are ~4-5 MB
        of mostly chrome (the matrix itself is tiny), which would balloon the
        cache across tens of thousands of maps.
        """
        url = urls.map_performance(mapstats_id, slug, self.settings.base_url)
        html = self._fetch(url, "map_performance", use_cache=False)
        if html is None:
            return False
        try:
            duels = p_matches.parse_map_performance(html, mapstats_id)
        except Exception as exc:
            log.warning("parse map performance failed %s: %s", url, exc)
            self.db.mark_scraped(url, "map_performance", "error")
            return False
        # AWP kills/deaths per player (row/column sums of the AWP matrix).
        # Opening kills/deaths come from the scoreboard, not here.
        awp: dict[int, list[int]] = {}
        for d in duels:
            awp.setdefault(d.killer_id, [0, 0])
            awp.setdefault(d.victim_id, [0, 0])
            awp[d.killer_id][0] += d.awp_kills     # AWP kills
            awp[d.victim_id][1] += d.awp_kills      # AWP deaths
        with self.db.transaction():
            self.db.save_all(duels)
            for pid, (ak_, ad_) in awp.items():
                self.db.update_map_awp(mapstats_id, pid, ak_, ad_)
        self.db.mark_scraped(url, "map_performance", "ok")
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
