import unittest
from datetime import date

from hltv_scraper import utils
from hltv_scraper.parsers import common


class NumericParsingTest(unittest.TestCase):
    def test_parse_int(self):
        self.assertEqual(utils.parse_int("1,204"), 1204)
        self.assertEqual(utils.parse_int("+14"), 14)
        self.assertEqual(utils.parse_int("-4"), -4)
        self.assertEqual(utils.parse_int("−4"), -4)  # unicode minus
        self.assertIsNone(utils.parse_int("-"))
        self.assertIsNone(utils.parse_int(None))

    def test_parse_float_and_percent(self):
        self.assertAlmostEqual(utils.parse_float("1.26"), 1.26)
        self.assertAlmostEqual(utils.parse_percent("73.5%"), 0.735)
        self.assertIsNone(utils.parse_percent(""))

    def test_slugify(self):
        self.assertEqual(utils.slugify("Natus Vincere"), "natus-vincere")
        self.assertEqual(utils.slugify("device"), "device")
        self.assertEqual(utils.slugify("ÆØÅ name"), "a-name")


class DateTest(unittest.TestCase):
    def test_parse_date_formats(self):
        self.assertEqual(utils.parse_date("2021-08-23"), date(2021, 8, 23))
        self.assertEqual(utils.parse_date("21/08/2012"), date(2012, 8, 21))
        self.assertEqual(utils.parse_date("Aug 21, 2012"), date(2012, 8, 21))

    def test_iter_periods(self):
        periods = list(utils.iter_periods(date(2020, 1, 1), date(2020, 12, 31), 6))
        self.assertEqual(periods[0][0], date(2020, 1, 1))
        self.assertEqual(periods[0][1], date(2020, 6, 30))
        self.assertEqual(periods[1][0], date(2020, 7, 1))
        self.assertEqual(periods[1][1], date(2020, 12, 31))


class CommonHelpersTest(unittest.TestCase):
    def test_norm_label(self):
        self.assertEqual(common.norm_label("K/D Ratio"), "kdratio")
        self.assertEqual(common.norm_label("Saved by teammate / round"),
                         "savedbyteammateround")

    def test_id_extraction(self):
        self.assertEqual(common.player_id_from_href("/player/7998/s1mple"), 7998)
        self.assertEqual(
            common.player_id_from_href("/stats/players/7998/s1mple"), 7998)
        self.assertEqual(
            common.player_id_from_href("/stats/players/individual/7998/s1mple"),
            7998)
        self.assertEqual(common.team_id_from_href("/team/4608/navi"), 4608)
        self.assertEqual(
            common.mapstats_id_from_href("/stats/matches/mapstatsid/123/x"), 123)


if __name__ == "__main__":
    unittest.main()
