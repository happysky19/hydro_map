"""One-command orchestration, failure handling and routing-note retention."""

from contextlib import ExitStack, redirect_stdout
import csv
from datetime import date
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import netCDF4
import numpy as np

import download_daily
from download_aorc import FIELDS, requested_hours
from meteorology import DERIVED_FIELDS
from test_download_cds import FakeClient


class DownloadDailyTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = download_daily
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.geojson = self.root / 'catchments.geojson'
        self.geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[dict(
            type='Feature', properties=dict(id='A', name='A', part='local',
                catchment_role='natural_reach_at_tailrace', diversion_intake_project='UPSTREAM',
                routing_requires_operations=True), geometry=dict(type='Polygon', coordinates=[[
                    [-120.03, 49.97], [-119.97, 49.97], [-119.97, 50.03],
                    [-120.03, 50.03], [-120.03, 49.97]]]))])))
        self.output = self.root / 'daily.csv'
        self.day = date(2025, 12, 31)

    def native_inputs(self, store, metas, times, blocks, variables, start, end, *unused):
        definitions = {name: (units, kind) for name, units, kind in FIELDS.values()}
        definitions.update(DERIVED_FIELDS)
        values = dict(temperature_c=5., precipitation_mm=1., rainfall_mm=.75, snowfall_mm=.25,
                      wind_speed_ms=5., relative_humidity_pct=80.)
        result = {}
        for name, (units, kind) in definitions.items():
            stamps = requested_hours(start, end, kind == 'hour_ending_amount')
            result[name] = (stamps, {'A': np.full(len(stamps), values.get(name, 1.))},
                            {'A': np.ones(len(stamps))}, units, kind)
        return result

    def aorc_inputs(self, stack):
        stack.enter_context(patch('download_aorc.metadata', return_value={}))
        stack.enter_context(patch('download_aorc.axis', side_effect=lambda store, year, name, *args:
            requested_hours(date(year, 1, 1), date(year, 12, 31), False)
            if name == 'time' else np.array([0., 1.])))
        stack.enter_context(patch('download_aorc.weights', return_value={
            'A': (np.array([0]), np.array([0]), np.array([1.]))}))
        stack.enter_context(patch('download_aorc.extract_native_series', side_effect=self.native_inputs))
        stack.enter_context(patch('shutil.which', return_value='zstd'))

    def test_three_sources_export_and_resume_without_retrieving_again(self):
        stream = io.StringIO()
        client = FakeClient()
        with ExitStack() as stack, redirect_stdout(stream):
            self.aorc_inputs(stack)
            report = self.pipeline.run(self.geojson, self.day, self.day, self.output, cds_client=client)
            with patch('download_aorc.extract_native_series', side_effect=AssertionError('Re-read AORC')), \
                 patch.object(client, 'retrieve', side_effect=AssertionError('Re-read CDS')):
                self.pipeline.run(self.geojson, self.day, self.day, self.output, cds_client=client)
        with self.output.open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 227)
        self.assertEqual(float(rows[0]['aorc_v1_1__precipitation_mm']), 24.)
        self.assertEqual(float(rows[0]['era5_land_cds__precipitation_mm']), 2.)
        self.assertEqual(float(rows[0]['era5_cds__cloud_cover_fraction']), .5)
        self.assertEqual(report['project_count'], 1)
        self.assertNotIn('Turbine inflow', stream.getvalue())
        for item in report['inputs']:
            self.assertTrue(item['metadata']['warnings'])
            self.assertTrue(item['metadata']['project_forcing']['A']['routing_requires_operations'])

    def test_missing_credentials_fail_before_source_downloads(self):
        with patch('shutil.which', return_value='zstd'), \
             patch('cdsapi.Client', side_effect=OSError('Missing CDS configuration')), \
             patch('download_aorc.run', side_effect=AssertionError('AORC started before credential check')):
            with self.assertRaisesRegex(OSError, 'CDS configuration'):
                self.pipeline.run(self.geojson, self.day, self.day, self.output)
        self.assertFalse(self.output.exists())

    def test_failed_grid_check_resumes_aorc_and_cached_land_after_code_update(self):
        client = FakeClient()
        retrieve = client.retrieve
        def response_with_roundoff(dataset, request, target):
            retrieve(dataset, request, target)
            if dataset == 'reanalysis-era5-land' and len(request['time']) == 1:
                with netCDF4.Dataset(target, 'a') as ds:
                    ds['longitude'][:] += 1e-12
        exact_grid = lambda reference, current: all(np.array_equal(a, b) for a, b in zip(reference, current))
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            self.aorc_inputs(stack)
            with patch.object(client, 'retrieve', side_effect=response_with_roundoff), \
                 patch('download_cds._same_grid', side_effect=exact_grid):
                with self.assertRaisesRegex(ValueError, 'CDS grid mismatch'):
                    self.pipeline.run(self.geojson, self.day, self.day, self.output, cds_client=client)
            self.assertEqual(client.calls, 2)
            self.assertFalse(self.output.exists())
            work = Path(str(self.output)+'.work')
            run_path = work/'era5-land/run.json'
            previous = json.loads(run_path.read_text())
            previous['code_sha256']['download_cds.py'] = 'old-processing-code'
            previous['methods'].pop('grid_coordinates')
            run_path.write_text(json.dumps(previous))
            def no_repeated_land_download(dataset, request, target):
                self.assertNotEqual(dataset, 'reanalysis-era5-land')
                retrieve(dataset, request, target)
            with patch('download_aorc.extract_native_series', side_effect=AssertionError('Re-read AORC')), \
                 patch.object(client, 'retrieve', side_effect=no_repeated_land_download):
                report = self.pipeline.run(self.geojson, self.day, self.day, self.output, cds_client=client)
        self.assertEqual(client.calls, 4)
        self.assertEqual(report['row_count'], 1)
        self.assertFalse((work/'era5-land/grid_mismatch.json').exists())
        with self.output.open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(float(row['era5_land_cds__precipitation_mm']), 2.)
        self.assertEqual(float(row['era5_cds__cloud_cover_fraction']), .5)

    def test_cli_needs_only_geometry_and_dates(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            self.aorc_inputs(stack)
            stack.enter_context(patch('cdsapi.Client', return_value=FakeClient()))
            stack.enter_context(patch.object(sys, 'argv', ['download_daily.py',
                '--geojson', str(self.geojson), '--start', str(self.day), '--end', str(self.day)]))
            previous = Path.cwd()
            stack.callback(os.chdir, previous)
            os.chdir(self.root)
            self.assertEqual(self.pipeline.main(), 0)
        path = self.root / 'outputs/catchment_daily_2025-12-31_2025-12-31.csv'
        with path.open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['project_id'], 'A')
        self.assertEqual(len(row), 227)

    def test_source_failure_preserves_previous_delivery(self):
        self.output.write_text('previous delivery\n')
        companion = Path(str(self.output) + '.manifest.json')
        companion.write_text('previous manifest\n')
        with ExitStack() as stack:
            self.aorc_inputs(stack)
            client = FakeClient()
            with patch.object(client, 'retrieve', side_effect=RuntimeError('Source unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'Source unavailable'):
                    self.pipeline.run(self.geojson, self.day, self.day, self.output, cds_client=client)
        self.assertEqual(self.output.read_text(), 'previous delivery\n')
        self.assertEqual(companion.read_text(), 'previous manifest\n')

    def test_invalid_window_and_nonlocal_geometry_rejected_before_network(self):
        with patch('cdsapi.Client', side_effect=AssertionError('Network setup started')):
            with self.assertRaisesRegex(ValueError, 'start|Start'):
                self.pipeline.run(self.geojson, date(2026, 1, 1), self.day, self.output)
            document = json.loads(self.geojson.read_text())
            document['features'][0]['properties']['part'] = 'total'
            self.geojson.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'local'):
                self.pipeline.run(self.geojson, self.day, self.day, self.output)


if __name__ == '__main__':
    unittest.main()
