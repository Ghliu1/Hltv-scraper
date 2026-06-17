"""Parse HLTV player statistics pages into a :class:`PlayerStatPeriod`.

HLTV splits a player's numbers across several sibling pages that share an id +
slug and a common ``?startDate=&endDate=&rankingFilter=`` query:

    /stats/players/<id>/<slug>             overview (rating, ADR, KAST, ...)
    /stats/players/individual/<id>/<slug>  entries, multi-kills, clutches
    (utility numbers appear on the overview/individual pages)

We parse each page into a flat label->value dict (see :mod:`common`) and then
merge the recognised metrics into one record. Unrecognised labels are simply
ignored, and missing ones stay ``None`` — both make the parser robust to
HLTV's periodic relabelling.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from .. import models, utils
from . import common


def parse_player_identity(html: str, player_id: int) -> models.Player:
    sp = common.soup(html)
    nick_el = sp.select_one(".summaryNickname, .playerNickname, .context-item-name")
    real_el = sp.select_one(".summaryRealname, .playerRealname")
    country_el = sp.select_one(".summaryRealname img, .playerRealname img, "
                               ".summaryFlag")
    nick = nick_el.get_text(strip=True) if nick_el else str(player_id)
    real = real_el.get_text(strip=True) if real_el else None
    country = None
    if country_el is not None:
        country = country_el.get("title") or country_el.get("alt")
    return models.Player(id=player_id, nick=nick, real_name=real, country=country)


def _merge_overview(rows: dict, rec: models.PlayerStatPeriod) -> None:
    p = common.pick
    rec.rating = utils.parse_float(p(rows, "rating20", "rating21", "rating"))
    # Detect rating version from whichever label was present.
    if "rating21" in rows:
        rec.rating_version = "2.1"
    elif "rating20" in rows or rec.rating is not None:
        rec.rating_version = "2.0"

    rec.kpr = utils.parse_float(p(rows, "killsperround", "killsround", "kpr"))
    rec.dpr = utils.parse_float(p(rows, "deathsperround", "deathsround", "dpr"))
    rec.apr = utils.parse_float(p(rows, "assistsperround", "assistsround"))
    rec.kast = utils.parse_percent(p(rows, "kast"))
    rec.impact = utils.parse_float(p(rows, "impact"))
    rec.adr = utils.parse_float(p(rows, "damageround", "adr",
                                    "averagedamageperround"))
    rec.kddiff = utils.parse_int(p(rows, "kddiff", "killsdeathsdifference"))

    rec.total_kills = utils.parse_int(p(rows, "totalkills"))
    rec.total_deaths = utils.parse_int(p(rows, "totaldeaths"))
    rec.headshot_pct = utils.parse_percent(p(rows, "headshot", "headshots",
                                              "headshotkills"))
    rec.kd_ratio = utils.parse_float(p(rows, "kdratio"))

    rec.maps_played = utils.parse_int(p(rows, "mapsplayed"))
    rec.rounds_played = utils.parse_int(p(rows, "roundsplayed"))

    rec.saved_by_teammate_pr = utils.parse_float(
        p(rows, "savedbyteammateround", "savedbyteammateperround"))
    rec.saved_teammates_pr = utils.parse_float(
        p(rows, "savedteammatesround", "savedteammatesperround"))
    rec.grenade_dmg_pr = utils.parse_float(
        p(rows, "grenadedmground", "grenadedamageperround"))
    rec.utility_damage_pr = utils.parse_float(
        p(rows, "utilitydamageround", "utilitydamageperround"))


def _merge_individual(rows: dict, rec: models.PlayerStatPeriod) -> None:
    p = common.pick

    # Opening duels / entries
    rec.opening_kills = utils.parse_int(p(rows, "totalopeningkills",
                                          "openingkills"))
    rec.opening_deaths = utils.parse_int(p(rows, "totalopeningdeaths",
                                           "openingdeaths"))
    rec.opening_kpr = utils.parse_float(p(rows, "openingkillratio",
                                          "openingkillsperround"))
    rec.opening_rating = utils.parse_float(p(rows, "openingkillrating"))
    rec.opening_success_pct = utils.parse_percent(
        p(rows, "openingkillwon", "wonafteropeningkill", "openingsuccess"))
    rec.team_win_pct_after_first_kill = utils.parse_percent(
        p(rows, "teamwinpercentafterfirstkill", "teamwinafterfirstkill"))

    # Multi-kill rounds
    rec.rounds_with_kills_0 = utils.parse_int(p(rows, "0killrounds"))
    rec.rounds_with_kills_1 = utils.parse_int(p(rows, "1killrounds"))
    rec.rounds_with_kills_2 = utils.parse_int(p(rows, "2killrounds"))
    rec.rounds_with_kills_3 = utils.parse_int(p(rows, "3killrounds"))
    rec.rounds_with_kills_4 = utils.parse_int(p(rows, "4killrounds"))
    rec.rounds_with_kills_5 = utils.parse_int(p(rows, "5killrounds"))

    # Clutches (HLTV labels them "1on1", "1on2", ...)
    rec.clutches_1v1 = utils.parse_int(p(rows, "1on1", "1v1clutcheswon", "1v1"))
    rec.clutches_1v2 = utils.parse_int(p(rows, "1on2", "1v2clutcheswon", "1v2"))
    rec.clutches_1v3 = utils.parse_int(p(rows, "1on3", "1v3clutcheswon", "1v3"))
    rec.clutches_1v4 = utils.parse_int(p(rows, "1on4", "1v4clutcheswon", "1v4"))
    rec.clutches_1v5 = utils.parse_int(p(rows, "1on5", "1v5clutcheswon", "1v5"))

    # Utility (also appears here on some layouts)
    if rec.flash_assists is None:
        rec.flash_assists = utils.parse_int(p(rows, "flashassists",
                                              "totalflashassists"))
    if rec.flashes_thrown is None:
        rec.flashes_thrown = utils.parse_int(p(rows, "flashesthrown",
                                               "totalflashesthrown"))
    if rec.enemies_flashed is None:
        rec.enemies_flashed = utils.parse_int(p(rows, "enemiesflashed",
                                                "thrownflashesthatenemies"))
    rec.flashed_per_thrown = utils.parse_float(
        p(rows, "flashedperthrown", "opponentsflashedperflash"))


def parse_player_stats(
    player_id: int,
    period_start: date,
    period_end: date,
    *,
    overview_html: Optional[str] = None,
    individual_html: Optional[str] = None,
    ranking_filter: str = "ALL",
) -> models.PlayerStatPeriod:
    """Merge whichever sub-pages were provided into one stat record."""
    rec = models.PlayerStatPeriod(
        player_id=player_id,
        period_start=period_start,
        period_end=period_end,
        ranking_filter=ranking_filter,
    )
    if overview_html:
        _merge_overview(common.stat_rows(common.soup(overview_html)), rec)
    if individual_html:
        _merge_individual(common.stat_rows(common.soup(individual_html)), rec)
    return rec
