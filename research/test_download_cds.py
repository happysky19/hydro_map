"""Checks for CDS units, UTC endpoints, profile crossings and verified resumes."""

import csv
from datetime import date
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import netCDF4
import numpy as np

from cds_fields import (freezing_level, root_zone_moisture, land_daily_fields, era_daily_fields,
                        ALL_FIELDS, PRESSURE_LEVELS)
from download_cds import make_requests, read_response, process_day, run_pipeline, align_hours


class FieldTests(unittest.TestCase):
    def test_one_percent_area_without_crossing_for_one_hour_masks_both_daily_heights(self):
        levels = np.array([1000, 900, 800])
        profile_t = np.broadcast_to(np.array([280., 274., 268.])[None, :, None, None],
                                    (24, 3, 1, 2)).copy()
        profile_z = np.broadcast_to(np.array([0., 1000., 2000.])[None, :, None, None],
                                    profile_t.shape)*9.80665
        profile_t[0, :, 0, 1] = [270., 264., 258.]
        surface = dict(sp=np.full((24, 1, 2), 101000.), z=np.full((24, 1, 2), -9.80665),
                       tcc=np.full((24, 1, 2), .5))
        fields = era_daily_fields(surface, dict(t=profile_t, z=profile_z), levels)
        rows = process_day(fields, {'A': np.array([[.99, .01]])}, date(2025, 12, 28), 'era5_cds')
        heights = [row for row in rows if row['variable'].startswith('freezing_level')]
        self.assertEqual(len(heights), 2)
        for row in heights:
            self.assertEqual(row['value'], '')
            self.assertEqual(row['valid_hours'], 23)
            self.assertAlmostEqual(row['min_valid_area_fraction'], .99)
            self.assertEqual(row['qc'], 'no_crossing')
        self.assertEqual(next(r for r in rows if r['variable'] == 'cloud_cover_fraction')['value'], .5)

    def test_soil_and_snow_units(self):
        arrays = {f'swvl{i}': np.ones((24, 2, 2)) * i / 10 for i in range(1, 5)}
        self.assertAlmostEqual(root_zone_moisture(arrays)[0, 0, 0], .265)
        arrays.update(t2m=np.ones((24, 2, 2))*273.15, sd=np.ones((24, 2, 2))*.2,
                      sde=np.ones((24, 2, 2))*.8, rsn=np.ones((24, 2, 2))*250,
                      snowc=np.ones((24, 2, 2))*100)
        arrays.update({f'stl{i}': np.ones((24, 2, 2))*280 for i in range(1, 5)})
        accum = {key: np.ones((2, 2)) for key in ['tp', 'smlt', 'e', 'ssr', 'str']}
        fields = land_daily_fields(arrays, accum)
        self.assertEqual(fields['snow_water_equivalent_mm'][0][0, 0, 0], 200)
        self.assertEqual(fields['snow_depth_m'][0][0, 0, 0], .8)
        self.assertEqual(fields['actual_evapotranspiration_mm'][0][0, 0], -1000)
        self.assertEqual(fields['net_radiation_energy_mjm2'][0][0, 0], 2e-6)

    def test_freezing_crossing_above_terrain_and_multiple(self):
        pressure = np.array([1000, 900, 800, 700, 600])
        heights = np.array([0, 1000, 2000, 3000, 4000.])[:, None, None]
        temperatures = np.array([285, 279, 273, 267, 261.])[:, None, None]
        height, agl, flags = freezing_level(temperatures, heights*9.80665,
                                            pressure, np.array([[95000]]), np.array([[500*9.80665]]))
        self.assertAlmostEqual(height[0, 0], 1975.)
        self.assertAlmostEqual(agl[0, 0], 1475.)
        self.assertEqual(flags[0, 0], 'valid')
        temperatures[:, 0, 0] = [280, 270, 280, 270, 260]
        height, _, flags = freezing_level(temperatures, heights*9.80665,
                                            pressure, np.array([[101000]]), np.array([[-10*9.80665]]))
        self.assertTrue(np.isnan(height[0, 0]))
        self.assertEqual(flags[0, 0], 'multiple_crossings')

    def test_no_extrapolation_and_below_ground(self):
        levels = np.array([1000, 900, 800])
        temp = np.array([280, 270, 260.])[:, None, None]
        z = np.array([0, 1000, 2000.])[:, None, None]*9.80665
        result = freezing_level(temp, z, levels, np.array([[85000]]), np.array([[1500*9.80665]]))
        self.assertTrue(np.isnan(result[0][0, 0]))
        self.assertEqual(result[2][0, 0], 'no_crossing')

    def test_inversion_and_missing_profile_are_not_bridged(self):
        levels = np.array([1000, 900, 800])
        temp = np.array([280, 270, 280.])[:, None, None]
        z = np.array([0, 1000, 2000.])[:, None, None]*9.80665
        args = (levels, np.array([[101000]]), np.array([[-10*9.80665]]))
        result = freezing_level(temp, z, *args)
        self.assertTrue(np.isnan(result[0][0, 0]))
        self.assertEqual(result[2][0, 0], 'multiple_crossings')
        temp[1] = np.nan
        self.assertEqual(freezing_level(temp, z, *args)[2][0, 0], 'missing_profile')

    def test_zero_plateau_uses_first_zero_height(self):
        levels = np.array([1000, 900, 800, 700])
        temp = np.array([280, 273.15, 273.15, 270.])[:, None, None]
        z = np.array([0, 1000, 2000, 3000.])[:, None, None]*9.80665
        result = freezing_level(temp, z, levels, np.array([[101000]]), np.array([[-10*9.80665]]))
        self.assertEqual(result[0][0, 0], 1000)

    def test_december_endpoint_request(self):
        requests = make_requests('era5-land', date(2025, 12, 31), [51, -121, 49, -119])
        endpoint = next(r for r in requests if r['kind'] == 'accumulated')
        self.assertEqual(endpoint['request']['year'], '2026')
        self.assertEqual(endpoint['request']['month'], '01')
        self.assertEqual(endpoint['request']['day'], ['01'])
        self.assertEqual(endpoint['request']['time'], ['00:00'])

    def test_batched_endpoint_dates_do_not_form_cartesian_year_months(self):
        requests = make_requests('era5-land', date(2025, 12, 29), [51, -121, 49, -119], date(2025, 12, 31))
        endpoints = [r['request'] for r in requests if r['kind'] == 'accumulated']
        self.assertEqual([(r['year'], r['month'], r['day']) for r in endpoints],
                         [('2025', '12', ['30', '31']), ('2026', '01', ['01'])])


def write_netcdf(path, variables, *, day='2025-12-31', hours=24, expver=False,
                 latitudes=None, longitudes=None, coordinate_dtype='f8'):
    latitudes = [49.95, 50.05] if latitudes is None else latitudes
    longitudes = [240.05, 239.95] if longitudes is None else longitudes
    with netCDF4.Dataset(path, 'w') as ds:
        for key, size in [('valid_time', hours), ('latitude', len(latitudes)), ('longitude', len(longitudes))]:
            ds.createDimension(key, size)
        time = ds.createVariable('valid_time', 'i8', ('valid_time',))
        time.units = f'hours since {day} 00:00:00'; time.calendar = 'proleptic_gregorian'
        time[:] = np.arange(hours)
        ds.createVariable('latitude', coordinate_dtype, ('latitude',))[:] = latitudes
        ds.createVariable('longitude', coordinate_dtype, ('longitude',))[:] = longitudes
        if expver:
            ds.createDimension('expver', 2)
        for name, (units, number) in variables.items():
            dims = ('valid_time', 'latitude', 'longitude')
            if expver:
                dims = ('expver', *dims)
            field = ds.createVariable(name, 'f8', dims, fill_value=-9999)
            field.units = units; field[:] = number


class ResponseTests(unittest.TestCase):
    def test_float32_regular_axis_and_duplicate_roundoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, dtype in [('a', 'f4'), ('b', 'f8')]:
                write_netcdf(root/f'{name}.nc', {'t2m': ('K', np.arange(9).reshape(1, 3, 3)+280)},
                             latitudes=[49.9, 50., 50.1], longitudes=[239.95, 240.05, 240.15],
                             coordinate_dtype=dtype)
            with zipfile.ZipFile(root/'data.zip', 'w') as archive:
                for name in ['a', 'b']: archive.write(root/f'{name}.nc', f'{name}.nc')
            result = read_response(root/'data.zip', ['t2m'])['t2m']
            np.testing.assert_allclose(result['longitude'], [-120.05, -119.95, -119.85], rtol=0, atol=2e-5)
            np.testing.assert_allclose(np.diff(result['longitude']), np.diff(result['longitude'])[0],
                                       rtol=0, atol=1e-12)
            np.testing.assert_array_equal(result['data'][0], np.arange(9).reshape(3, 3)[::-1]+280)

    def test_irregular_axis_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.nc'
            write_netcdf(path, {'t2m': ('K', 280)}, longitudes=[239.9, 240., 240.11])
            with self.assertRaisesRegex(ValueError, 'regular.*longitude'):
                read_response(path, ['t2m'])

    def test_axis_orientation_expver_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.nc'
            write_netcdf(path, {'t2m': ('K', 280)}, expver=True)
            result = read_response(path, ['t2m'])['t2m']
            np.testing.assert_allclose(result['latitude'], [50.05, 49.95])
            np.testing.assert_allclose(result['longitude'], [-120.05, -119.95])
            self.assertEqual(result['data'].shape, (24, 2, 2))
            with netCDF4.Dataset(path, 'a') as ds:
                ds['t2m'][0, 0, 0, 0] = -9999
            self.assertTrue(np.all(read_response(path, ['t2m'])['t2m']['data'] == 280))
            with netCDF4.Dataset(path, 'a') as ds:
                ds['t2m'][1, 1, 0, 0] = 281
            with self.assertRaisesRegex(ValueError, 'Conflicting expver'):
                read_response(path, ['t2m'])
            write_netcdf(path, {'t2m': ('degC', 10)})
            with self.assertRaisesRegex(ValueError, 'units'):
                read_response(path, ['t2m'])

    def test_missing_cell_does_not_renormalize(self):
        data = np.ones((24, 2, 2)); data[0, 0, 0] = np.nan
        fields = {'example': (data, '1', 'mean', None)}
        rows = process_day(fields, {'A': np.ones((2, 2))}, date(2025, 12, 31), 'era5_cds')
        self.assertEqual(rows[0]['value'], '')
        self.assertEqual(rows[0]['valid_hours'], 23)
        self.assertEqual(rows[0]['min_valid_area_fraction'], .75)

    def test_archive_members_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('../data.nc', b'not netcdf')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                read_response(path, ['t2m'])

    def test_zip_split_fields_and_day_slices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_netcdf(root/'t.nc', {'t2m': ('K', 280)}, day='2025-12-30', hours=48)
            write_netcdf(root/'sd.nc', {'sd': ('m of water equivalent', .1)}, day='2025-12-30', hours=48)
            with zipfile.ZipFile(root/'data.zip', 'w') as archive:
                archive.write(root/'t.nc', 'instant/data_t.nc')
                archive.write(root/'sd.nc', 'instant/data_sd.nc')
            result = read_response(root/'data.zip', ['t2m', 'sd'], date(2025, 12, 31))
            self.assertEqual(result['t2m']['data'].shape, (24, 2, 2))
            self.assertEqual(align_hours(result['sd'], date(2025, 12, 31))[0, 0, 0], .1)

    def test_duplicate_fields_with_changed_grid_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['a', 'b']:
                write_netcdf(root/f'{name}.nc', {'t2m': ('K', 280)})
            with netCDF4.Dataset(root/'b.nc', 'a') as ds:
                ds['latitude'][:] += 1
            with zipfile.ZipFile(root/'data.zip', 'w') as archive:
                for name in ['a', 'b']: archive.write(root/f'{name}.nc', f'{name}.nc')
            with self.assertRaisesRegex(ValueError, 'Conflicting duplicate field'):
                read_response(root/'data.zip', ['t2m'])


class FakeClient:
    def __init__(self):
        self.calls = 0

    def retrieve(self, dataset, request, target):
        self.calls += 1
        first = lambda value: value[0] if isinstance(value, list) else value
        day = f'{first(request["year"])}-{first(request["month"])}-{request["day"][0]}'
        hours = len(request['time'])*len(request['day'])
        lookup = {value[0]: (key, value[1]) for key, value in ALL_FIELDS.items()}
        values = {'t2m': 280., 'sd': .2, 'sde': .8, 'rsn': 250., 'snowc': 100.,
                  'tp': .002, 'smlt': .001, 'e': -.001, 'ssr': 86400., 'str': -43200.,
                  'tcc': .5, 'sp': 99000., 'z': 100*9.80665}
        fields = {lookup[name][0]: (lookup[name][1], values.get(lookup[name][0], .25)) for name in request['variable']}
        write_netcdf(target, fields, day=day, hours=hours)
        with netCDF4.Dataset(target, 'a') as ds:
            ds['valid_time'][:] = [day_index*24+int(hour[:2]) for day_index in range(len(request['day']))
                                   for hour in request['time']]
        if dataset == 'reanalysis-era5-pressure-levels':
            with netCDF4.Dataset(target, 'w') as ds:
                for key, size in [('valid_time', hours), ('pressure_level', len(PRESSURE_LEVELS)),
                                  ('latitude', 2), ('longitude', 2)]:
                    ds.createDimension(key, size)
                time = ds.createVariable('valid_time', 'i8', ('valid_time',))
                time.units = f'hours since {day} 00:00:00'; time[:] = np.arange(hours)
                ds.createVariable('latitude', 'f8', ('latitude',))[:] = [49.95, 50.05]
                ds.createVariable('longitude', 'f8', ('longitude',))[:] = [240.05, 239.95]
                level = ds.createVariable('pressure_level', 'f8', ('pressure_level',))
                level.units = 'hPa'; level[:] = PRESSURE_LEVELS
                z = (1000-np.array(PRESSURE_LEVELS))*10.
                for name, value, units in [('z', z*9.80665, 'm**2 s**-2'), ('t', 280-z*.006, 'K')]:
                    field = ds.createVariable(name, 'f8', ('valid_time', 'pressure_level', 'latitude', 'longitude'))
                    field.units = units; field[:] = value[None, :, None, None]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.geometry = self.root/'catchments.geojson'
        self.geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
            'type': 'Feature', 'properties': {'id': 'A', 'name': 'A', 'part': 'local'},
            'geometry': {'type': 'Polygon', 'coordinates': [[[-120.03, 49.97], [-119.97, 49.97],
                          [-119.97, 50.03], [-120.03, 50.03], [-120.03, 49.97]]]}}]}))
        self.arguments = (self.geometry, 'era5-land', self.root/'output', self.root/'cache',
                          '2025-12-31', '2025-12-31')

    def test_roundoff_between_fields_preserves_daily_values(self):
        client = FakeClient()
        retrieve = client.retrieve
        def rounded_response(dataset, request, target):
            retrieve(dataset, request, target)
            if len(request['time']) == 1:
                with netCDF4.Dataset(target, 'a') as ds:
                    for axis in ['latitude', 'longitude']:
                        ds[axis][:] = np.asarray(ds[axis][:], dtype='f4')
        with patch.object(client, 'retrieve', side_effect=rounded_response):
            run_pipeline(*self.arguments, client=client)
        with gzip.open(self.root/'output/daily_2025-12.csv.gz', 'rt') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 23)
        self.assertTrue(all(row['qc'] == 'valid' for row in rows))
        self.assertEqual(next(float(r['value']) for r in rows if r['variable'] == 'precipitation_mm'), 2.)

    def test_real_grid_shift_is_rejected_with_diagnostics(self):
        client = FakeClient()
        retrieve = client.retrieve
        def shifted_response(dataset, request, target):
            retrieve(dataset, request, target)
            if len(request['time']) == 1:
                with netCDF4.Dataset(target, 'a') as ds:
                    ds['longitude'][:] += .001
        with patch.object(client, 'retrieve', side_effect=shifted_response):
            with self.assertRaisesRegex(ValueError, 'CDS grid.*accumulated/tp.*longitude'):
                run_pipeline(*self.arguments, client=client)
        diagnostic = json.loads((self.root/'output/grid_mismatch.json').read_text())
        self.assertAlmostEqual(diagnostic['axes']['longitude']['max_abs_difference_degrees'], .001)
        self.assertFalse((self.root/'output/daily_2025-12.csv.gz').exists())

    def test_unfinished_processing_update_reuses_verified_downloads(self):
        client = FakeClient()
        with patch('download_cds.land_daily_fields', side_effect=ValueError('Interrupted processing')):
            with self.assertRaisesRegex(ValueError, 'Interrupted processing'):
                run_pipeline(*self.arguments, client=client)
        run_path = self.root/'output/run.json'
        previous = json.loads(run_path.read_text())
        previous['code_sha256']['download_cds.py'] = 'old-processing-code'
        previous['methods'].pop('grid_coordinates', None)
        run_path.write_text(json.dumps(previous))
        (self.root/'output/daily_2025-12.csv.part').write_bytes(b'incomplete output')
        with patch.object(client, 'retrieve', side_effect=AssertionError('Redownloaded cached response')):
            run_pipeline(*self.arguments, client=client)
        history = list((self.root/'output/run_history').glob('*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text()), previous)
        self.assertEqual(client.calls, 2)
        self.assertTrue((self.root/'output/month_2025-12.json').exists())
        self.assertFalse((self.root/'output/daily_2025-12.csv.part').exists())

    def test_processing_update_cannot_mix_completed_months(self):
        run_pipeline(*self.arguments, client=FakeClient())
        run_path = self.root/'output/run.json'
        previous = json.loads(run_path.read_text())
        previous['code_sha256']['download_cds.py'] = 'old-processing-code'
        run_path.write_text(json.dumps(previous))
        for artifact in ['month_2025-12.json', 'daily_2025-12.csv.gz']:
            with self.subTest(remaining_artifact=artifact):
                with self.assertRaisesRegex(ValueError, 'configuration differs'):
                    run_pipeline(*self.arguments, cache_only=True)
            if artifact.startswith('month'):
                (self.root/'output'/artifact).unlink()

    def test_unfinished_processing_update_cannot_change_request(self):
        with patch('download_cds.land_daily_fields', side_effect=ValueError('Interrupted processing')):
            with self.assertRaisesRegex(ValueError, 'Interrupted processing'):
                run_pipeline(*self.arguments, client=FakeClient())
        run_path = self.root/'output/run.json'
        previous = json.loads(run_path.read_text())
        previous['code_sha256']['download_cds.py'] = 'old-processing-code'
        for key, value in [('geometry_sha256', 'different-geometry'), ('start', '2025-12-30'),
                           ('projects', ['B']), ('chunk_days', 31)]:
            with self.subTest(changed=key):
                run_path.write_text(json.dumps(dict(previous, **{key: value})))
                with self.assertRaisesRegex(ValueError, 'configuration differs'):
                    run_pipeline(*self.arguments, cache_only=True)

    def test_different_grid_dimensions_are_rejected(self):
        client = FakeClient()
        retrieve = client.retrieve
        def different_shape(dataset, request, target):
            retrieve(dataset, request, target)
            if len(request['time']) == 1:
                fields = {key: (definition[1], .001) for key, definition in ALL_FIELDS.items()
                          if definition[0] in request['variable']}
                write_netcdf(target, fields, day='2026-01-01', hours=1,
                             longitudes=[239.95, 240.05, 240.15])
        with patch.object(client, 'retrieve', side_effect=different_shape):
            with self.assertRaisesRegex(ValueError, 'CDS grid mismatch'):
                run_pipeline(*self.arguments, client=client)
        axes = json.loads((self.root/'output/grid_mismatch.json').read_text())['axes']
        self.assertEqual(axes['longitude']['reference']['count'], 2)
        self.assertEqual(axes['longitude']['received']['count'], 3)
        self.assertIsNone(axes['longitude']['max_abs_difference_degrees'])

    def test_full_synthetic_products_verified_resume_and_configuration_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); geometry = root/'catchments.geojson'
            geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
                'type': 'Feature', 'properties': {'id': 'A', 'name': 'A', 'part': 'local'},
                'geometry': {'type': 'Polygon', 'coordinates': [[[-120.03, 49.97], [-119.97, 49.97],
                              [-119.97, 50.03], [-120.03, 50.03], [-120.03, 49.97]]]}}]}))
            for product, expected_count in [('era5-land', 23), ('era5', 3)]:
                client = FakeClient(); output = root/product; cache = root/f'{product}_cache'
                run = run_pipeline(geometry, product, output, cache, '2025-12-31', '2025-12-31', client=client)
                self.assertEqual(client.calls, 2)
                with gzip.open(output/'daily_2025-12.csv.gz', 'rt') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(all(row['qc'] == 'valid' for row in rows))
                values = {row['variable']: float(row['value']) for row in rows}
                if product == 'era5-land':
                    self.assertAlmostEqual(values['precipitation_mm'], 2.)
                    self.assertAlmostEqual(values['actual_evapotranspiration_mm'], 1.)
                else:
                    self.assertAlmostEqual(values['freezing_level_geopotential_height_m'], (280-273.15)/.006)
                run_pipeline(geometry, product, output, cache, '2025-12-31', '2025-12-31', client=client)
                self.assertEqual(client.calls, 2)
                (output/'daily_2025-12.csv.gz').write_text('corrupt')
                run_pipeline(geometry, product, output, cache, '2025-12-31', '2025-12-31', cache_only=True)
                self.assertEqual(json.loads((output/'month_2025-12.json').read_text())['rows'], expected_count)
                with self.assertRaisesRegex(ValueError, 'configuration differs'):
                    run_pipeline(geometry, product, output, cache, '2025-12-30', '2025-12-31', cache_only=True)

    def test_calendar_batch_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); geometry = root/'catchments.geojson'
            geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
                'type': 'Feature', 'properties': {'id': 'A', 'name': 'A', 'part': 'local'},
                'geometry': {'type': 'Polygon', 'coordinates': [[[-120.03, 49.97], [-119.97, 49.97],
                              [-119.97, 50.03], [-120.03, 50.03], [-120.03, 49.97]]]}}]}))
            client = FakeClient()
            run_pipeline(geometry, 'era5-land', root/'output', root/'cache', '2025-12-29', '2026-01-01', client=client)
            self.assertEqual(client.calls, 5)
            for month, count in [('2025-12', 69), ('2026-01', 23)]:
                with gzip.open(root/'output'/f'daily_{month}.csv.gz', 'rt') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row['qc'] == 'valid' for row in rows))
                self.assertTrue(all(float(row['value']) == 2 for row in rows if row['variable'] == 'precipitation_mm'))


if __name__ == '__main__':
    unittest.main()
