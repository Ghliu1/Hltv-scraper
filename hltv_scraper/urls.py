"""Builders for the HLTV URLs the scraper visits.

Centralising URL construction keeps the query-parameter conventions
(``startDate``/``endDate``/``rankingFilter``) in one place and makes the
scrapers read declaratively.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Optional
from urllib.parse import urlencode

from .config import BASE_URL
from .utils import slugify


def ranking(d: date, base: str = BASE_URL) -> str:
    """Weekly world ranking. HLTV publishes Mondays; it snaps to the nearest."""
    month = calendar.month_name[d.month].lower()
    return f"{base}/ranking/teams/{d.year}/{month}/{d.day}"


def _stats_query(start: Optional[date], end: Optional[date],
                 ranking_filter: Optional[str], **extra) -> str:
    params = {}
    if start:
        params["startDate"] = start.isoformat()
    if end:
        params["endDate"] = end.isoformat()
    if ranking_filter and ranking_filter != "ALL":
        params["rankingFilter"] = ranking_filter
    params.update({k: v for k, v in extra.items() if v is not None})
    return ("?" + urlencode(params)) if params else ""


def player_overview(player_id: int, slug: str, start: Optional[date] = None,
                    end: Optional[date] = None, ranking_filter: Optional[str] = None,
                    base: str = BASE_URL) -> str:
    return (f"{base}/stats/players/{player_id}/{slugify(slug)}"
            + _stats_query(start, end, ranking_filter))


def player_individual(player_id: int, slug: str, start: Optional[date] = None,
                      end: Optional[date] = None,
                      ranking_filter: Optional[str] = None,
                      base: str = BASE_URL) -> str:
    return (f"{base}/stats/players/individual/{player_id}/{slugify(slug)}"
            + _stats_query(start, end, ranking_filter))


def player_matches(player_id: int, slug: str, start: Optional[date] = None,
                   end: Optional[date] = None,
                   ranking_filter: Optional[str] = None,
                   base: str = BASE_URL) -> str:
    return (f"{base}/stats/players/matches/{player_id}/{slugify(slug)}"
            + _stats_query(start, end, ranking_filter))


def matches_list(start: Optional[date] = None, end: Optional[date] = None,
                 ranking_filter: Optional[str] = None, offset: int = 0,
                 base: str = BASE_URL) -> str:
    extra = {"offset": offset} if offset else {}
    return (f"{base}/stats/matches"
            + _stats_query(start, end, ranking_filter, **extra))


def map_stats(mapstats_id: int, slug: str = "x", base: str = BASE_URL) -> str:
    return f"{base}/stats/matches/mapstatsid/{mapstats_id}/{slugify(slug)}"


def events_archive(offset: int = 0, base: str = BASE_URL) -> str:
    extra = {"offset": offset} if offset else {}
    q = ("?" + urlencode(extra)) if extra else ""
    return f"{base}/events/archive{q}"
