"""ARCO-ERA5 responses have the CDS layout: area subset, west-to-east longitudes and hourly times."""

from contextlib import redirect_stdout
import csv
from datetime import date
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numcodecs
import numpy as np

import arco_era5
from download_cds import make_requests, read_response, run_pipeline


def fake_store(path):
    """Encoded chunks: time values equal their index; fields equal the hour index or are constant."""
    codec = numcodecs.Blosc()
    array, key = path.split('/')
    if array == 'time':
        first = int(key) * arco_era5.TIME_CHUNK
        return codec.encode(np.arange(first, first + arco_era5.TIME_CHUNK, dtype='<i8'))
    index = int(key.split('.')[0])
    shape = (len(arco_era5.LATITUDE), len(arco_era5.LONGITUDE))
    value = {'total_cloud_cover': .5, 'zero_degree_level': float(index % 1000),
             'geopotential_at_surface': 100 * 9.80665}[array]
    return codec.encode(np.full(shape, value, dtype='<f4'))


class ArcoTests(unittest.TestCase):
    def test_response_matches_cds_request_layout(self):
        client = arco_era5.ArcoClient(workers=2)
        request = make_requests('era5', date(2025, 12, 30), [50.25, -120.25, 49.75, -119.75], date(2025, 12, 31))[0]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(client, '_get', side_effect=lambda path: client.codec.decode(fake_store(path))):
            target = Path(directory) / 'response.nc'
            client.retrieve(request['dataset'], request['request'], target)
            fields = read_response(target, request['fields'])
        np.testing.assert_allclose(fields['tcc']['longitude'], [-120.25, -120., -119.75])
        np.testing.assert_allclose(fields['tcc']['latitude'], [50.25, 50., 49.75])
        self.assertEqual(fields['deg0l']['data'].shape, (48, 3, 3))
        first = int((arco_era5.datetime(2025, 12, 30, tzinfo=arco_era5.timezone.utc)
                     - arco_era5.EPOCH).total_seconds()) // 3600
        np.testing.assert_allclose(fields['deg0l']['data'][:, 0, 0], (first + np.arange(48)) % 1000)
        self.assertTrue(np.all(fields['z']['data'] == np.float32(100 * 9.80665)))

    def test_pipeline_runs_on_arco_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            geometry = root / 'catchments.geojson'
            geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
                'type': 'Feature', 'properties': {'id': 'A', 'name': 'A', 'part': 'local'},
                'geometry': {'type': 'Polygon', 'coordinates': [[[-120.1, 49.9], [-119.9, 49.9],
                              [-119.9, 50.1], [-120.1, 50.1], [-120.1, 49.9]]]}}]}))
            client = arco_era5.ArcoClient(workers=2)
            with patch.object(client, '_get', side_effect=lambda path: client.codec.decode(fake_store(path))), \
                    redirect_stdout(io.StringIO()):
                run_pipeline(geometry, 'era5', root / 'era5', root / 'cache', '2025-12-31', '2025-12-31', client=client)
            with gzip.open(root / 'era5/daily_2025-12.csv.gz', 'rt') as stream:
                rows = {row['variable']: row for row in csv.DictReader(stream)}
            self.assertEqual(rows['cloud_cover_fraction']['qc'], 'valid')
            self.assertAlmostEqual(float(rows['cloud_cover_fraction']['value']), .5)
            manifest = json.loads((root / 'cache/requests.manifest.json').read_text())
            self.assertTrue(all('ARCO' in record['provider'] for record in manifest.values()))
            self.assertIn('ARCO', json.loads((root / 'era5/run.json').read_text())['methods']['provider'])


if __name__ == '__main__':
    unittest.main()
