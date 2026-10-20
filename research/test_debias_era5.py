"""Quantile mapping recovers a known ERA5-Land to AORC relation and fills only where AORC is blank."""

from contextlib import redirect_stdout
import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

import debias_era5
from delivery_variables import AORC, LAND, REQUESTED
from export_daily import digest
from meteorology import hargreaves_pet


LATITUDES = {'A': 50., 'B': 45.}
GAP = (date(2023, 6, 18), 'A')


def write_table(path, keys, values, inputs=()):
    """A full table and manifest; values maps (source, variable) to arrays over keys."""
    names = {key: f"{key[0].replace('.', '_')}__{key[1]}" for key in values}
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['date', 'project_id', *names.values()])
        for i, (day, project) in enumerate(keys):
            writer.writerow([day.isoformat(), project, *['' if np.isnan(v[i]) else repr(float(v[i]))
                                                         for v in values.values()]])
    columns = {'date': {}, 'project_id': {}, **{name: dict(source=source, variable=variable)
                                                for (source, variable), name in names.items()}}
    Path(str(path) + '.manifest.json').write_text(json.dumps(dict(
        columns=columns, output_sha256=digest(path), inputs=list(inputs))))


def truth(land, dates, projects):
    """AORC as a known function of ERA5-Land: offsets, ratios and no drizzle below 1 mm."""
    a = dict(tmean_c=land['tmean_c'] + 2, tmin_c=land['tmin_c'] + 1, tmax_c=land['tmax_c'] + 3,
             relative_humidity_pct=land['relative_humidity_pct'] - 5,
             shortwave_down_mean_wm2=.9 * land['shortwave_down_mean_wm2'],
             vapor_pressure_deficit_kpa=1.2 * land['vapor_pressure_deficit_kpa'],
             precipitation_mm=np.where(land['precipitation_mm'] < 1, 0., 1.3 * land['precipitation_mm']))
    snow = np.where(land['precipitation_mm'] > 0, land['snowfall_mm'] / land['precipitation_mm'], 0.)
    a['snowfall_mm'] = a['precipitation_mm'] * snow
    a['rainfall_mm'] = a['precipitation_mm'] - a['snowfall_mm']
    a['pet_hargreaves_mm'] = np.array([hargreaves_pet(*s, LATITUDES[p], d) for s, p, d in zip(
        zip(a['tmean_c'], a['tmin_c'], a['tmax_c']), projects, dates)])
    return a


class DebiasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        days = [date(2022, 1, 1) + timedelta(days=i) for i in range((date(2025, 12, 31) - date(2022, 1, 1)).days + 1)]
        self.keys = [(day, project) for day in days for project in sorted(LATITUDES)]
        dates = [day for day, _ in self.keys]
        projects = np.array([project for _, project in self.keys])
        rng = np.random.default_rng(0)
        n = len(self.keys)
        season = np.cos(2 * np.pi * (np.array([d.timetuple().tm_yday for d in dates]) - 200) / 365)
        tmean = 5 + 10 * season + rng.normal(0, 3, n)
        precipitation = rng.gamma(.6, 5, n)
        self.land = dict(tmean_c=tmean, tmin_c=tmean - 4 - np.abs(rng.normal(0, 1, n)),
                         tmax_c=tmean + 4 + np.abs(rng.normal(0, 1, n)),
                         relative_humidity_pct=np.clip(70 - 10 * season + rng.normal(0, 8, n), 6, 100),
                         shortwave_down_mean_wm2=np.clip(150 + 100 * season + rng.normal(0, 20, n), 5, None),
                         vapor_pressure_deficit_kpa=np.clip(.5 + .4 * season + rng.normal(0, .2, n), .01, None),
                         precipitation_mm=precipitation, snowfall_mm=np.where(tmean < 0, precipitation, 0.))
        self.truth = truth(self.land, dates, projects)
        self.dates, self.projects = dates, projects
        overlap = np.array([d.year < 2025 for d in dates])
        aorc = {v: np.where(overlap, values, np.nan) for v, values in self.truth.items()}
        self.gap = self.keys.index(GAP)
        for values in aorc.values():
            values[self.gap] = np.nan
        self.other = {(item['source'], item['variable']): float(i) for i, item in enumerate(REQUESTED)
                      if item['source'] != AORC}
        era5 = {(LAND, v): values for v, values in self.land.items()}
        era5.update({key: np.full(n, value) for key, value in self.other.items()})
        self.aorc_path, self.era5_path = self.root / 'aorc_full.csv', self.root / 'era5.csv'
        write_table(self.aorc_path, self.keys, {(AORC, v): values for v, values in aorc.items()},
                    [dict(metadata=dict(source_id=AORC, pet_centroid_latitudes=LATITUDES))])
        write_table(self.era5_path, self.keys, era5)
        self.output = self.root / 'daily.csv'
        with redirect_stdout(io.StringIO()):
            debias_era5.run(self.aorc_path, self.era5_path, self.output)
        with self.output.open() as stream:
            self.rows = list(csv.DictReader(stream))

    def series(self, variable):
        return np.array([float(row[variable]) if row[variable] else np.nan for row in self.rows])

    def test_aorc_is_kept_and_blanks_are_filled_and_flagged(self):
        names = [item['variable'] for item in REQUESTED if item['source'] == AORC]
        self.assertEqual(list(self.rows[0]), ['date', 'project_id', *[item['variable'] for item in REQUESTED],
                                              'aorc_gap_filled'])
        self.assertEqual(len(self.rows), len(self.keys))
        flagged = np.array([bool(row['aorc_gap_filled']) for row in self.rows])
        expected = np.array([d.year == 2025 for d in self.dates])
        expected[self.gap] = True
        np.testing.assert_array_equal(flagged, expected)
        self.assertEqual(self.rows[self.gap]['aorc_gap_filled'], ';'.join(names))
        for variable in names:
            np.testing.assert_allclose(self.series(variable)[~expected], self.truth[variable][~expected])
        for (_, variable), value in self.other.items():
            self.assertTrue(np.all(self.series(variable) == value), variable)

    def test_filled_values_follow_the_fitted_relation(self):
        filled = np.array([d.year == 2025 for d in self.dates])
        for variable in ('tmean_c', 'tmin_c', 'tmax_c', 'shortwave_down_mean_wm2', 'vapor_pressure_deficit_kpa'):
            np.testing.assert_allclose(self.series(variable)[filled], self.truth[variable][filled],
                                       rtol=1e-6, atol=1e-6, err_msg=variable)
        rain, snow, total = (self.series(v)[filled] for v in ('rainfall_mm', 'snowfall_mm', 'precipitation_mm'))
        np.testing.assert_allclose(rain + snow, total)
        true = self.truth['precipitation_mm'][filled]
        self.assertAlmostEqual(total.mean(), true.mean(), delta=.02 * true.mean())
        self.assertAlmostEqual(np.mean(total > .1), np.mean(true > .1), delta=.02)
        self.assertLess(np.mean(np.abs(total - true)), .05)
        self.assertGreater(np.mean(self.land['precipitation_mm'][filled] > .1), np.mean(true > .1) + .1)
        np.testing.assert_allclose(self.series('pet_hargreaves_mm')[filled],
                                   self.truth['pet_hargreaves_mm'][filled], rtol=1e-6)

    def test_check_compares_held_out_years(self):
        with self.output.with_name('daily_check.csv').open() as stream:
            rows = {(row['variable'], row['project_id']): row for row in csv.DictReader(stream)}
        tmean = rows['tmean_c', 'ALL']
        self.assertAlmostEqual(float(tmean['era5_land_mae']), 2, places=6)
        self.assertLess(float(tmean['corrected_mae']), .01)
        precipitation = rows['precipitation_mm', 'A']
        self.assertLess(abs(float(precipitation['corrected_wet_days']) - float(precipitation['aorc_wet_days'])), .03)
        self.assertGreater(float(precipitation['era5_land_wet_days']), float(precipitation['aorc_wet_days']) + .1)
        self.assertEqual(rows['tmean_c', 'A']['aorc_wet_days'], '')

    def test_short_overlap_is_refused(self):
        short = {(AORC, v): np.where([d < date(2022, 2, 1) for d in self.dates], values, np.nan)
                 for v, values in self.truth.items()}
        write_table(self.aorc_path, self.keys, short,
                    [dict(metadata=dict(source_id=AORC, pet_centroid_latitudes=LATITUDES))])
        with self.assertRaisesRegex(ValueError, 'days with both sources'), redirect_stdout(io.StringIO()):
            debias_era5.run(self.aorc_path, self.era5_path, self.root / 'short.csv')


if __name__ == '__main__':
    unittest.main()
