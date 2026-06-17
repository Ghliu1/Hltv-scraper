"""End-to-end test of the orchestration + persistence + analysis stack.

A FakeClient serves saved fixtures instead of hitting HLTV, so we exercise the
full fetch -> parse -> persist -> analyse pipeline deterministically and offline.
"""

import os
import tempfile
import unittest
from datetime import date

from hltv_scraper.config import Settings
from hltv_scraper.db import Database
from hltv_scraper.scrapers import Orchestrator
from hltv_scraper.analysis import contribution, meta

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class FakeClient:
    """Serves fixtures based on URL shape; records what was requested."""

    def __init__(self):
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        if "/ranking/teams/" in url:
            return load("ranking.html")
        if "/stats/players/individual/" in url:
            return load("player_individual.html")
        if "/stats/players/" in url:
            return load("player_overview.html")
        if "/mapstatsid/" in url:
            return load("map_stats.html")
        raise AssertionError(f"unexpected url: {url}")

    def get_cached_only(self, url):
        return None


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        settings = Settings()
        settings.db_path = os.path.join(self.tmp, "t.sqlite3")
        settings.data_dir = self.tmp
        settings.cache_dir = os.path.join(self.tmp, "cache")
        settings.start_date = date(2021, 1, 1)
        self.db = Database(settings.db_path)
        self.orch = Orchestrator(settings, client=FakeClient(), db=self.db)

    def test_full_pipeline(self):
        # 1) Rankings -> teams, players, rosters
        n = self.orch.scrape_rankings(date(2021, 8, 1), date(2021, 8, 1),
                                      step_days=28)
        self.assertGreaterEqual(n, 1)
        counts = self.db.counts()
        self.assertEqual(counts["teams"], 2)
        self.assertGreaterEqual(counts["players"], 5)

        # 2) Player discovery respects top-N ranking filter
        targets = self.orch.discover_player_ids(top_n=100)
        ids = {pid for pid, _ in targets}
        self.assertIn(7998, ids)

        # 3) Per-period player stats persisted with merged fields
        saved = self.orch.scrape_player_periods(
            7998, "s1mple", date(2021, 1, 1), date(2021, 6, 30),
            period_months=6,
        )
        self.assertEqual(saved, 1)
        row = self.db.query(
            "SELECT * FROM player_stat_periods WHERE player_id=7998")[0]
        self.assertAlmostEqual(row["rating"], 1.26)
        self.assertEqual(row["opening_kills"], 1204)
        self.assertEqual(row["clutches_1v1"], 180)

        # 4) Map scoreboard + derived head-to-head
        ok = self.orch.scrape_map_scoreboard(123456, "mirage")
        self.assertTrue(ok)
        self.assertEqual(self.db.counts()["player_map_performance"], 4)
        self.assertGreater(self.db.counts()["head_to_head"], 0)

        # 5) Analysis runs over the populated store
        contribs = contribution.compute_contributions(self.db)
        self.assertEqual(len(contribs), 1)
        bd = contribs[0]
        self.assertGreater(bd.score, 0)
        self.assertIn("fragging", bd.facets)

        frag = meta.fragging_meta(self.db)
        self.assertEqual(len(frag), 1)
        self.assertAlmostEqual(frag[0]["avg_rating"], 1.26)

    def test_resumable_scrape_log(self):
        url = "https://www.hltv.org/ranking/teams/2021/august/2"
        self.assertFalse(self.db.is_scraped(url))
        self.db.mark_scraped(url, "ranking", "ok")
        self.assertTrue(self.db.is_scraped(url))


if __name__ == "__main__":
    unittest.main()
