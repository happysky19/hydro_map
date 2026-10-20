"""ERA5-only download: both products exported without AORC, and a repeat reads nothing again."""

from contextlib import redirect_stdout
import csv
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download_era5
from test_download_cds import FakeClient


class DownloadEra5Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.geojson = self.root / 'catchments.geojson'
        self.geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[dict(
            type='Feature', properties=dict(id='A', name='A', part='local'), geometry=dict(
                type='Polygon', coordinates=[[[-120.03, 49.97], [-119.97, 49.97], [-119.97, 50.03],
                                              [-120.03, 50.03], [-120.03, 49.97]]]))])))
        self.output = self.root / 'era5.csv'
        self.days = date(2025, 12, 31), date(2026, 1, 1)

    def test_both_products_exported_and_resumed(self):
        client = FakeClient()
        with patch('arco_era5.ArcoClient', return_value=client), redirect_stdout(io.StringIO()):
            report = download_era5.run(self.geojson, *self.days, self.output, land_source='cds', cds_client=client)
            calls = client.calls
            with patch.object(client, 'retrieve', side_effect=AssertionError('Retrieved again')):
                download_era5.run(self.geojson, *self.days, self.output, land_source='cds', cds_client=client)
        self.assertEqual(client.calls, calls)
        self.assertEqual(report['row_count'], 2)
        with self.output.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row['date'] for row in rows], ['2025-12-31', '2026-01-01'])
        values = [name for name in rows[0] if name not in ('date', 'project_id')]
        self.assertTrue(values)
        self.assertTrue(all(name.startswith(('era5_land_cds__', 'era5_cds__')) for name in values))
        self.assertEqual(float(rows[1]['era5_land_cds__precipitation_mm']), 2.)
        self.assertEqual(float(rows[1]['era5_cds__cloud_cover_fraction']), .5)
        self.assertTrue(Path(str(self.output) + '.manifest.json').is_file())

    def test_missing_cds_configuration_stops_before_downloading(self):
        with patch('cdsapi.Client', side_effect=Exception('Missing/incomplete configuration file')), \
             patch('download_cds.run_pipeline', side_effect=AssertionError('Download started')):
            with self.assertRaisesRegex(Exception, 'configuration'):
                download_era5.run(self.geojson, *self.days, self.output)


if __name__ == '__main__':
    unittest.main()
