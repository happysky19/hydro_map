from datetime import date, timedelta
from math import isnan
import unittest
import daymet_calendar
from daymet_calendar import align_daymet_series, daymet_date


class DaymetCalendarTests(unittest.TestCase):
    def test_leap_day_is_retained_and_year_end_not_shifted(self):
        self.assertEqual(daymet_date(1996, 60), date(1996, 2, 29))
        self.assertEqual(daymet_date(1996, 365), date(1996, 12, 30))
        self.assertEqual(daymet_date(1995, 365), date(1995, 12, 31))
        result = align_daymet_series([date(1996, 12, 30), date(1997, 1, 1)], [8., 2.], date(1996, 12, 30), date(1997, 1, 1))
        self.assertEqual([r['date'] for r in result], ['1996-12-30', '1996-12-31', '1997-01-01'])
        self.assertTrue(isnan(result[1]['value']))
        self.assertEqual(result[1]['qc'], 'daymet_calendar_gap')
        self.assertEqual(result[2]['value'], 2.)

    def test_thirty_year_window_has_exact_eight_structural_gaps(self):
        dates = [daymet_date(year, doy) for year in range(1995, 2025) for doy in range(1, 366)]
        result = align_daymet_series(dates, [1.] * len(dates), date(1995, 1, 1), date(2024, 12, 31))
        self.assertEqual(len(result), 10958)
        self.assertEqual([r['date'] for r in result if r['qc'] == 'daymet_calendar_gap'], [f'{year}-12-31' for year in range(1996, 2025, 4)])
        self.assertEqual(sum(r['qc'] == 'valid' for r in result), 10950)

    def test_missing_source_and_invalid_calendar_are_not_filled(self):
        result = align_daymet_series([], [], date(1996, 2, 29), date(1996, 2, 29))
        self.assertEqual(result[0]['qc'], 'source_missing')
        self.assertTrue(isnan(result[0]['value']))
        for doy in [0, 366, 60.5, True]:
            with self.assertRaises(ValueError): daymet_date(1996, doy)
        with self.assertRaises(ValueError):
            align_daymet_series([date(1996, 12, 31)], [3.], date(1996, 12, 31), date(1996, 12, 31))
        with self.assertRaises(ValueError):
            align_daymet_series([date(1997, 1, 1)] * 2, [3., 4.], date(1997, 1, 1), date(1997, 1, 1))

    def test_requested_window_rejects_short_shifted_and_duplicate_responses(self):
        expected = [date(1996, 2, 28), date(1996, 2, 29), date(1996, 3, 1)]
        daymet_calendar.validate_window(1996, 58, 60, expected, [59, 60, 61])
        for dates, year_days in [
            (expected[:2], [59, 60]),
            ([d + timedelta(days=1) for d in expected], [60, 61, 62]),
            ([expected[0]] * 3, [59, 59, 59]),
            (expected, [59, 60.5, 61]),
        ]:
            with self.assertRaises(ValueError):
                daymet_calendar.validate_window(1996, 58, 60, dates, year_days)


if __name__ == '__main__':
    unittest.main()
