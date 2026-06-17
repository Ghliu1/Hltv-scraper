"""Parse HLTV match results listings and per-map scoreboards.

Two page types:

* the stats *matches* listing (``/stats/matches?...``) — a paginated table of
  played maps, each linking to a map-stats page;
* a single map-stats page (``/stats/matches/mapstatsid/<id>/<slug>``) — the
  full per-player scoreboard (K/A/D, +/-, ADR, KAST, rating) for both teams,
  which is the granular source we aggregate into head-to-head and weapon data.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .. import models, utils
from . import common


def _stats_team_id(href: str) -> Optional[int]:
    """Both ``/team/<id>/`` and ``/stats/teams/<id>/`` share the team id."""
    m = re.search(r"/teams?/(\d+)/", href)
    return int(m.group(1)) if m else None


def parse_matches_list(html: str) -> List[dict]:
    """Return lightweight dicts describing each played map in a results table.

    Each dict has ``mapstats_id``, ``match_id`` (if linked), ``map_name``,
    ``date``, team names/ids and scores — enough to enqueue map-stats pages and
    to populate the ``map_stats`` table.
    """
    sp = common.soup(html)
    out: List[dict] = []
    table = sp.select_one(".stats-table, table.stats-table")
    if table is None:
        return out

    for tr in table.select("tbody tr"):
        link = tr.select_one("a[href*='/mapstatsid/']")
        if link is None:
            continue
        rec: dict = {
            "mapstats_id": common.mapstats_id_from_href(link["href"]),
            "map_name": None,
            "date": None,
            "team1_name": None,
            "team2_name": None,
            "team1_id": None,
            "team2_id": None,
            "team1_score": None,
            "team2_score": None,
            "event_name": None,
        }

        date_el = tr.select_one(".date-col, .time")
        rec["date"] = utils.parse_date(date_el.get_text()) if date_el else None

        teams = tr.select("a[href^='/stats/teams/'], .team-col a")
        if len(teams) >= 2:
            rec["team1_name"] = teams[0].get_text(strip=True)
            rec["team2_name"] = teams[1].get_text(strip=True)

        map_el = tr.select_one(".map-pool, .dynamic-map-name-full, .statsMapLogo")
        if map_el is not None:
            rec["map_name"] = (map_el.get("title") or map_el.get_text(strip=True)
                               or None)

        result_el = tr.select_one(".match-score, .stats-rating, .result-score")
        if result_el is not None:
            nums = [utils.parse_int(s.get_text()) for s in result_el.find_all("span")]
            nums = [n for n in nums if n is not None]
            if len(nums) >= 2:
                rec["team1_score"], rec["team2_score"] = nums[0], nums[1]

        ev = tr.select_one("a[href^='/events/'], .event-col a")
        if ev is not None:
            rec["event_name"] = ev.get_text(strip=True)

        if rec["mapstats_id"] is not None:
            out.append(rec)
    return out


def parse_map_stats(html: str, mapstats_id: int) -> tuple[
    models.MapStat, List[models.PlayerMapPerformance]
]:
    """Parse one map-stats page into a MapStat + both teams' scoreboards."""
    sp = common.soup(html)

    # --- map / match header ----------------------------------------------
    map_name = None
    map_el = sp.select_one(".match-info-box .bold, .map-text, .mapname")
    if map_el:
        map_name = map_el.get_text(strip=True)

    match_id = None
    match_link = sp.select_one("a[href*='/matches/']")
    if match_link:
        match_id = common.match_id_from_href(match_link["href"])

    match_date = None
    date_el = sp.select_one(".match-info-box .small-text, .timeAndEvent .date")
    if date_el:
        match_date = utils.parse_date(date_el.get_text())

    team_links = sp.select(".team-left a[href*='/teams/'], "
                           ".team-right a[href*='/teams/']")
    team1_id = team2_id = None
    if len(team_links) >= 2:
        team1_id = _stats_team_id(team_links[0]["href"])
        team2_id = _stats_team_id(team_links[1]["href"])

    # Team round scores in the header (e.g. "16 : 9")
    score_els = sp.select(".team-left .bold, .team-right .bold")
    t1_score = utils.parse_int(score_els[0].get_text()) if len(score_els) > 0 else None
    t2_score = utils.parse_int(score_els[1].get_text()) if len(score_els) > 1 else None

    mapstat = models.MapStat(
        id=mapstats_id,
        match_id=match_id,
        map_name=map_name,
        match_date=match_date,
        event_id=None,
        team1_id=team1_id,
        team2_id=team2_id,
        team1_score=t1_score,
        team2_score=t2_score,
    )

    # --- per-player scoreboards (two tables, one per team) ----------------
    perfs: List[models.PlayerMapPerformance] = []
    tables = sp.select("table.stats-table.totalstats, table.totalstats, "
                       "table.stats-table")
    team_ids = [team1_id, team2_id]
    for ti, table in enumerate(tables[:2]):
        tid = team_ids[ti] if ti < len(team_ids) else None
        for tr in table.select("tbody tr"):
            plink = tr.select_one("a[href*='/players/']")
            if plink is None:
                continue
            pid = common.player_id_from_href(plink["href"])
            if pid is None:
                continue
            nick = plink.get_text(strip=True)

            def cell(cls):
                el = tr.select_one(f".{cls}")
                return el.get_text(strip=True) if el else None

            # "20 (12)" -> kills 20, hs 12
            kills_txt = cell("st-kills")
            assists_txt = cell("st-assists")
            perfs.append(models.PlayerMapPerformance(
                map_id=mapstats_id,
                player_id=pid,
                player_nick=nick,
                team_id=tid,
                kills=utils.parse_int(kills_txt),
                deaths=utils.parse_int(cell("st-deaths")),
                assists=utils.parse_int(assists_txt),
                kddiff=utils.parse_int(cell("st-kddiff")),
                adr=utils.parse_float(cell("st-adr")),
                kast=utils.parse_percent(cell("st-kast")),
                rating=utils.parse_float(cell("st-rating")),
                first_kills=utils.parse_int(cell("st-fkdiff")),
            ))

    return mapstat, perfs
