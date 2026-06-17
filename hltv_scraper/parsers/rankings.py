"""Parse HLTV weekly team-ranking snapshots.

URL form: ``/ranking/teams/<year>/<month-name>/<day>`` (the Monday of a week).
Each entry yields the team's rank + points and its 5-player lineup, which is
how we reconstruct rosters over time and decide which teams are "tier 1-3"
(top-N) at any given date.
"""

from __future__ import annotations

from datetime import date
from typing import List, Tuple

from .. import models, utils
from . import common


def parse_ranking(html: str, snapshot_date: date) -> Tuple[
    List[models.TeamRanking],
    List[models.RosterMembership],
    List[models.Player],
    List[models.Team],
]:
    sp = common.soup(html)
    rankings: List[models.TeamRanking] = []
    rosters: List[models.RosterMembership] = []
    players: dict[int, models.Player] = {}
    teams: dict[int, models.Team] = {}

    for box in sp.select(".ranked-team"):
        pos_el = box.select_one(".position")
        rank = utils.parse_int(pos_el.get_text()) if pos_el else None

        team_link = box.select_one("a[href^='/team/']")
        team_id = common.team_id_from_href(team_link["href"]) if team_link else None

        name_el = box.select_one(".name")
        team_name = name_el.get_text(strip=True) if name_el else (
            team_link.get_text(strip=True) if team_link else None
        )

        points_el = box.select_one(".points")
        points = utils.parse_int(points_el.get_text()) if points_el else None

        if rank is None or team_id is None:
            continue

        teams[team_id] = models.Team(id=team_id, name=team_name or str(team_id))
        rankings.append(
            models.TeamRanking(
                snapshot_date=snapshot_date,
                rank=rank,
                team_id=team_id,
                team_name=team_name or str(team_id),
                points=points,
            )
        )

        for pl in box.select("a[href^='/player/']"):
            pid = common.player_id_from_href(pl["href"])
            if pid is None:
                continue
            nick = pl.get("title") or pl.get_text(strip=True)
            # Lineup links often wrap an image; fall back to the slug.
            if not nick:
                parts = pl["href"].rstrip("/").split("/")
                nick = parts[-1] if parts else str(pid)
            players.setdefault(pid, models.Player(id=pid, nick=nick))
            rosters.append(
                models.RosterMembership(
                    snapshot_date=snapshot_date,
                    team_id=team_id,
                    player_id=pid,
                    player_nick=nick,
                )
            )

    return rankings, rosters, list(players.values()), list(teams.values())
