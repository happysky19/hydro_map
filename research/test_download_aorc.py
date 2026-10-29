import datetime as dt
import csv
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from download_aorc import (BASE, Store, accumulate, extract_native_series, load_polygons,
                           requested_hours, run, unpack)


class AorcDownloadTests(unittest.TestCase):
    def test_tailrace_metadata_is_retained_and_validated_in_raw_geojson(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catchments.geojson'
            feature = dict(type='Feature', properties=dict(id='A', part='local',
                catchment_role='natural_reach_at_tailrace', diversion_intake_project='UPSTREAM',
                routing_requires_operations=True), geometry=dict(type='Polygon',
                coordinates=[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]))
            path.write_text(json.dumps(dict(type='FeatureCollection', features=[feature])))
            _, metadata = load_polygons(path, None, include_metadata=True)
            record = metadata['project_forcing']['A']
            self.assertEqual(record['catchment_role'], 'natural_reach_at_tailrace')
            self.assertEqual(record['diversion_intake_project'], 'UPSTREAM')
            self.assertIs(record['routing_requires_operations'], True)
            self.assertIn('bypassed river', metadata['warnings'][0])
            self.assertIn('turbine inflow', metadata['warnings'][0])
            for key, value in [('routing_requires_operations', 'true'),
                               ('routing_requires_operations', 1),
                               ('routing_requires_operations', False),
                               ('diversion_intake_project', 'A'),
                               ('catchment_role', 'dam_outlet')]:
                original = feature['properties'][key]
                feature['properties'][key] = value
                path.write_text(json.dumps(dict(type='FeatureCollection', features=[feature])))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    load_polygons(path, None)
                feature['properties'][key] = original

    def test_declared_forcing_groups_preserve_metadata_and_validate_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'catchments.geojson'
            features = [dict(type='Feature', properties=dict(id=identifier, part='local',
                forcing_group='AB', geometry_status='shared_unit_approximation',
                shared_outlet_projects=['A', 'B']), geometry=dict(type='Polygon',
                coordinates=[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]))
                for identifier in ('A', 'B')]
            path.write_text(json.dumps(dict(type='FeatureCollection', features=features)))
            polygons, metadata = load_polygons(path, ['B'], include_metadata=True)
            self.assertEqual(list(polygons), ['B'])
            self.assertEqual(metadata['forcing_groups'], {'AB': ['B']})
            self.assertEqual(metadata['project_forcing']['B']['shared_outlet_projects'], ['A', 'B'])
            self.assertTrue(metadata['warnings'])
            for feature in features:
                feature['properties'] = dict(id=feature['properties']['id'], part='local',
                    shared_outlet_projects=[], geometry_status='hydrobasins_delineation')
            path.write_text(json.dumps(dict(type='FeatureCollection', features=features)))
            _, metadata = load_polygons(path, None, include_metadata=True)
            self.assertEqual(metadata['forcing_groups'], {'A': ['A'], 'B': ['B']})
            self.assertFalse(metadata['warnings'])
            for feature in features:
                feature['properties']['forcing_group'] = 'AB'
            features[0]['properties']['part'] = 'total'
            path.write_text(json.dumps(dict(type='FeatureCollection', features=features)))
            with self.assertRaisesRegex(ValueError, 'same geometry and part'):
                load_polygons(path, None)
            features[0]['properties']['part'] = 'local'
            features[0]['geometry']['coordinates'][0][1][0] = 2
            path.write_text(json.dumps(dict(type='FeatureCollection', features=features)))
            with self.assertRaisesRegex(ValueError, 'same geometry and part'):
                load_polygons(path, ['B'])

    def test_offline_store_remembers_objects_the_source_reported_absent(self):
        import json, tempfile
        from download_aorc import Store
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            absent = 'https://example.invalid/2026.zarr/.zmetadata'
            (cache / 'requests.jsonl').write_text(json.dumps(dict(url=absent, status=404, bytes=0)) + '\n')
            store = Store(cache, 1e6, offline=True)
            with self.assertRaises(FileNotFoundError):
                store.read(absent)
            with self.assertRaisesRegex(ValueError, 'offline cache'):
                store.read('https://example.invalid/2025.zarr/.zmetadata')

    def test_amounts_need_next_midnight(self):
        start = end = dt.date(2025, 12, 31)
        states = requested_hours(start, end, False)
        amounts = requested_hours(start, end, True)
        self.assertEqual(len(states), 24)
        self.assertEqual(amounts[0], states[0] + 3600)
        self.assertEqual(amounts[-1], dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())

    def test_packed_fill_is_masked_before_conversion(self):
        attrs = dict(scale_factor=.1, add_offset=273.15, missing_value=-32767)
        result = unpack(np.array([-32767, 100], dtype=np.int16), attrs, -32767, 'temperature_c')
        self.assertTrue(np.isnan(result[0]))
        self.assertAlmostEqual(result[1], 10.)

    def test_invalid_cell_does_not_renormalize(self):
        values = np.array([[1., 3.], [1., np.nan]])
        sums, coverage = accumulate(values, np.array([.25, .75]))
        np.testing.assert_allclose(sums, [2.5, .25])
        np.testing.assert_allclose(coverage, [1., .25])

    def native_derived_extraction(self, missing_union=False, missing_boundary_precip=False):
        variables = ['APCP_surface', 'TMP_2maboveground', 'SPFH_2maboveground', 'PRES_surface',
                     'DSWRF_surface', 'DLWRF_surface', 'UGRD_10maboveground', 'VGRD_10maboveground']
        scales = {'TMP_2maboveground': (1., 273.15), 'SPFH_2maboveground': (.000001, 0.),
                  'PRES_surface': (10., 0.)}
        metas = {year: {} for year in (2024, 2025)}
        for meta in metas.values():
            for variable in variables:
                scale, offset = scales.get(variable, (1., 0.))
                meta[variable + '/.zattrs'] = dict(scale_factor=scale, add_offset=offset,
                                                  missing_value=-32767)
                meta[variable + '/.zarray'] = dict(fill_value=-32767)
        times = {year: requested_hours(dt.date(year, 1, 1), dt.date(year, 12, 31), False)
                 for year in metas}
        blocks = {(0, 0): {'A': (np.array([0, 0]), np.array([0, 1]), np.array([.5, .5]))}}
        read_urls, discarded = [], []

        class FakeStore:
            bytes = 0

            def read(self, url):
                read_urls.append(url)
                if missing_boundary_precip and '2025.zarr/APCP' in url:
                    raise FileNotFoundError(url)
                return url.encode(), None

            def discard(self, url):
                discarded.append(url)

        def decoded(payload, dtype, zstd):
            variable = payload.decode().split('/')[-2]
            pair = {'APCP_surface': [2, 4], 'TMP_2maboveground': [-2, 3],
                    'SPFH_2maboveground': [3653, 5260], 'PRES_surface': [9000, 9000],
                    'DSWRF_surface': [100, 300], 'DLWRF_surface': [250, 350],
                    'UGRD_10maboveground': [3, -3], 'VGRD_10maboveground': [4, -4]}[variable]
            if missing_union and variable == 'SPFH_2maboveground':
                pair[1] = -32767
            if missing_union and variable == 'PRES_surface':
                pair[0] = -32767
            grid = np.zeros((1, 1, 256), dtype=np.int16)
            grid[0, 0, :2] = pair
            return np.broadcast_to(grid, (144, 128, 256)), 0

        with patch('download_aorc.decode', side_effect=decoded), patch('builtins.print'):
            result = extract_native_series(FakeStore(), metas, times, blocks, variables,
                dt.date(2024, 12, 31), dt.date(2024, 12, 31), 'unused', 2, False)
        self.assertEqual(len(read_urls), len(set(read_urls)))
        self.assertEqual(len(read_urls), 16)
        self.assertEqual(len(discarded), 15 if missing_boundary_precip else 16)
        return result

    def test_native_derived_extraction_preserves_nonlinear_cellwise_wind_and_phase(self):
        result = self.native_derived_extraction()
        np.testing.assert_allclose(result['wind_speed_ms'][1]['A'], np.full(24, 5.))
        np.testing.assert_allclose(result['rainfall_mm'][1]['A'], np.full(24, 2.))
        np.testing.assert_allclose(result['snowfall_mm'][1]['A'], np.full(24, 1.))
        for name in result:
            np.testing.assert_allclose(result[name][2]['A'], np.ones(24))
        self.assertEqual(result['rainfall_mm'][0][-1], dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(result['relative_humidity_pct'][0][-1], dt.datetime(2024, 12, 31, 23, tzinfo=dt.timezone.utc).timestamp())

    def test_derived_missing_mask_is_union_not_minimum_of_input_coverages(self):
        result = self.native_derived_extraction(missing_union=True)
        np.testing.assert_allclose(result['specific_humidity_kgkg'][2]['A'], np.full(24, .5))
        np.testing.assert_allclose(result['surface_pressure_pa'][2]['A'], np.full(24, .5))
        for name in ('relative_humidity_pct', 'wet_bulb_temperature_c', 'rainfall_mm', 'snowfall_mm'):
            np.testing.assert_allclose(result[name][2]['A'], np.zeros(24))
        np.testing.assert_allclose(result['wind_speed_ms'][2]['A'], np.ones(24))

    def test_boundary_precipitation_gap_only_masks_phase_amounts(self):
        result = self.native_derived_extraction(missing_boundary_precip=True)
        for name in ('rainfall_mm', 'snowfall_mm', 'precipitation_mm'):
            np.testing.assert_allclose(result[name][2]['A'], [1.] * 23 + [0.])
        for name in ('temperature_c', 'relative_humidity_pct', 'wet_bulb_temperature_c'):
            np.testing.assert_allclose(result[name][2]['A'], np.ones(24))

    def run_twice(self, folder, properties, extracted):
        """Run a one-day extraction twice over one catchment; returns the arguments and the extractor mock."""
        geojson = folder / 'catchment.geojson'
        geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[dict(
            type='Feature', properties=dict(id='A', part='local', **properties), geometry=dict(type='Polygon',
            coordinates=[[[0, 44], [1, 44], [1, 46], [0, 46], [0, 44]]]))])))
        args = SimpleNamespace(geojson=geojson, projects=None, start=dt.date(2024, 12, 31),
            end=dt.date(2024, 12, 31), output_dir=folder / 'output', cache_dir=folder / 'cache',
            workers=1, max_download_gb=.001, cache_only=True, keep_chunks=False)

        def fake_axis(store, year, name, metadata, zstd):
            return (requested_hours(dt.date(year, 1, 1), dt.date(year, 12, 31), False)
                    if name == 'time' else np.array([0., 1.]))

        with patch('download_aorc.shutil.which', return_value='zstd'), \
             patch('download_aorc.metadata', return_value={}), \
             patch('download_aorc.axis', side_effect=fake_axis), \
             patch('download_aorc.weights', return_value={
                 'A': (np.array([0]), np.array([0]), np.array([1.]))}), \
             patch('download_aorc.extract_native_series', return_value=extracted) as native, \
             patch('builtins.print'):
            run(args)
            run(args)
        return args, native

    def test_run_records_methods_dependencies_schema_and_pet(self):
        with tempfile.TemporaryDirectory() as directory:
            args, native = self.run_twice(Path(directory), {}, self.native_derived_extraction())
            self.assertEqual(native.call_count, 1)
            metadata = json.loads((args.output_dir / 'run.json').read_text())
            self.assertEqual(metadata['source_id'], 'aorc_v1.1')
            self.assertEqual(len(metadata['variables']), 8)
            self.assertEqual(metadata['pet_centroid_latitudes'], {'A': 45.})
            self.assertIn('meteorology.py', metadata['code_sha256'])
            self.assertEqual(metadata['derived_methods']['phase']['snow_threshold_c'], .5)
            with gzip.open(args.output_dir / 'daily_2024.csv.gz', 'rt') as stream:
                rows = {row['variable']: row for row in csv.DictReader(stream)}
            self.assertEqual(set(rows), set(metadata['daily_schema']))
            self.assertEqual(len(rows), 19)
            self.assertTrue(all(row['qc'] == 'valid' and row['valid_hours'] == '24' for row in rows.values()))
            self.assertEqual(float(rows['rainfall_mm']['value']) + float(rows['snowfall_mm']['value']),
                             float(rows['precipitation_mm']['value']))
            self.assertEqual(rows['pet_hargreaves_mm']['units'], 'mm/day')

    def test_a_year_with_gaps_keeps_routing_metadata_and_is_not_recomputed(self):
        properties = dict(catchment_role='natural_reach_at_tailrace', diversion_intake_project='UPSTREAM',
                          routing_requires_operations=True)
        extracted = self.native_derived_extraction(missing_boundary_precip=True)
        with tempfile.TemporaryDirectory() as directory:
            args, native = self.run_twice(Path(directory), properties, extracted)
            self.assertEqual(native.call_count, 1)
            metadata = json.loads((args.output_dir / 'run.json').read_text())
            self.assertEqual(metadata['forcing_groups'], {'A': ['A']})
            self.assertEqual(metadata['project_forcing']['A']['forcing_group'], 'A')
            self.assertEqual(metadata['project_forcing']['A']['diversion_intake_project'], 'UPSTREAM')
            self.assertIs(metadata['project_forcing']['A']['routing_requires_operations'], True)
            self.assertIn('turbine inflow', metadata['warnings'][0])
            self.assertEqual(json.loads((args.output_dir / 'year_2024.json').read_text())['status'],
                             'computed_with_gaps')
            with gzip.open(args.output_dir / 'daily_2024.csv.gz', 'rt') as stream:
                precipitation = next(row for row in csv.DictReader(stream) if row['variable'] == 'precipitation_mm')
            self.assertEqual((precipitation['value'], precipitation['valid_hours']), ('', '23'))

    def test_hash_valid_cache_is_read_without_a_request(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), limit=100)
            url = BASE + '2025.zarr/APCP_surface/0.0.0'
            path = store.path(url)
            path.write_bytes(b'old')
            path.with_suffix('.json').write_text(json.dumps(dict(
                url=url, status=200, bytes=3, sha256=hashlib.sha256(b'old').hexdigest())))
            with patch('download_aorc.requests.get') as request:
                self.assertEqual(store.read(url)[0], b'old')
                request.assert_not_called()
            self.assertEqual(store.bytes, 0)


if __name__ == '__main__':
    unittest.main()
