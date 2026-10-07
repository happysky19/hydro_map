"""Checks for CDS units, UTC endpoints, native freezing level and verified resumes."""

from contextlib import redirect_stdout
import csv
from datetime import date, datetime, timedelta
import gzip
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import netCDF4
import numpy as np

from cds_fields import (root_zone_moisture, land_daily_fields, era_daily_fields, dewpoint_humidity,
                        ALL_FIELDS, LAND_STATES, LAND_ACCUMULATED)
from download_cds import (make_requests, accumulated_requests, read_response, process_day, run_pipeline,
                          align_hours, prefetch)
from meteorology import humidity


class FieldTests(unittest.TestCase):
    def test_native_freezing_level_stays_defined_when_column_is_below_freezing(self):
        surface = dict(deg0l=np.full((24, 1, 2), 1200.), z=np.full((24, 1, 2), 500*9.80665),
                       tcc=np.full((24, 1, 2), .5))
        surface['deg0l'][0, 0, 1] = 0.
        rows = process_day(era_daily_fields(surface), {'A': np.array([[.99, .01]])},
                           date(2025, 12, 28), 'era5_cds')
        values = {row['variable']: row for row in rows}
        self.assertTrue(all(row['qc'] == 'valid' and row['valid_hours'] == 24 for row in rows))
        self.assertAlmostEqual(values['freezing_level_above_ground_m']['value'], 1200 - 12/24)
        self.assertAlmostEqual(values['freezing_level_above_sea_level_m']['value'], 1700 - 12/24)
        self.assertEqual(values['cloud_cover_fraction']['value'], .5)

    def test_perennial_snow_cells_are_excluded_from_snow_states_only(self):
        states = {key: np.full((24, 1, 2), 280.) for key in LAND_STATES}
        states.update(sp=np.full((24, 1, 2), 90000.), sd=np.array([.2, 8.])[None, None, :].repeat(24, 0),
                      sde=np.array([.8, 20.])[None, None, :].repeat(24, 0))
        accumulated = {key: np.full((1, 2), .001) for key in LAND_ACCUMULATED}
        rows = process_day(land_daily_fields(states, accumulated), {'A': np.array([[.75, .25]])},
                           date(2025, 12, 31), 'era5_land_cds')
        values = {row['variable']: row for row in rows}
        self.assertAlmostEqual(values['snow_water_equivalent_mm']['value'], 200)
        self.assertAlmostEqual(values['snow_depth_m']['value'], .8)
        self.assertAlmostEqual(values['perennial_snow_area_pct']['value'], 25)
        self.assertAlmostEqual(values['precipitation_mm']['value'], 1)
        self.assertTrue(all(row['qc'] == 'valid' for row in rows))
        states['sd'][:] = 8.
        rows = process_day(land_daily_fields(states, accumulated), {'A': np.array([[.75, .25]])},
                           date(2025, 12, 31), 'era5_land_cds')
        swe = next(row for row in rows if row['variable'] == 'snow_water_equivalent_mm')
        self.assertEqual((swe['value'], swe['qc']), ('', 'no_defined_area'))

    def test_snow_density_uses_snow_covered_cells_only(self):
        states = {key: np.full((24, 1, 2), 280.) for key in LAND_STATES}
        states.update(sp=np.full((24, 1, 2), 90000.), sd=np.array([.2, -1e-24])[None, None, :].repeat(24, 0),
                      rsn=np.array([300., 100.])[None, None, :].repeat(24, 0))
        accumulated = {key: np.full((1, 2), .001) for key in LAND_ACCUMULATED}
        fields = land_daily_fields(states, accumulated)
        rows = {row['variable']: row for row in
                process_day(fields, {'A': np.array([[.5, .5]])}, date(2025, 7, 15), 'era5_land_cds')}
        self.assertAlmostEqual(rows['snow_density_kgm3']['value'], 300)
        self.assertAlmostEqual(rows['snow_water_equivalent_mm']['value'], 100)
        states['sd'][:] = 0.
        rows = {row['variable']: row for row in process_day(land_daily_fields(states, accumulated),
                {'A': np.array([[.5, .5]])}, date(2025, 7, 15), 'era5_land_cds')}
        self.assertEqual((rows['snow_density_kgm3']['value'], rows['snow_density_kgm3']['qc']), ('', 'no_defined_area'))
        self.assertEqual(rows['snow_water_equivalent_mm']['value'], 0)

    def test_dewpoint_humidity_matches_aorc_conventions(self):
        temperature, pressure = np.array([283.15, 263.15]), np.array([90000., 70000.])
        specific, rh, vpd = dewpoint_humidity(temperature, temperature, pressure)
        np.testing.assert_allclose(rh, 100)
        np.testing.assert_allclose(vpd, 0, atol=1e-12)
        np.testing.assert_allclose(humidity(temperature-273.15, specific, pressure)[0], 100)
        drier = dewpoint_humidity(temperature, temperature-5, pressure)
        self.assertTrue(np.all(drier[1] < 100) and np.all(drier[2] > 0) and np.all(drier[0] < specific))

    def test_soil_and_snow_units(self):
        arrays = {f'swvl{i}': np.ones((24, 2, 2)) * i / 10 for i in range(1, 5)}
        self.assertAlmostEqual(root_zone_moisture(arrays)[0, 0, 0], .265)
        arrays.update(t2m=np.ones((24, 2, 2))*273.15, d2m=np.ones((24, 2, 2))*270.,
                      sp=np.ones((24, 2, 2))*90000., u10=np.ones((24, 2, 2))*3., v10=np.ones((24, 2, 2))*4.,
                      sd=np.ones((24, 2, 2))*.2,
                      sde=np.ones((24, 2, 2))*.8, rsn=np.ones((24, 2, 2))*250,
                      snowc=np.ones((24, 2, 2))*100)
        arrays.update({f'stl{i}': np.ones((24, 2, 2))*280 for i in range(1, 5)})
        accum = {key: np.ones((2, 2)) for key in LAND_ACCUMULATED}
        accum.update(tp=np.full((2, 2), .004), sf=np.full((2, 2), .001), ssrd=np.full((2, 2), 8.64e6))
        fields = land_daily_fields(arrays, accum)
        self.assertEqual(fields['snow_water_equivalent_mm'][0][0, 0, 0], 200)
        self.assertEqual(fields['snow_depth_m'][0][0, 0, 0], .8)
        self.assertEqual(fields['actual_evapotranspiration_mm'][0][0, 0], -1000)
        self.assertEqual(fields['net_radiation_energy_mjm2'][0][0, 0], 2e-6)
        self.assertAlmostEqual(fields['snowfall_mm'][0][0, 0], 1)
        self.assertAlmostEqual(fields['rainfall_mm'][0][0, 0], 3)
        self.assertAlmostEqual(fields['shortwave_down_mean_wm2'][0][0, 0], 100)
        self.assertAlmostEqual(fields['shortwave_down_energy_mjm2'][0][0, 0], 8.64)
        self.assertEqual(fields['wind_speed_ms'][0][0, 0, 0], 5)
        self.assertLess(fields['wet_bulb_temperature_c'][0][0, 0, 0], 0)

    def test_era5_requests_only_single_level_fields(self):
        requests = make_requests('era5', date(2025, 12, 29), [51, -121, 49, -119], date(2025, 12, 31))
        self.assertEqual([r['dataset'] for r in requests], ['reanalysis-era5-single-levels'])
        self.assertIn('zero_degree_level', requests[0]['request']['variable'])
        self.assertNotIn('pressure_level', requests[0]['request'])

    def test_december_endpoint_request(self):
        requests = accumulated_requests(date(2025, 12, 31), date(2025, 12, 31), [51, -121, 49, -119])
        self.assertEqual(len(requests), 1)
        endpoint = requests[0]['request']
        self.assertEqual((endpoint['year'], endpoint['month'], endpoint['day'], endpoint['time']),
                         ('2026', ['01'], ['01'], ['00:00']))
        self.assertEqual([r['kind'] for r in make_requests('era5-land', date(2025, 12, 31), [51, -121, 49, -119])],
                         ['states'])

    def test_endpoints_are_one_request_per_year(self):
        requests = accumulated_requests(date(2024, 1, 1), date(2025, 12, 31), [51, -121, 49, -119])
        self.assertEqual([(r['request']['year'], len(r['request']['month']), len(r['request']['day']))
                          for r in requests], [('2024', 12, 31), ('2025', 12, 31), ('2026', 1, 1)])
        cost = max(len(r['fields'])*len(r['request']['month'])*len(r['request']['day'])*2 for r in requests)
        self.assertLess(cost, 12000)

    def test_fields_for_the_cds_get_their_own_endpoint_request(self):
        area = [51, -121, 49, -119]
        whole = accumulated_requests(date(2025, 12, 31), date(2025, 12, 31), area)
        split = accumulated_requests(date(2025, 12, 31), date(2025, 12, 31), area, ('str',))
        self.assertEqual([spec.get('provider') for spec in split], [None, 'cds'])
        self.assertEqual(split[1]['fields'], ['str'])
        self.assertEqual(split[1]['request']['variable'], ['surface_net_thermal_radiation'])
        self.assertEqual(split[0]['fields'] + split[1]['fields'], whole[0]['fields'])


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
    # Prefetch threads call retrieve concurrently; the HDF5 library is not thread-safe.
    lock = threading.Lock()

    def __init__(self):
        self.calls = 0

    def retrieve(self, dataset, request, target):
        with self.lock:
            self._write(dataset, request, target)

    def _write(self, dataset, request, target):
        self.calls += 1
        listed = lambda value: value if isinstance(value, list) else [value]
        stamps = []
        for year in listed(request['year']):
            for month in listed(request['month']):
                for day in listed(request['day']):
                    try:
                        stamp = datetime(int(year), int(month), int(day))
                    except ValueError:
                        continue
                    stamps += [stamp + timedelta(hours=int(hour[:2])) for hour in request['time']]
        origin = min(stamps)
        day, hours = origin.date().isoformat(), len(stamps)
        lookup = {value[0]: (key, value[1]) for key, value in ALL_FIELDS.items()}
        values = {'t2m': 280., 'd2m': 275., 'sp': 99000., 'u10': 3., 'v10': 4.,
                  'sd': .2, 'sde': .8, 'rsn': 250., 'snowc': 100.,
                  'tp': .002, 'sf': .0005, 'smlt': .001, 'e': -.001, 'ssrd': 8.64e6, 'strd': 2.592e7,
                  'ssr': 86400., 'str': -43200., 'tcc': .5, 'deg0l': 1500., 'z': 100*9.80665,
                  **{f'stl{i}': 275. for i in range(1, 5)}}
        fields = {lookup[name][0]: (lookup[name][1], values.get(lookup[name][0], .25)) for name in request['variable']}
        write_netcdf(target, fields, day=day, hours=hours)
        with netCDF4.Dataset(target, 'a') as ds:
            ds['valid_time'][:] = [int((stamp - origin).total_seconds()//3600) for stamp in stamps]


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
        self.assertEqual(len(rows), 40)
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
            for product, expected_count, calls in [('era5-land', 40, 2), ('era5', 3, 1)]:
                client = FakeClient(); output = root/product; cache = root/f'{product}_cache'
                run = run_pipeline(geometry, product, output, cache, '2025-12-31', '2025-12-31', client=client)
                self.assertEqual(client.calls, calls)
                with gzip.open(output/'daily_2025-12.csv.gz', 'rt') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(all(row['qc'] == 'valid' for row in rows))
                values = {row['variable']: float(row['value']) for row in rows}
                if product == 'era5-land':
                    self.assertAlmostEqual(values['precipitation_mm'], 2.)
                    self.assertAlmostEqual(values['actual_evapotranspiration_mm'], 1.)
                    self.assertAlmostEqual(values['snowfall_mm'] + values['rainfall_mm'], 2.)
                    self.assertAlmostEqual(values['longwave_down_mean_wm2'], 300.)
                    self.assertAlmostEqual(values['wind_speed_ms'], 5.)
                else:
                    self.assertAlmostEqual(values['freezing_level_above_ground_m'], 1500.)
                    self.assertAlmostEqual(values['freezing_level_above_sea_level_m'], 1600.)
                run_pipeline(geometry, product, output, cache, '2025-12-31', '2025-12-31', client=client)
                self.assertEqual(client.calls, calls)
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
            self.assertEqual(client.calls, 4)
            for month, count in [('2025-12', 120), ('2026-01', 40)]:
                with gzip.open(root/'output'/f'daily_{month}.csv.gz', 'rt') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row['qc'] == 'valid' for row in rows))
                self.assertTrue(all(float(row['value']) == 2 for row in rows if row['variable'] == 'precipitation_mm'))

    def test_prefetch_retries_transient_failures_and_caches_once(self):
        client = FakeClient()
        retrieve, failures = client.retrieve, []
        def flaky(dataset, request, target):
            if not failures:
                failures.append(dataset)
                raise RuntimeError('Number queued requests for this dataset is temporarily limited')
            retrieve(dataset, request, target)
        area = [50.1, -120.1, 49.9, -119.9]
        specs = make_requests('era5-land', date(2025, 12, 31), area) + accumulated_requests(
            date(2025, 12, 31), date(2025, 12, 31), area)
        manifest = {}
        with patch.object(client, 'retrieve', side_effect=flaky), redirect_stdout(io.StringIO()):
            prefetch(specs, self.root/'cache', manifest, client, workers=2, retry_seconds=0)
        self.assertEqual(len(failures), 1)
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(manifest), 2)
        self.assertEqual(json.loads((self.root/'cache/requests.manifest.json').read_text()).keys(), manifest.keys())

    def test_queue_limit_is_transient_even_with_status_digits_in_job_id(self):
        from download_cds import transient
        import requests
        queued = requests.HTTPError('400 Client Error: Bad Request for url: https://cds.climate.copernicus.eu/'
                                    'api/retrieve/v1/jobs/a71f9fc3-b27d-49f0-bc5a-bd403ce3f43c/results\n'
                                    'Number queued requests for this dataset is temporarily limited')
        self.assertTrue(transient(queued))
        self.assertFalse(transient(requests.HTTPError('403 Client Error: Forbidden ... cost limits exceeded')))
        self.assertFalse(transient(RuntimeError('Source unavailable')))

    def test_newest_first_requests_latest_month_first(self):
        client = FakeClient()
        retrieve, order = client.retrieve, []
        def record(dataset, request, target):
            order.append((request['year'], request['month']))
            retrieve(dataset, request, target)
        with patch.object(client, 'retrieve', side_effect=record), redirect_stdout(io.StringIO()):
            run_pipeline(self.geometry, 'era5', self.root/'output', self.root/'cache', '2025-12-30', '2026-01-02',
                         client=client, workers=1, newest_first=True)
        self.assertEqual(order, [(['2026'], ['01']), (['2025'], ['12'])])
        self.assertTrue((self.root/'output/month_2025-12.json').exists())

    def test_response_cached_by_another_process_is_reused(self):
        from download_cds import fetch_response
        client = FakeClient()
        spec = make_requests('era5', date(2025, 12, 31), [50.1, -120.1, 49.9, -119.9])[0]
        (self.root/'cache').mkdir()
        fetch_response(spec, self.root/'cache', {}, client, lock=threading.Lock())
        with patch.object(client, 'retrieve', side_effect=AssertionError('Downloaded again')):
            fetch_response(spec, self.root/'cache', {}, client)
        self.assertEqual(client.calls, 1)

    def test_queue_limits_do_not_use_up_attempts_and_failures_do_not_stop_others(self):
        client = FakeClient()
        retrieve, rejected = client.retrieve, []
        def busy(dataset, request, target):
            if dataset == 'reanalysis-era5-land' and len(request['time']) == 24 and len(rejected) < 3:
                rejected.append(1)
                raise RuntimeError('Number queued requests for this dataset is temporarily limited')
            if dataset == 'reanalysis-era5-land' and len(request['time']) == 1:
                raise RuntimeError('Request is invalid')
            retrieve(dataset, request, target)
        area = [50.1, -120.1, 49.9, -119.9]
        specs = make_requests('era5-land', date(2025, 12, 31), area) + accumulated_requests(
            date(2025, 12, 31), date(2025, 12, 31), area)
        manifest = {}
        with patch.object(client, 'retrieve', side_effect=busy), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'invalid'):
                prefetch(specs, self.root/'cache', manifest, client, workers=1, attempts=2, retry_seconds=0)
        self.assertEqual(len(rejected), 3)
        self.assertEqual(len(manifest), 1)

    def test_latest_year_is_processed_before_older_years_download(self):
        client = FakeClient()
        retrieve = client.retrieve
        def older_unavailable(dataset, request, target):
            if request['year'] == ['2025']:
                raise RuntimeError('Request is invalid')
            retrieve(dataset, request, target)
        with patch.object(client, 'retrieve', side_effect=older_unavailable), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'invalid'):
                run_pipeline(self.geometry, 'era5', self.root/'output', self.root/'cache', '2025-12-30',
                             '2026-01-02', client=client, newest_first=True)
        self.assertTrue((self.root/'output/month_2026-01.json').exists())
        self.assertFalse((self.root/'output/month_2025-12.json').exists())

    def test_fields_lacking_in_another_provider_come_from_the_cds(self):
        hub, cds = FakeClient(), FakeClient()
        requested = {'hub': [], 'cds': []}
        for name, client in [('hub', hub), ('cds', cds)]:
            retrieve = client.retrieve
            def record(dataset, request, target, name=name, retrieve=retrieve):
                requested[name] += request['variable']
                retrieve(dataset, request, target)
            client.retrieve = record
        with redirect_stdout(io.StringIO()):
            run_pipeline(*self.arguments, client=hub, cds_client=cds, land_source='edh')
        self.assertEqual(requested['cds'], ['surface_net_thermal_radiation'])
        self.assertNotIn('surface_net_thermal_radiation', requested['hub'])
        with gzip.open(self.root/'output/daily_2025-12.csv.gz', 'rt') as stream:
            rows = {row['variable']: row for row in csv.DictReader(stream)}
        self.assertEqual(rows['net_radiation_mean_wm2']['qc'], 'valid')
        self.assertAlmostEqual(float(rows['net_radiation_mean_wm2']['value']), .5)
        manifest = json.loads((self.root/'cache/requests.manifest.json').read_text())
        self.assertEqual(len(manifest), 3)

    def test_request_errors_are_not_retried(self):
        client = FakeClient()
        specs = make_requests('era5', date(2025, 12, 31), [50.1, -120.1, 49.9, -119.9])
        for error in (RuntimeError('Source unavailable'),
                      OSError('403 Client Error: Forbidden; cost limits exceeded')):
            with self.subTest(error=str(error)):
                with patch.object(client, 'retrieve', side_effect=error) as retrieve, \
                     patch('download_cds.time.sleep', side_effect=AssertionError('Retried')):
                    with self.assertRaises(type(error)):
                        prefetch(specs, self.root/'cache', {}, client)
                self.assertEqual(retrieve.call_count, 1)


if __name__ == '__main__':
    unittest.main()
