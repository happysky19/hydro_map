"""Every plot kind renders from partially downloaded source folders and from a delivery."""

from contextlib import redirect_stdout
import csv
from datetime import date, timedelta
import gzip
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import plot_daily


FIELDS = ['date', 'source', 'project_id', 'variable', 'value', 'units', 'expected_hours',
          'valid_hours', 'min_valid_area_fraction', 'qc']


class PlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        days = [date(2023, 9, 1) + timedelta(days=i) for i in range(500)]
        values = {
            ('aorc', 'aorc_v1.1'): {'precipitation_mm': 3., 'rainfall_mm': 2., 'snowfall_mm': 1., 'tmean_c': 4.,
                                   'tmin_c': -1., 'tmax_c': 9., 'pet_hargreaves_mm': 1.5},
            ('era5-land', 'era5_land_cds'): {'snow_water_equivalent_mm': 80., 'precipitation_mm': 3.5,
                                             'soil_moisture_layer1_m3m3': .3, 'tmean_c': 3.,
                                             'root_zone_soil_moisture_0_100cm_m3m3': .32,
                                             'actual_evapotranspiration_mm': 1.},
        }
        for (folder, source), fields in values.items():
            (self.root / folder).mkdir()
            with gzip.open(self.root / folder / 'daily_2023.csv.gz', 'wt', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                for index, day in enumerate(days):
                    if day == date(2024, 6, 18):
                        continue
                    for variable, base in fields.items():
                        writer.writerow(dict(date=day.isoformat(), source=source, project_id='A', variable=variable,
                                             value=base + np.sin(index/20), units='', expected_hours=24,
                                             valid_hours=24, min_valid_area_fraction=1, qc='valid'))

    def render(self, *arguments):
        output = self.root / 'figure.png'
        argv = ['plot_daily.py', str(self.root), '--project', 'A', '--output', str(output), *arguments]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            plot_daily.main()
        self.assertGreater(output.stat().st_size, 10000)

    def test_each_kind_renders_partial_downloads(self):
        self.render()
        self.render('--kind', 'compare', '--variable', 'tmean_c', '--resample', 'W')
        self.render('--kind', 'wateryear', '--variable', 'precipitation_mm')
        self.render('--kind', 'wateryear', '--variable', 'snow_water_equivalent_mm', '--start', '2023-10-01')

    def test_gaps_break_lines_and_periods_need_complete_amounts(self):
        days = np.array([date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 4)])
        regular, values = plot_daily.resample(days, np.array([1., 2., 4.]), 'D', True)
        self.assertEqual(len(regular), 4)
        self.assertTrue(np.isnan(values[2]))
        weeks, totals = plot_daily.resample(days, np.array([1., 2., 4.]), 'W', True)
        self.assertTrue(np.isnan(totals[0]))


if __name__ == '__main__':
    unittest.main()
