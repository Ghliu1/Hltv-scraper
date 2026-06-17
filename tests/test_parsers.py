"""Unit tests for the pure parsing layer, run against saved fixtures.

These prove the parser *logic* is correct for HLTV's documented markup without
needing network access. They double as living documentation of what each page
yields.
"""

import os
import unittest
from datetime import date

from hltv_scraper.parsers import rankings, player_stats, matches

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class RankingParserTest(unittest.TestCase):
    def test_parse_ranking(self):
        rk, rosters, players, teams = rankings.parse_ranking(
            load("ranking.html"), date(2021, 8, 23)
        )
        self.assertEqual(len(rk), 2)
        navi = next(r for r in rk if r.team_id == 4608)
        self.assertEqual(navi.rank, 1)
        self.assertEqual(navi.team_name, "Natus Vincere")
        self.assertEqual(navi.points, 963)

        # Rosters / players linked correctly.
        navi_players = {m.player_id for m in rosters if m.team_id == 4608}
        self.assertIn(7998, navi_players)
        self.assertEqual(len(navi_players), 5)
        nicks = {p.id: p.nick for p in players}
        self.assertEqual(nicks[7998], "s1mple")
        self.assertEqual({t.id for t in teams}, {4608, 6665})


class PlayerStatsParserTest(unittest.TestCase):
    def test_overview_metrics(self):
        rec = player_stats.parse_player_stats(
            7998, date(2021, 1, 1), date(2021, 6, 30),
            overview_html=load("player_overview.html"),
        )
        self.assertAlmostEqual(rec.rating, 1.26)
        self.assertEqual(rec.rating_version, "2.1")
        self.assertAlmostEqual(rec.kast, 0.735)
        self.assertAlmostEqual(rec.adr, 85.7)
        self.assertAlmostEqual(rec.dpr, 0.61)
        self.assertEqual(rec.total_kills, 10543)
        self.assertEqual(rec.total_deaths, 7801)
        self.assertAlmostEqual(rec.headshot_pct, 0.478)
        self.assertAlmostEqual(rec.kd_ratio, 1.35)
        self.assertEqual(rec.maps_played, 412)
        self.assertEqual(rec.rounds_played, 10980)
        self.assertAlmostEqual(rec.kpr, 0.80)
        self.assertAlmostEqual(rec.grenade_dmg_pr, 4.1)

    def test_individual_metrics(self):
        rec = player_stats.parse_player_stats(
            7998, date(2021, 1, 1), date(2021, 6, 30),
            individual_html=load("player_individual.html"),
        )
        self.assertEqual(rec.opening_kills, 1204)
        self.assertEqual(rec.opening_deaths, 812)
        self.assertAlmostEqual(rec.opening_kpr, 1.48)
        self.assertAlmostEqual(rec.opening_rating, 1.31)
        self.assertAlmostEqual(rec.opening_success_pct, 0.724)
        self.assertAlmostEqual(rec.team_win_pct_after_first_kill, 0.701)
        self.assertEqual(rec.rounds_with_kills_3, 720)
        self.assertEqual(rec.rounds_with_kills_5, 61)
        self.assertEqual(rec.clutches_1v1, 180)
        self.assertEqual(rec.clutches_1v5, 3)
        self.assertEqual(rec.flash_assists, 410)

    def test_merge_overview_and_individual(self):
        rec = player_stats.parse_player_stats(
            7998, date(2021, 1, 1), date(2021, 6, 30),
            overview_html=load("player_overview.html"),
            individual_html=load("player_individual.html"),
        )
        # Fields from both pages coexist on one record.
        self.assertEqual(rec.maps_played, 412)
        self.assertEqual(rec.opening_kills, 1204)


class MapStatsParserTest(unittest.TestCase):
    def test_parse_map_stats(self):
        mapstat, perfs = matches.parse_map_stats(load("map_stats.html"), 123456)
        self.assertEqual(mapstat.id, 123456)
        self.assertEqual(mapstat.map_name, "Mirage")
        self.assertEqual(mapstat.match_id, 2350001)
        self.assertEqual(mapstat.team1_id, 4608)
        self.assertEqual(mapstat.team2_id, 6665)
        self.assertEqual(mapstat.team1_score, 16)
        self.assertEqual(mapstat.team2_score, 9)

        self.assertEqual(len(perfs), 4)
        s1mple = next(p for p in perfs if p.player_id == 7998)
        self.assertEqual(s1mple.kills, 28)
        self.assertEqual(s1mple.deaths, 14)
        self.assertEqual(s1mple.kddiff, 14)
        self.assertAlmostEqual(s1mple.adr, 102.3)
        self.assertAlmostEqual(s1mple.kast, 0.815)
        self.assertAlmostEqual(s1mple.rating, 1.74)
        self.assertEqual(s1mple.team_id, 4608)
        # Opposing team players attributed to team 6665.
        device = next(p for p in perfs if p.player_id == 7592)
        self.assertEqual(device.team_id, 6665)


if __name__ == "__main__":
    unittest.main()
