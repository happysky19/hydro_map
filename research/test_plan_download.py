"""Checks for catchment and calendar errors that would invalidate extraction."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from plan_download import make_plan


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'catchments.geojson'
        self.feature = {
            'type': 'Feature',
            'properties': {'id': 'A', 'name': 'A', 'part': 'local', 'area': 1},
            'geometry': {'type': 'Polygon', 'coordinates': [
                [[-120, 45], [-119, 45], [-119, 46], [-120, 46], [-120, 45]]]},
        }
        self.config = {'period': {'start': '1996-01-01', 'end': '2025-12-31',
                                  'daily_time_zone': 'UTC'},
                       'pilot_projects': ['A'], 'sources': {}}

    def plan(self, features):
        self.path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}))
        return make_plan(self.path, self.config)

    def test_gregorian_calendar_and_recomputed_area(self):
        result = self.plan([self.feature])
        self.assertEqual(result['expected_gregorian_days_per_project'], 10958)
        self.assertGreater(result['projects'][0]['area_km2'], 8000)
        self.assertEqual(result['projects'][0]['bounds_west_south_east_north'],
                         [-120, 45, -119, 46])

    def test_duplicate_identifiers(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.plan([self.feature, self.feature])

    def test_mixed_catchment_definitions(self):
        second = copy.deepcopy(self.feature)
        second['properties'].update(id='B', part='total')
        with self.assertRaisesRegex(ValueError, 'mix local and total'):
            self.plan([self.feature, second])

    def test_missing_pilot(self):
        self.config['pilot_projects'] = ['B']
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.plan([self.feature])

    def test_boundary_hour_and_daymet_calendar_gaps(self):
        self.config['period'].update(start='1995-01-01', end='2024-12-31')
        result = self.plan([self.feature])
        self.assertEqual(result['hour_ending_precipitation_window_utc'],
                         ['1995-01-01T01:00:00Z', '2025-01-01T00:00:00Z'])
        self.assertEqual(len(result['daymet_calendar_gaps']), 8)
        self.assertEqual(result['daymet_calendar_gaps'][0], '1996-12-31')
        self.assertEqual(result['daymet_calendar_gaps'][-1], '2024-12-31')
        self.assertEqual(result['expected_daymet_records_per_project'], 10950)


if __name__ == '__main__':
    unittest.main()
