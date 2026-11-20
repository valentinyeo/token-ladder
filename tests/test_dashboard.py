import unittest

from _load import tl

NOW = tl.iso_to_ts("2026-10-24T09:00:00+00:00")


def win(used, time_gone, minutes=10080):
    hours_left = minutes * (1 - time_gone / 100) / 60
    return tl.window(used, NOW + hours_left * 3600, minutes, NOW)


class UsageRowTests(unittest.TestCase):
    def test_row_shows_pace_and_reset_clock(self):
        line = tl.usage_line(win(50, 50))
        self.assertIn("50% used", line)
        self.assertIn("ON PACE", line)
        self.assertRegex(line, r"resets [A-Z][a-z]{2} \d{2}:\d{2}, in \d+ h")

    def test_row_says_behind_when_the_bar_is_left_of_the_tick(self):
        self.assertIn("BEHIND", tl.usage_line(win(20, 50)))

    def test_row_says_ahead_when_the_bar_is_right_of_the_tick(self):
        self.assertIn("AHEAD", tl.usage_line(win(80, 20)))

    def test_ten_points_off_pace_still_reads_on_pace(self):
        self.assertIn("ON PACE", tl.usage_line(win(40, 50)))
        self.assertIn("ON PACE", tl.usage_line(win(60, 50)))

    def test_row_without_numbers_has_no_pace_word(self):
        line = tl.usage_line(tl.window(None, None, 10080, NOW))
        self.assertIn("unknown", line)
        self.assertNotIn("PACE", line)


if __name__ == "__main__":
    unittest.main()
