"""Check accumulation endpoints and strict missing-hour behavior."""

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from probe_era5_land import DAY, daily_fields, main


class DailyFieldsTests(unittest.TestCase):
    def setUp(self):
        self.times = np.array([(DAY + timedelta(hours=h)).timestamp() for h in range(25)])
        self.arrays = {'tp': np.arange(25, dtype=float)[:, None, None] / 1000,
                       't2m': np.full((25, 1, 1), 273.15),
                       'sd': np.full((25, 1, 1), .2)}
        self.arrays['tp'][0] = .1  # Previous day's accumulation is excluded.

    def test_endpoint_not_sum(self):
        result = daily_fields(self.arrays, self.times)
        self.assertEqual(result['precipitation_mm'].item(), 24)
        self.assertAlmostEqual(result['temperature_c'].item(), 0)
        self.assertAlmostEqual(result['swe_mm'].item(), 200)

    def test_missing_boundary_rejected(self):
        with self.assertRaisesRegex(ValueError, '25 consecutive'):
            daily_fields(self.arrays, self.times[:-1])

    def test_missing_state_hour_stays_missing(self):
        self.arrays['t2m'][3] = np.nan
        self.assertTrue(np.isnan(daily_fields(self.arrays, self.times)['temperature_c'].item()))

    def test_failed_cache_replay_cannot_leave_a_complete_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            geojson = root / 'polygon.geojson'
            geojson.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
                {'type': 'Feature', 'properties': {'id': 'A'}, 'geometry': {'type': 'Polygon',
                 'coordinates': [[[-120, 45], [-119, 45], [-119, 46], [-120, 46], [-120, 45]]]}}
            ]}))
            (root / 'summary.json').write_text('{"status":"bounded_polygon_pilot_complete"}')
            argv = ['probe', '--geojson', str(geojson), '--output', str(root), '--cache-only']
            with patch('sys.argv', argv), patch('probe_era5_land.requests.get') as network:
                with self.assertRaisesRegex(ValueError, 'Missing or invalid cached'):
                    main()
                network.assert_not_called()
            self.assertEqual(json.loads((root / 'summary.json').read_text())['status'], 'running')


if __name__ == '__main__':
    unittest.main()
