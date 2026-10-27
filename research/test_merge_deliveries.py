"""An ERA5-Land delivery north of AORC, and its merge with an AORC delivery into one table, README and GeoJSON."""

from contextlib import ExitStack, redirect_stdout
import csv
import io
import json
import unittest

import download_daily
from deliver_daily import companion
import merge_deliveries
import plot_daily
from test_download_cds import FakeClient
import test_download_daily


class MergeTests(unittest.TestCase):
    def case(self, project):
        case = test_download_daily.DownloadDailyTests('test_requested_table_can_be_written_as_parquet')
        case.setUp()
        self.addCleanup(case.doCleanups)
        doc = json.loads(case.geojson.read_text())
        doc['features'][0]['properties']['id'] = project
        case.geojson.write_text(json.dumps(doc))
        return case

    def deliveries(self):
        aorc, north = self.case('A'), self.case('B')
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            aorc.aorc_inputs(stack)
            download_daily.run(aorc.geojson, aorc.day, aorc.day, aorc.root / 'columbia.parquet', cds_client=FakeClient())
        with redirect_stdout(io.StringIO()):
            download_daily.run(north.geojson, north.day, north.day, north.root / 'peace.csv',
                               cds_client=FakeClient(), primary='era5-land')
        return aorc, north

    def test_without_aorc_the_aorc_columns_come_from_era5_land(self):
        _, north = self.deliveries()
        with (north.root / 'peace_full.csv').open() as stream:
            full = next(csv.DictReader(stream))
        with (north.root / 'peace.csv').open() as stream:
            requested = next(csv.DictReader(stream))
        self.assertFalse(any(name.startswith('aorc_') for name in full))
        self.assertEqual(requested['precipitation_mm'], full['era5_land_cds__precipitation_mm'])
        self.assertEqual(requested['tmean_degC'], full['era5_land_cds__tmean_degC'])
        from shapely.geometry import shape
        from meteorology import hargreaves_pet
        latitude = shape(json.loads(north.geojson.read_text())['features'][0]['geometry']).centroid.y
        expected = hargreaves_pet(*(float(full[f'era5_land_cds__{name}_degC']) for name in ('tmean', 'tmin', 'tmax')),
                                  latitude, north.day)
        self.assertAlmostEqual(float(requested['pet_hargreaves_mm_day']), expected)
        self.assertEqual(requested['aorc_gap_filled'], '')
        readme = (north.root / 'peace_README.md').read_text()
        self.assertNotIn('AORC v1.1', readme)
        self.assertIn('ERA5-Land also supplies the AORC variables', readme)

    def test_deliveries_merge_with_sources_named_and_plot_from_the_merged_table(self):
        import pyarrow.parquet as pq
        aorc, north = self.deliveries()
        from deliver_daily import write_delivery
        write_delivery(north.root / 'peace_full.csv', north.root / 'peace.parquet', north.root / 'peace_README.md')
        output = aorc.root / 'merged' / 'all.parquet'
        merge_deliveries.merge([aorc.root / 'columbia.parquet', north.root / 'peace.parquet'], output,
                               [aorc.geojson, north.geojson])
        table = pq.read_table(output)
        self.assertEqual(table['project_id'].to_pylist(), ['A', 'B'])
        self.assertEqual(table.column_names, pq.read_table(aorc.root / 'columbia.parquet').column_names)
        readme = companion(output, '_README', '.md').read_text()
        self.assertIn('AORC v1.1', readme)
        self.assertIn('all catchments except', readme)
        self.assertIn('ERA5-Land also supplies the AORC variables', readme)
        polygons = json.loads(companion(output, '_catchments', '.geojson').read_text())
        self.assertEqual(sorted(f['properties']['id'] for f in polygons['features']), ['A', 'B'])
        series = plot_daily.load_many(output, {'precipitation_mm', 'tmean_c'})
        self.assertEqual(sorted(series), ['A', 'B'])
        self.assertIn((plot_daily.DELIVERED, 'tmean_c'), series['B'])

    def test_a_table_without_its_manifest_is_taken_as_an_aorc_delivery(self):
        import shutil
        import pyarrow.parquet as pq
        aorc, north = self.deliveries()
        joined = aorc.root / 'joined' / 'columbia_1996_2026.parquet'   # as written by merge_daily.py, no manifest
        joined.parent.mkdir()
        shutil.copy(aorc.root / 'columbia.parquet', joined)
        output = aorc.root / 'merged' / 'all.parquet'
        merge_deliveries.merge([joined, north.root / 'peace.csv'], output, [aorc.geojson, north.geojson])
        self.assertEqual(pq.read_table(output)['project_id'].to_pylist(), ['A', 'B'])
        readme = companion(output, '_README', '.md').read_text()
        self.assertIn('all catchments except', readme)
        manifest = json.loads((aorc.root / 'merged' / 'all.parquet.manifest.json').read_text())
        self.assertEqual(manifest['primary_sources'], {'aorc_v1.1': ['A'], 'era5_land_cds': ['B']})

    def test_a_replaced_catchment_is_named_and_left_out_of_the_fill_counts(self):
        from unittest import mock
        import pyarrow as pa
        import pyarrow.parquet as pq
        from deliver_daily import FILL_COLUMN
        aorc, north = self.deliveries()
        joined = aorc.root / 'joined' / 'columbia_1996_2026.parquet'   # every row flagged, as from debias_era5.py
        joined.parent.mkdir()
        table = pq.read_table(aorc.root / 'columbia.parquet')
        flags = pa.array(['precipitation_mm;tmean_degC'] * table.num_rows, pa.string())
        pq.write_table(table.set_column(table.column_names.index(FILL_COLUMN), FILL_COLUMN, flags), joined)
        output = aorc.root / 'merged' / 'all.parquet'
        with mock.patch.object(merge_deliveries, 'REPLACED', ('A',)):
            merge_deliveries.merge([joined, north.root / 'peace.csv'], output, [aorc.geojson, north.geojson])
        readme = companion(output, '_README', '.md').read_text()
        self.assertIn('mapped onto AORC for', readme)
        self.assertIn('every AORC column therefore holds ERA5-Land quantile-mapped onto AORC of 1996-2006', readme)
        self.assertNotIn('Filled project-days', readme)
        manifest = json.loads((aorc.root / 'merged' / 'all.parquet.manifest.json').read_text())
        self.assertEqual(manifest['aorc_replaced'], {'catchments': ['A'], 'fitted_on': ['1996-01-01', '2006-12-31']})
        merge_deliveries.merge([aorc.root / 'columbia.parquet', north.root / 'peace.csv'],
                               aorc.root / 'plain.parquet', [aorc.geojson, north.geojson])
        self.assertNotIn('quantile-mapped onto AORC of', (aorc.root / 'plain_README.md').read_text())

    def test_overlapping_catchments_are_refused(self):
        aorc, _ = self.deliveries()
        with self.assertRaisesRegex(ValueError, 'already in another input'):
            merge_deliveries.merge([aorc.root / 'columbia.parquet', aorc.root / 'columbia.parquet'],
                                   aorc.root / 'twice.parquet', [aorc.geojson])


if __name__ == '__main__':
    unittest.main()
