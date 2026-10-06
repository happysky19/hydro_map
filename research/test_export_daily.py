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
from unittest.mock import patch

from aggregate_daily import DAILY_FIELDS
from export_daily import export_daily


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

    def row(self, source='aorc_v1.1', stamp='2024-02-28', project='A', **changes):
        return dict(date=stamp, source=source, project_id=project,
                    variable='precipitation_mm', value='2.5', units='mm',
                    expected_hours='24', valid_hours='24', min_valid_area_fraction='1',
                    qc='valid', **changes)

    def source(self, name='aorc', source='aorc_v1.1', legacy=False):
        folder = self.root / name
        folder.mkdir()
        run = dict(schema=2, source='https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/'
                   if legacy else source, projects=['A', 'B'], start=str(self.start),
                   end=str(self.end), day='UTC', geometry_sha256=digest(self.geojson),
                   variables=['APCP_surface'])
        if not legacy:
            run.update(source_id=source, daily_schema={
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
        aorc = self.source(legacy=True)
        missing = self.row(stamp='2024-03-01')
        missing.update(value='', qc='invalid_hours', valid_hours='23', min_valid_area_fraction='0')
        self.period(aorc, [self.row(), missing])
        cds = self.source('cds', 'era5_land_cds')
        self.period(cds, [self.row('era5_land_cds')], '2024-02',
                    start='2024-02-28', end='2024-02-29')
        self.period(cds, [], '2024-03', start='2024-03-01', end='2024-03-01')
        report = self.export([aorc, cds])
        rows = self.read()
        self.assertEqual(len(rows), 6)
        self.assertEqual([(r['date'], r['project_id']) for r in rows], [
            ('2024-02-28', 'A'), ('2024-02-28', 'B'), ('2024-02-29', 'A'),
            ('2024-02-29', 'B'), ('2024-03-01', 'A'), ('2024-03-01', 'B')])
        self.assertEqual(rows[0]['aorc_v1_1__precipitation_mm'], '2.5')
        self.assertEqual(rows[0]['era5_land_cds__precipitation_mm'], '2.5')
        self.assertEqual(rows[2]['aorc_v1_1__precipitation_mm'], '')
        self.assertEqual(rows[2]['aorc_v1_1__precipitation_mm__qc'], 'missing_record')
        self.assertEqual(rows[4]['aorc_v1_1__precipitation_mm__qc'], 'invalid_hours')
        self.assertEqual(rows[4]['aorc_v1_1__precipitation_mm__valid_hours'], '23')
        manifest = json.loads(Path(str(self.output) + '.manifest.json').read_text())
        self.assertEqual(report['row_count'], 6)
        self.assertEqual(manifest['output_sha256'], digest(self.output))
        self.assertEqual(manifest['columns']['aorc_v1_1__precipitation_mm']['units'], 'mm')
        self.assertEqual(manifest['inputs'][0]['metadata'], json.loads((aorc / 'run.json').read_text()))
        self.assertEqual(manifest['catchment_part'], 'local')

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
        companion = Path(str(self.output) + '.manifest.json')
        companion.write_text('keep metadata')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.export([folder])
        self.assertEqual(self.output.read_text(), 'keep data')
        self.assertEqual(companion.read_text(), 'keep metadata')

    def test_duplicate_observations_are_rejected(self):
        folder = self.source()
        self.period(folder, [self.row(), self.row()])
        with self.assertRaisesRegex(ValueError, '[Dd]uplicate'):
            self.export([folder])

    def test_sanitized_source_collision_is_rejected(self):
        first = self.source('first', 'aorc.v1')
        second = self.source('second', 'aorc-v1')
        self.period(first, [self.row('aorc.v1')])
        self.period(second, [self.row('aorc-v1')])
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.export([first, second])

    def test_source_prefixes_stay_distinct_even_for_disjoint_variables(self):
        first = self.source('first', 'aorc.v1')
        second = self.source('second', 'aorc-v1')
        run = json.loads((second / 'run.json').read_text())
        run['daily_schema'] = {'tmean_c': dict(units='degC', statistic='mean')}
        (second / 'run.json').write_text(json.dumps(run))
        row = self.row('aorc-v1')
        row.update(variable='tmean_c', units='degC')
        self.period(first, [self.row('aorc.v1')])
        self.period(second, [row])
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.export([first, second])

    def test_configuration_hash_mismatch_is_rejected(self):
        folder = self.source()
        self.period(folder, [self.row()])
        path = folder / 'year_2024.json'
        record = json.loads(path.read_text())
        record['configuration_sha256'] = '0' * 64
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'configuration hash'):
            self.export([folder])

    def test_cds_manifest_row_count_is_verified(self):
        folder = self.source('cds', 'era5_cds')
        self.period(folder, [self.row('era5_cds')])
        path = folder / 'year_2024.json'
        record = json.loads(path.read_text())
        del record['daily_rows']
        record['rows'] = 2
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'row count'):
            self.export([folder])

    def test_absent_declared_series_retains_column_and_missing_qc(self):
        folder = self.source()
        run = json.loads((folder / 'run.json').read_text())
        run['daily_schema']['tmean_c'] = dict(units='degC', statistic='mean')
        (folder / 'run.json').write_text(json.dumps(run))
        self.period(folder, [self.row()])
        self.export([folder])
        self.assertTrue(all(row['aorc_v1_1__tmean_c'] == '' and
                            row['aorc_v1_1__tmean_c__qc'] == 'missing_record' for row in self.read()))

    def test_manifest_directory_conflict_preserves_existing_output(self):
        folder = self.source()
        self.period(folder, [self.row()])
        self.output.write_text('keep data')
        Path(str(self.output) + '.manifest.json').mkdir()
        with self.assertRaises((ValueError, OSError)):
            self.export([folder])
        self.assertEqual(self.output.read_text(), 'keep data')

    def test_manifest_publish_failure_rolls_back_the_data_file(self):
        folder = self.source()
        self.period(folder, [self.row()])
        self.output.write_text('keep data')
        companion = Path(str(self.output) + '.manifest.json')
        companion.write_text('keep metadata')
        replace = Path.replace

        def fail_manifest(path, target):
            if target == companion:
                raise OSError('manifest publication interrupted')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_manifest), self.assertRaisesRegex(OSError, 'interrupted'):
            self.export([folder])
        self.assertEqual(self.output.read_text(), 'keep data')
        self.assertEqual(companion.read_text(), 'keep metadata')

    def test_invalid_qc_cannot_hide_a_numeric_value(self):
        folder = self.source()
        row = self.row()
        row.update(qc='invalid_hours', valid_hours='23')
        self.period(folder, [row])
        with self.assertRaisesRegex(ValueError, 'QC|qc'):
            self.export([folder])
        self.assertFalse(self.output.exists())

    def test_unit_conflict_and_unidentified_source_are_rejected(self):
        folder = self.source()
        for field, value in [('units', 'K'), ('source', 'unidentified')]:
            row = self.row()
            row[field] = value
            self.period(folder, [row])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.export([folder])

    def test_geometry_hash_and_missing_project_are_rejected(self):
        folder = self.source()
        original = json.loads((folder / 'run.json').read_text())
        for field, value in [('geometry_sha256', '0' * 64), ('projects', ['A'])]:
            run = dict(original, **{field: value})
            (folder / 'run.json').write_text(json.dumps(run))
            self.period(folder, [self.row()])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.export([folder])

    def test_unmanifested_daily_and_input_overwrite_are_rejected(self):
        folder = self.source()
        daily = self.period(folder, [self.row()])
        before = daily.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite|input'):
            self.export([folder], daily)
        self.assertEqual(daily.read_bytes(), before)
        (folder / 'year_2024.json').unlink()
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.export([folder])

    def test_gzip_export_has_same_rows(self):
        folder = self.source()
        self.period(folder, [self.row()])
        output = self.root / 'delivery.csv.gz'
        self.export([folder], output)
        self.assertEqual(len(self.read(output)), 6)

    def test_generated_cds_monthly_tables_export_together(self):
        from download_cds import run_pipeline
        from test_download_cds import FakeClient

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
        self.assertEqual(len(rows[0]), 132)
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


if __name__ == '__main__':
    unittest.main()
