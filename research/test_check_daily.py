"""Daily diagnostics distinguish identities, missing data and source disagreement."""

import csv
from datetime import date, timedelta
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from export_daily import _value_column, digest


class CheckDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'daily.csv'
        self.columns = {'date': {}, 'project_id': {}}
        self.values = {}
        for source, fields in {
            'aorc_v1.1': {
                'precipitation_mm': ('mm', 3), 'rainfall_mm': ('mm', 1),
                'snowfall_mm': ('mm', 2), 'tmin_c': ('degC', -12),
                'tmean_c': ('degC', -9), 'tmax_c': ('degC', -6),
                'u_wind_ms': ('m/s', .6), 'v_wind_ms': ('m/s', .2),
                'wind_speed_ms': ('m/s', 1.5),
                'longwave_down_mean_wm2': ('W/m2', 179.3),
                'longwave_down_energy_mjm2': ('MJ/m2', 179.3*.0864),
                'pet_hargreaves_mm': ('mm/day', .15)},
            'era5_land_cds': {
                'precipitation_mm': ('mm', .4), 'tmean_c': ('degC', -8),
                'actual_evapotranspiration_mm': ('mm', .6),
                'net_shortwave_energy_mjm2': ('MJ/m2', 3.4),
                'net_longwave_energy_mjm2': ('MJ/m2', -5.5),
                'net_radiation_energy_mjm2': ('MJ/m2', -2.1),
                'net_radiation_mean_wm2': ('W/m2', -2.1/.0864),
                'soil_moisture_layer1_m3m3': ('m3/m3', .2),
                'soil_moisture_layer2_m3m3': ('m3/m3', .3),
                'soil_moisture_layer3_m3m3': ('m3/m3', .4),
                'root_zone_soil_moisture_0_100cm_m3m3': ('m3/m3', .365)},
            'era5_cds': {'freezing_level_above_terrain_m': ('m', None)},
        }.items():
            for variable, (units, value) in fields.items():
                column = _value_column(source, variable, units)
                self.columns[column] = dict(source=source, variable=variable, units=units)
                self.values[column] = value
        self.rows, self.quality = [], []
        for day in ('2025-12-28', '2025-12-29', '2025-12-30'):
            self.rows.append(dict(date=day, project_id='A', **self.values))
            qc = dict(date=day, project_id='A')
            for column, value in self.values.items():
                qc.update({column+'__qc': 'no_crossing' if value is None else 'valid',
                           column+'__valid_hours': 0 if value is None else 24,
                           column+'__expected_hours': 24,
                           column+'__min_valid_area_fraction': 0 if value is None else 1})
            self.quality.append(qc)
        self.save()

    def save(self):
        qc_path = self.path.with_name('daily_qc.csv')
        for path, rows in ((self.path, self.rows), (qc_path, self.quality)):
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        manifest = dict(schema_version=2, day='UTC', start='2025-12-28', end='2025-12-30',
                        row_count=3, project_count=1, project_metadata={'A': {'name': 'Alpine'}},
                        columns=self.columns, qc_columns={key: {} for key in self.quality[0]},
                        qc_file=qc_path.name, output_sha256=digest(self.path),
                        qc_output_sha256=digest(qc_path))
        Path(str(self.path)+'.manifest.json').write_text(json.dumps(manifest))

    def check(self, plots=False):
        self.assertIsNotNone(importlib.util.find_spec('check_daily'), 'Daily checker is missing')
        from check_daily import check_file
        return check_file(self.path, plots=plots)

    def test_valid_physics_missing_freezing_and_cross_source_statistics(self):
        report = self.check(plots=True)
        self.assertEqual(report['failed_checks'], 0)
        self.assertEqual(report['missing_values'], 3)
        comparisons = report['comparisons']
        precip = next(row for row in comparisons
                      if row['project_id'] == 'A' and row['variable'] == 'precipitation_mm')
        self.assertAlmostEqual(precip['bias_land_minus_aorc'], -2.6)
        self.assertAlmostEqual(precip['rmse'], 2.6)
        self.assertIsNone(precip['correlation'])
        self.assertEqual(precip['paired_days'], 3)
        folder = Path(str(self.path)+'.checks')
        for name in ('coverage.png', 'source_comparison.png', 'timeseries.pdf',
                     'summary.json', 'checks.csv', 'variables.csv', 'qc_summary.csv'):
            self.assertTrue((folder/name).is_file(), name)
        with (folder/'qc_summary.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertTrue(any(r['qc'] == 'no_crossing' and r['days'] == '3' for r in rows))

    def test_freezing_diagnostics_distinguish_partial_support_from_missing_profiles(self):
        column = 'era5_cds__freezing_level_above_terrain_m'
        self.quality[0][column+'__valid_hours'] = 23
        self.quality[0][column+'__min_valid_area_fraction'] = .99
        self.quality[1][column+'__qc'] = 'missing_profile;no_crossing'
        self.save()
        from contextlib import redirect_stdout
        import io
        text = io.StringIO()
        with redirect_stdout(text):
            report = self.check()
        diagnostic = report['freezing_level'][column]
        self.assertEqual(diagnostic['valid_project_days'], 0)
        self.assertEqual(diagnostic['project_days'], 3)
        self.assertEqual(diagnostic['valid_hours_range'], [0, 23])
        self.assertEqual(diagnostic['min_valid_area_fraction_range'], [0, .99])
        self.assertEqual(diagnostic['qc_counts'], {'no_crossing': 2, 'missing_profile;no_crossing': 1})
        self.assertIn('No usable freezing-level values', text.getvalue())
        self.assertIn('missing_profile;no_crossing=1', text.getvalue())

    def test_changed_values_fail_identities_without_calling_missing_values_zero(self):
        self.rows[0]['aorc_v1_1__snowfall_mm'] = 10
        self.rows[1]['aorc_v1_1__longwave_down_energy_MJ_m2'] = 179.3*.0036
        self.rows[2]['aorc_v1_1__wind_speed_m_s'] = .1
        self.rows[2]['era5_land_cds__root_zone_soil_moisture_0_100cm_m3_m3'] = .5
        self.save()
        report = self.check()
        failed = {row['check'] for row in report['checks'] if row['failed']}
        self.assertEqual(failed, {'aorc_v1.1: rain + snow = precipitation',
                                 'aorc_v1.1: longwave energy = mean flux * 0.0864',
                                 'aorc_v1.1: mean speed >= magnitude of mean vector',
                                 'era5_land_cds: root-zone thickness weighting'})

    def test_hash_and_row_alignment_errors_stop_the_check(self):
        self.path.write_text(self.path.read_text()+'\n')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.check()
        self.quality[0]['date'] = '2025-12-29'
        self.save()
        with self.assertRaisesRegex(ValueError, 'keys|order|align'):
            self.check()

    def test_qc_value_disagreement_is_reported(self):
        column = 'era5_cds__freezing_level_above_terrain_m'
        self.rows[0][column] = 0
        self.quality[1]['aorc_v1_1__precipitation_mm__valid_hours'] = 23
        self.save()
        report = self.check()
        failures = [row for row in report['checks'] if row['failed']]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['check'], 'QC/value agreement')
        self.assertEqual(failures[0]['failed'], 2)

    def test_gzip_and_parquet_keep_values_and_qc_alignment(self):
        baseline = self.check()
        manifest = json.loads(Path(str(self.path)+'.manifest.json').read_text())
        from check_daily import check_file
        for suffix in ('.csv.gz', '.parquet'):
            if suffix == '.parquet' and importlib.util.find_spec('pyarrow') is None:
                continue
            with self.subTest(suffix=suffix):
                path = self.path.with_name('compressed'+suffix)
                qc = self.path.with_name('compressed_qc'+suffix)
                for output, rows in ((path, self.rows), (qc, self.quality)):
                    if suffix == '.parquet':
                        import pyarrow as pa
                        import pyarrow.parquet as pq
                        pq.write_table(pa.Table.from_pylist(rows), output)
                    else:
                        with gzip.open(output, 'wt', newline='') as stream:
                            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                            writer.writeheader()
                            writer.writerows(rows)
                record = dict(manifest, qc_file=qc.name, output_sha256=digest(path),
                              qc_output_sha256=digest(qc))
                Path(str(path)+'.manifest.json').write_text(json.dumps(record))
                result = check_file(path, plots=False)
                self.assertEqual(result['checks'], baseline['checks'])
                self.assertEqual(result['comparisons'], baseline['comparisons'])

    def test_long_window_plots_handle_all_missing_series(self):
        start = date(2024, 1, 1)
        row, qc = self.rows[0], self.quality[0]
        self.rows = [dict(row, date=str(start+timedelta(days=i))) for i in range(400)]
        self.quality = [dict(qc, date=r['date']) for r in self.rows]
        self.save()
        path = Path(str(self.path)+'.manifest.json')
        metadata = json.loads(path.read_text())
        metadata.update(start=str(start), end=self.rows[-1]['date'], row_count=400)
        path.write_text(json.dumps(metadata))
        report = self.check(plots=True)
        self.assertEqual(report['missing_values'], 400)
        self.assertEqual(report['failed_checks'], 0)


if __name__ == '__main__':
    unittest.main()
