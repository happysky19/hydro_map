"""Delivery exports preserve source identity, missing observations and provenance."""

import csv
from datetime import date
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from catchment_history.aggregate import DAILY_FIELDS
from catchment_history.export import export_daily


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportDailyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.geojson = self.root / 'catchments.geojson'
        self.geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[
            dict(type='Feature', properties=dict(id=identifier, name=identifier, part='local'),
                 geometry=dict(type='Polygon', coordinates=[[[0, 0], [1, 0], [1, 1], [0, 0]]]))
            for identifier in ('A', 'B')])))
        self.start, self.end = date(2024, 2, 28), date(2024, 3, 1)
        self.output = self.root / 'delivery.csv'
        self.qc_output = self.root / 'delivery_qc.csv'

    def row(self, source='aorc_v1.1', stamp='2024-02-28', project='A', **changes):
        return dict(date=stamp, source=source, project_id=project,
                    variable='precipitation_mm', value='2.5', units='mm',
                    expected_hours='24', valid_hours='24', min_valid_area_fraction='1',
                    qc='valid', **changes)

    def source(self, name='aorc', source='aorc_v1.1'):
        folder = self.root / name
        folder.mkdir()
        run = dict(schema=2, source=source, source_id=source, projects=['A', 'B'], start=str(self.start),
                   end=str(self.end), day='UTC', geometry_sha256=digest(self.geojson),
                   variables=['APCP_surface'], daily_schema={
                       'precipitation_mm': dict(units='mm', statistic='Sum of hourly water-equivalent amounts')})
        (folder / 'run.json').write_text(json.dumps(run))
        return folder

    def period(self, folder, rows, token='2024', **changes):
        daily = folder / f'daily_{token}.csv.gz'
        with gzip.open(daily, 'wt', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=DAILY_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        run = json.loads((folder / 'run.json').read_text())
        record = dict(configuration_sha256=hashlib.sha256(
            json.dumps(run, sort_keys=True).encode()).hexdigest(), daily_sha256=digest(daily),
            daily_rows=len(rows), status='complete', **changes)
        prefix = 'month' if len(token) == 7 else 'year'
        (folder / f'{prefix}_{token}.json').write_text(json.dumps(record))
        return daily

    def export(self, folders, output=None):
        return export_daily(self.geojson, folders, self.start, self.end, output or self.output)

    def read(self, output=None):
        path = output or self.output
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', newline='') as stream:
            return list(csv.DictReader(stream))

    def test_complete_leap_grid_mixed_sources_and_missing_observations(self):
        aorc = self.source()
        missing = self.row(stamp='2024-03-01')
        missing.update(value='', qc='invalid_hours', valid_hours='23', min_valid_area_fraction='0')
        self.period(aorc, [self.row(), missing])
        cds = self.source('cds', 'era5_land_cds')
        self.period(cds, [self.row('era5_land_cds')], '2024-02',
                    start='2024-02-28', end='2024-02-29')
        self.period(cds, [], '2024-03', start='2024-03-01', end='2024-03-01')
        report = self.export([aorc, cds])
        rows = self.read()
        self.assertEqual(list(rows[0]), ['date', 'project_id', 'aorc_v1_1__precipitation_mm',
                                         'era5_land_cds__precipitation_mm'])
        quality = self.read(self.qc_output)
        self.assertEqual([(r['date'], r['project_id']) for r in quality],
                         [(r['date'], r['project_id']) for r in rows])
        self.assertEqual(len(quality[0]), 10)
        self.assertEqual(len(rows), 6)
        self.assertEqual([(r['date'], r['project_id']) for r in rows], [
            ('2024-02-28', 'A'), ('2024-02-28', 'B'), ('2024-02-29', 'A'),
            ('2024-02-29', 'B'), ('2024-03-01', 'A'), ('2024-03-01', 'B')])
        self.assertEqual(rows[0]['aorc_v1_1__precipitation_mm'], '2.5')
        self.assertEqual(rows[0]['era5_land_cds__precipitation_mm'], '2.5')
        self.assertEqual(rows[2]['aorc_v1_1__precipitation_mm'], '')
        self.assertEqual(quality[2]['aorc_v1_1__precipitation_mm__qc'], 'missing_record')
        self.assertEqual(quality[4]['aorc_v1_1__precipitation_mm__qc'], 'invalid_hours')
        self.assertEqual(quality[4]['aorc_v1_1__precipitation_mm__valid_hours'], '23')
        manifest = json.loads(Path(str(self.output) + '.manifest.json').read_text())
        self.assertEqual(report['row_count'], 6)
        self.assertEqual(manifest['output_sha256'], digest(self.output))
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(manifest['qc_file'], 'delivery_qc.csv')
        self.assertEqual(manifest['qc_output_sha256'], digest(self.qc_output))
        self.assertEqual(set(manifest['columns']), set(rows[0]))
        self.assertEqual(set(manifest['qc_columns']), set(quality[0]))
        self.assertEqual(manifest['columns']['aorc_v1_1__precipitation_mm']['units'], 'mm')
        self.assertEqual(manifest['inputs'][0]['metadata'], json.loads((aorc / 'run.json').read_text()))
        self.assertEqual(manifest['catchment_part'], 'local')

    def test_unit_names_preserve_values_and_source_variable_mapping(self):
        fields = [
            ('tmean_c', 'degC', 'tmean_degC', -1.25),
            ('wind_speed_ms', 'm/s', 'wind_speed_m_s', 5.),
            ('specific_humidity_kgkg', 'kg/kg', 'specific_humidity_kg_kg', .006),
            ('surface_pressure_pa', 'Pa', 'surface_pressure_Pa', 90000.),
            ('vapor_pressure_deficit_kpa', 'kPa', 'vapor_pressure_deficit_kPa', .5),
            ('shortwave_down_mean_wm2', 'W/m2', 'shortwave_down_mean_W_m2', 100.),
            ('net_radiation_energy_mjm2', 'MJ/m2', 'net_radiation_energy_MJ_m2', 8.64),
            ('snow_density_kgm3', 'kg/m3', 'snow_density_kg_m3', 250.),
            ('soil_moisture_layer1_m3m3', 'm3/m3', 'soil_moisture_layer1_m3_m3', .25),
            ('relative_humidity_pct', '%', 'relative_humidity_pct', 80.),
            ('cloud_cover_fraction', '1', 'cloud_cover_fraction', .5),
            ('snow_depth_m', 'm', 'snow_depth_m', .8),
            ('precipitation_mm', 'mm', 'precipitation_mm', 0.),
            ('pet_hargreaves_mm', 'mm/day', 'pet_hargreaves_mm_day', 2.),
        ]
        folder = self.source('fields', 'example')
        path = folder/'run.json'
        run = json.loads(path.read_text())
        run['daily_schema'] = {name: dict(units=units, statistic='mean') for name, units, _, _ in fields}
        path.write_text(json.dumps(run))
        observations = []
        for name, units, _, value in fields:
            row = self.row('example')
            row.update(variable=name, units=units, value=str(value))
            observations.append(row)
        self.period(folder, observations)
        report = self.export([folder])
        values = self.read()[0]
        for name, units, exported, value in fields:
            with self.subTest(variable=name):
                column = f'example__{exported}'
                self.assertIn(column, values)
                self.assertEqual(float(values[column]), value)
                self.assertEqual(report['columns'][column]['variable'], name)
                self.assertEqual(report['columns'][column]['units'], units)

    def test_missing_period_is_not_confused_with_missing_observations(self):
        folder = self.source()
        self.period(folder, [self.row()], '2024-02')
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.export([folder])

    def test_hash_failure_preserves_existing_output_and_manifest(self):
        folder = self.source()
        daily = self.period(folder, [self.row()])
        daily.write_bytes(daily.read_bytes() + b'changed')
        self.output.write_text('keep data')
        self.qc_output.write_text('keep quality')
        companion = Path(str(self.output) + '.manifest.json')
        companion.write_text('keep metadata')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.export([folder])
        self.assertEqual(self.output.read_text(), 'keep data')
        self.assertEqual(self.qc_output.read_text(), 'keep quality')
        self.assertEqual(companion.read_text(), 'keep metadata')

    def test_duplicate_observations_are_rejected(self):
        folder = self.source()
        self.period(folder, [self.row(), self.row()])
        with self.assertRaisesRegex(ValueError, '[Dd]uplicate'):
            self.export([folder])

    def test_configuration_hash_mismatch_is_rejected(self):
        folder = self.source()
        self.period(folder, [self.row()])
        path = folder / 'year_2024.json'
        record = json.loads(path.read_text())
        record['configuration_sha256'] = '0' * 64
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'configuration hash'):
            self.export([folder])

    def test_absent_declared_series_retains_column_and_missing_qc(self):
        folder = self.source()
        run = json.loads((folder / 'run.json').read_text())
        run['daily_schema']['tmean_c'] = dict(units='degC', statistic='mean')
        (folder / 'run.json').write_text(json.dumps(run))
        self.period(folder, [self.row()])
        self.export([folder])
        self.assertTrue(all(row['aorc_v1_1__tmean_degC'] == '' for row in self.read()))
        self.assertTrue(all(row['aorc_v1_1__tmean_degC__qc'] == 'missing_record'
                            for row in self.read(self.qc_output)))

    def test_invalid_qc_cannot_hide_a_numeric_value(self):
        folder = self.source()
        row = self.row()
        row.update(qc='invalid_hours', valid_hours='23')
        self.period(folder, [row])
        with self.assertRaisesRegex(ValueError, 'QC|qc'):
            self.export([folder])
        self.assertFalse(self.output.exists())

    def test_geometry_hash_and_missing_project_are_rejected(self):
        folder = self.source()
        original = json.loads((folder / 'run.json').read_text())
        for field, value in [('geometry_sha256', '0' * 64), ('projects', ['A'])]:
            run = dict(original, **{field: value})
            (folder / 'run.json').write_text(json.dumps(run))
            self.period(folder, [self.row()])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.export([folder])

    def test_generated_cds_monthly_tables_export_together(self):
        from catchment_history.cds import run_pipeline
        from test_cds import FakeClient

        doc = json.loads(self.geojson.read_text())
        doc['features'] = doc['features'][:1]
        doc['features'][0]['geometry']['coordinates'] = [
            [[-120.04, 49.96], [-119.96, 49.96], [-119.96, 50.04],
             [-120.04, 50.04], [-120.04, 49.96]]]
        self.geojson.write_text(json.dumps(doc))
        stamp = date(2025, 12, 31)
        folders = []
        for product in ('era5-land', 'era5'):
            output = self.root / product
            run_pipeline(self.geojson, product, output, self.root / 'cache', stamp, stamp,
                         client=FakeClient())
            folders.append(output)
        export_daily(self.geojson, folders, stamp, stamp, self.output)
        rows = self.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 45)
        self.assertEqual(len(self.read(self.qc_output)[0]), 174)
        self.assertAlmostEqual(float(rows[0]['era5_land_cds__snow_water_equivalent_mm']), 200)
        self.assertAlmostEqual(float(rows[0]['era5_cds__cloud_cover_fraction']), .5)

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'optional pyarrow unavailable')
    def test_parquet_export_retains_typed_nulls(self):
        import pyarrow.parquet as pq
        folder = self.source()
        self.period(folder, [self.row()])
        output = self.root / 'delivery.parquet'
        self.export([folder], output)
        rows = pq.read_table(output).to_pylist()
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['date'], self.start)
        self.assertEqual(rows[0]['aorc_v1_1__precipitation_mm'], 2.5)
        self.assertIsNone(rows[1]['aorc_v1_1__precipitation_mm'])
        quality = pq.read_table(self.root/'delivery_qc.parquet').to_pylist()
        self.assertEqual(quality[0]['date'], rows[0]['date'])
        self.assertEqual(quality[1]['aorc_v1_1__precipitation_mm__qc'], 'missing_record')
        self.assertEqual(quality[0]['aorc_v1_1__precipitation_mm__valid_hours'], 24)
        self.assertIsInstance(quality[0]['aorc_v1_1__precipitation_mm__min_valid_area_fraction'], float)


if __name__ == '__main__':
    unittest.main()
