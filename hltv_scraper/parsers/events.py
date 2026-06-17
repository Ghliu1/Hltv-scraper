"""Parse the HLTV events archive listing into :class:`Event` records.

Events carry the tier/prize-pool context that lets the analysis layer weight a
player's stats by the calibre of competition (a clutch at a Major is worth more
than one at an online qualifier).
"""

from __future__ import annotations

from typing import List

from .. import models, utils
from . import common


def parse_events_archive(html: str) -> List[models.Event]:
    sp = common.soup(html)
    out: List[models.Event] = []
    for card in sp.select("a.small-event, a.big-event, .event-col a[href^='/events/']"):
        eid = common.event_id_from_href(card.get("href", ""))
        if eid is None:
            continue
        name_el = card.select_one(".text-ellipsis, .event-name-small, .eventname")
        name = name_el.get_text(strip=True) if name_el else card.get_text(strip=True)

        dates = card.select(".eventDetails span[data-unix], .col-desc .smallish")
        start = utils.parse_date(dates[0].get_text()) if len(dates) > 0 else None
        end = utils.parse_date(dates[1].get_text()) if len(dates) > 1 else None

        prize_el = card.select_one(".prizePoolEllipsis, .prize")
        loc_el = card.select_one(".smallCountry img, .location img")

        out.append(models.Event(
            id=eid,
            name=name or str(eid),
            start_date=start,
            end_date=end,
            prize_pool=prize_el.get_text(strip=True) if prize_el else None,
            location=(loc_el.get("title") if loc_el else None),
        ))
    return out
