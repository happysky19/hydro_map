"""Every plot kind renders from partially downloaded source folders and from a delivery."""

from contextlib import redirect_stdout
import csv
from datetime import date, timedelta
import gzip
import io
import json
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


class CatchmentTests(unittest.TestCase):
    """The map, seasons and anomaly kinds draw every catchment from one working directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'aorc').mkdir()
        days = [date(2021, 10, 1) + timedelta(days=i) for i in range(365 * 3)]
        with gzip.open(self.root / 'aorc' / 'daily_2021.csv.gz', 'wt', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for index, day in enumerate(days):
                for project, wet in (('MICA', 3.), ('SWIFT', 8.), ('X', 1.)):
                    if project == 'X' and day == date(2023, 6, 18):
                        continue   # one missing day must not drop the year
                    values = {'precipitation_mm': wet + wet * np.sin(index / 58), 'tmean_c': 5 + 12 * np.sin(index / 58),
                              'snowfall_mm': wet / 2 if day.month in (11, 12, 1, 2, 3) else 0.}
                    for variable, value in values.items():
                        writer.writerow(dict(date=day.isoformat(), source='aorc_v1.1', project_id=project,
                                             variable=variable, value=value, units='', expected_hours=24,
                                             valid_hours=24, min_valid_area_fraction=1, qc='valid'))
        square = lambda lon, lat: [[[lon, lat], [lon + 1, lat], [lon + 1, lat + 1], [lon, lat + 1], [lon, lat]]]
        features = [
            dict(type='Feature', properties=dict(id='MICA', name='Mica', lat=52.1, lon=-118.6, area_local=21054),
                 geometry=dict(type='Polygon', coordinates=square(-119, 51.5))),
            dict(type='Feature', properties=dict(id='SWIFT', name='Swift', lat=46.1, lon=-122.2, area_local=1240),
                 geometry=dict(type='MultiPolygon', coordinates=[square(-123, 45.5), square(-124.5, 45.5)])),
            dict(type='Feature', properties=dict(id='X', name="Seli's Ksanka Qlispe (Kerr)", lat=47.7, lon=-114.2,
                                                 area_local=14085),
                 geometry=dict(type='Polygon', coordinates=square(-115, 47))),
        ]
        self.geojson = self.root / 'catchments.geojson'
        self.geojson.write_text(json.dumps(dict(type='FeatureCollection', features=features)))

    def render(self, kind):
        output = self.root / f'{kind}.png'
        argv = ['plot_daily.py', str(self.root), '--kind', kind, '--geojson', str(self.geojson), '--output', str(output)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            plot_daily.main()
        self.assertGreater(output.stat().st_size, 10000)

    def test_each_catchment_kind_renders(self):
        for kind in plot_daily.CATCHMENT_KINDS:
            self.render(kind)

    def test_rows_follow_river_systems_then_latitude_and_short_names(self):
        meta = plot_daily.catchments(self.geojson)
        self.assertEqual(meta['X']['name'], 'Kerr')
        self.assertEqual(len(meta['SWIFT']['rings']), 2)
        rows, groups = plot_daily.arrange(['X', 'SWIFT', 'MICA'], meta)
        self.assertEqual(rows, ['MICA', 'SWIFT', 'X'])
        self.assertEqual(groups, [('Columbia, Canada', 1), ('Cascades', 1), ('Other', 1)])

    def test_complete_years_allow_a_few_missing_days(self):
        series = plot_daily.load_many(self.root, ['precipitation_mm'])
        counts = plot_daily.year_counts(series, ('aorc_v1.1', 'precipitation_mm'))
        self.assertEqual(plot_daily.complete_years(counts, list(series)), [2022, 2023])
        water = plot_daily.year_counts(series, ('aorc_v1.1', 'precipitation_mm'), water=True)
        self.assertEqual(plot_daily.complete_years(water, list(series)), [2022, 2023, 2024])
        self.assertEqual(plot_daily.span([1997, 1998, 2000, 2002, 2003, 2004]), '1997–1998, 2000 and 2002–2004')
        self.assertEqual(plot_daily.span([2024]), '2024')


if __name__ == '__main__':
    unittest.main()
