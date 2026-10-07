"""Gap filling of the requested table keeps the physical identities and flags every value."""

from datetime import date, timedelta
import unittest

import numpy as np

from deliver_daily import fill_aorc_gaps
from meteorology import hargreaves_pet


class GapFillTests(unittest.TestCase):
    def setUp(self):
        days = [date(2024, 6, 1) + timedelta(days=i) for i in range(30)]
        self.dates = days * 2
        self.projects = np.array(['A']*30 + ['B']*30)
        n = len(self.dates)
        ramp = np.tile(np.arange(30.), 2)
        self.land = dict(precipitation_mm=1 + ramp % 4, snowfall_mm=np.zeros(n),
                         tmean_c=10 + ramp/10, tmin_c=4 + ramp/10, tmax_c=16 + ramp/10,
                         relative_humidity_pct=np.full(n, 99.), u_wind_ms=np.full(n, 3.),
                         v_wind_ms=np.full(n, 4.), wind_speed_ms=np.full(n, 5.5))
        self.values = dict(precipitation_mm=2*self.land['precipitation_mm'],
                           rainfall_mm=2*self.land['precipitation_mm'], snowfall_mm=np.zeros(n),
                           tmean_c=self.land['tmean_c'] + 1, tmin_c=self.land['tmin_c'] + 3,
                           tmax_c=self.land['tmax_c'] - 3, relative_humidity_pct=np.full(n, 99.) + 5,
                           u_wind_ms=np.full(n, 3.), v_wind_ms=np.full(n, 4.),
                           wind_speed_ms=np.full(n, 5.5), pet_hargreaves_mm=np.full(n, 3.))
        self.gap = 17
        for array in self.values.values():
            array[self.gap] = np.nan
        self.land['precipitation_mm'][self.gap] = 3.
        self.land['snowfall_mm'][self.gap] = 1.

    def test_adjusted_fill_and_consistency(self):
        filled = fill_aorc_gaps(self.values, self.land, self.projects, self.dates, {'A': 50., 'B': 45.})
        row = self.gap
        self.assertAlmostEqual(self.values['precipitation_mm'][row], 6.)
        self.assertAlmostEqual(self.values['snowfall_mm'][row], 2.)
        self.assertAlmostEqual(self.values['rainfall_mm'][row], 4.)
        self.assertAlmostEqual(self.values['tmean_c'][row], self.land['tmean_c'][row] + 1)
        self.assertLessEqual(self.values['tmin_c'][row], self.values['tmean_c'][row])
        self.assertGreaterEqual(self.values['tmax_c'][row], self.values['tmean_c'][row])
        self.assertLessEqual(self.values['relative_humidity_pct'][row], 100)
        self.assertGreaterEqual(self.values['wind_speed_ms'][row], 5.)
        stats = [self.values[name][row] for name in ('tmean_c', 'tmin_c', 'tmax_c')]
        self.assertAlmostEqual(self.values['pet_hargreaves_mm'][row],
                               hargreaves_pet(*stats, 50., self.dates[row]))
        self.assertTrue(all(rows[row] for rows in filled.values()))
        self.assertEqual(sum(int(rows.sum()) for rows in filled.values()), len(filled))
        self.assertTrue(all(np.isfinite(array).all() for array in self.values.values()))

    def test_no_counterpart_leaves_blank(self):
        self.land['precipitation_mm'][self.gap] = np.nan
        filled = fill_aorc_gaps(self.values, self.land, self.projects, self.dates, {'A': 50.})
        self.assertTrue(np.isnan(self.values['precipitation_mm'][self.gap]))
        self.assertFalse(filled['precipitation_mm'].any())
        self.assertFalse(filled['rainfall_mm'].any())


if __name__ == '__main__':
    unittest.main()
