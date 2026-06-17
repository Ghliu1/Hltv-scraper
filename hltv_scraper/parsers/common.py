"""Shared parsing helpers built on BeautifulSoup.

The recurring HLTV pattern is "a label paired with a value" (``.stats-row``,
summary breakdown boxes, info tables). We normalise labels into stable keys and
expose a single dict of ``{normalized_label: raw_value_text}`` so the typed
parsers can map known metrics into fields by fuzzy matching, tolerating the
cosmetic label changes HLTV makes over the years.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from bs4 import BeautifulSoup

from .. import utils


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


_norm_re = re.compile(r"[^a-z0-9]+")


def norm_label(text: str) -> str:
    """Normalise a stat label to a stable lookup key.

    'Total opening kills' -> 'totalopeningkills'
    'K/D Ratio'           -> 'kdratio'
    'Saved by teammate / round' -> 'savedbyteammateround'
    """
    return _norm_re.sub("", text.strip().lower())


def player_id_from_href(href: str) -> Optional[int]:
    """'/stats/players/7998/s1mple' or '/player/7998/s1mple' -> 7998."""
    m = re.search(r"/(?:stats/)?players?/(?:individual/)?(\d+)/", href)
    if m:
        return int(m.group(1))
    m = re.search(r"/player/(\d+)/", href)
    return int(m.group(1)) if m else None


def team_id_from_href(href: str) -> Optional[int]:
    m = re.search(r"/team/(\d+)/", href)
    return int(m.group(1)) if m else None


def event_id_from_href(href: str) -> Optional[int]:
    m = re.search(r"/events?/(\d+)/", href)
    return int(m.group(1)) if m else None


def match_id_from_href(href: str) -> Optional[int]:
    m = re.search(r"/matches?/(\d+)/", href)
    return int(m.group(1)) if m else None


def mapstats_id_from_href(href: str) -> Optional[int]:
    m = re.search(r"/mapstatsid/(\d+)/", href)
    return int(m.group(1)) if m else None


def stat_rows(sp: BeautifulSoup) -> Dict[str, str]:
    """Collect every label/value pair on a stats page into one dict.

    Covers the three layouts HLTV uses:
      * ``.stats-row`` — two child spans (label, value)
      * summary breakdown boxes — ``.summaryStatBreakdownDataValue`` plus a
        sibling ``.summaryStatBreakdownSubHeader``
      * generic two-cell ``.stats-table`` info rows
    Later writes win, but values are identical across layouts in practice.
    """
    out: Dict[str, str] = {}

    for row in sp.select(".stats-row"):
        spans = row.find_all("span")
        if len(spans) >= 2:
            out[norm_label(spans[0].get_text())] = spans[-1].get_text(strip=True)

    for box in sp.select(".summaryStatBreakdownRow, .summaryStatBreakdown"):
        val = box.select_one(".summaryStatBreakdownDataValue")
        head = box.select_one(
            ".summaryStatBreakdownSubHeader, .summaryStatBreakdownDataDescire"
        )
        if val and head:
            # Header may contain a unit subtitle; take its leading word(s).
            label = head.get_text(" ", strip=True)
            out[norm_label(label)] = val.get_text(strip=True)

    return out


def pick(rows: Dict[str, str], *keys: str) -> Optional[str]:
    """Return the first present value among ``keys`` (already normalised)."""
    for k in keys:
        if k in rows and rows[k] not in ("", "-"):
            return rows[k]
    return None
