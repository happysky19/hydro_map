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
        self.feature['properties'].update(forcing_group='A',
            geometry_status='hydrobasins_delineation', shared_outlet_projects=[])
        result = self.plan([self.feature])
        self.assertEqual(result['expected_gregorian_days_per_project'], 10958)
        self.assertGreater(result['projects'][0]['area_km2'], 8000)
        self.assertEqual(result['projects'][0]['bounds_west_south_east_north'],
                         [-120, 45, -119, 46])
        self.assertEqual(result['project_forcing']['A']['shared_outlet_projects'], [])
        self.assertFalse(result['warnings'])

    def test_duplicate_identifiers(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.plan([self.feature, self.feature])

    def test_shared_geometry_file_plans_all_43_projects(self):
        features = []
        for number in range(43):
            feature = copy.deepcopy(self.feature)
            feature['properties'].update(id=f'PROJECT_{number}', name=f'Project {number}',
                                         kind='storage', mw=100, lat=45.5, lon=-119.5,
                                         area=20000, area_local=1, area_geometry=2)
            features.append(feature)
        self.config['pilot_projects'] = ['PROJECT_0', 'PROJECT_42']
        result = self.plan(features)
        self.assertEqual(result['project_count'], 43)
        self.assertEqual(result['forcing_group_count'], 43)
        self.assertEqual(result['expected_project_day_rows'], 43*10958)
        self.assertEqual(len(result['projects']), 43)
        self.assertTrue(all(project['area_km2'] > 8000 for project in result['projects']))

    def test_declared_shared_group_retains_project_mapping_and_warning(self):
        second = copy.deepcopy(self.feature)
        second['properties']['id'] = 'B'
        for feature in (self.feature, second):
            feature['properties'].update(forcing_group='AB',
                geometry_status='shared_unit_approximation', shared_outlet_projects=['A', 'B'])
        result = self.plan([self.feature, second])
        self.assertEqual(result['forcing_group_count'], 1)
        self.assertEqual(result['forcing_groups'], {'AB': ['A', 'B']})
        self.assertEqual(result['project_forcing']['A']['shared_outlet_projects'], ['A', 'B'])
        self.assertEqual(result['projects'][1]['forcing_group'], 'AB')
        self.assertIn('Do not sum', result['warnings'][0])
        second['geometry']['coordinates'][0][1][0] = -118.5
        with self.assertRaisesRegex(ValueError, 'same geometry and part'):
            self.plan([self.feature, second])

    def test_mixed_catchment_definitions(self):
        second = copy.deepcopy(self.feature)
        second['properties'].update(id='B', part='total')
        with self.assertRaisesRegex(ValueError, 'mix local and total'):
            self.plan([self.feature, second])

    def test_tailrace_metadata_retains_intake_outside_filtered_file(self):
        self.feature['properties'].update(catchment_role='natural_reach_at_tailrace',
            diversion_intake_project='UPSTREAM', routing_requires_operations=True)
        result = self.plan([self.feature])
        record = result['project_forcing']['A']
        self.assertEqual(record['catchment_role'], 'natural_reach_at_tailrace')
        self.assertEqual(record['diversion_intake_project'], 'UPSTREAM')
        self.assertIs(record['routing_requires_operations'], True)
        self.assertEqual(result['projects'][0]['diversion_intake_project'], 'UPSTREAM')
        self.assertEqual(result['forcing_groups'], {'A': ['A']})
        self.assertIn('bypassed river', result['warnings'][0])
        self.assertIn('turbine inflow', result['warnings'][0])
        self.assertIn('UPSTREAM', result['warnings'][0])

    def test_invalid_routing_metadata_is_not_silently_dropped(self):
        cases = [
            {'routing_requires_operations': 'true'},
            {'routing_requires_operations': 1},
            {'catchment_role': []},
            {'catchment_role': 'unknown'},
            {'diversion_intake_project': 'UPSTREAM'},
            {'catchment_role': 'natural_reach_at_tailrace'},
            {'catchment_role': 'natural_reach_at_tailrace',
             'diversion_intake_project': 'UPSTREAM', 'routing_requires_operations': False},
            {'catchment_role': 'natural_reach_at_tailrace',
             'diversion_intake_project': None, 'routing_requires_operations': True},
            {'catchment_role': 'natural_reach_at_tailrace',
             'diversion_intake_project': ' UPSTREAM', 'routing_requires_operations': True},
            {'catchment_role': 'natural_reach_at_tailrace',
             'diversion_intake_project': 'A', 'routing_requires_operations': True},
        ]
        for metadata in cases:
            feature = copy.deepcopy(self.feature)
            feature['properties'].update(metadata)
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.plan([feature])

    def test_explicit_ordinary_role_and_boolean_are_preserved(self):
        self.feature['properties'].update(catchment_role='dam_outlet',
                                          routing_requires_operations=False)
        result = self.plan([self.feature])
        self.assertEqual(result['project_forcing']['A']['catchment_role'], 'dam_outlet')
        self.assertIs(result['project_forcing']['A']['routing_requires_operations'], False)
        self.assertFalse(result['warnings'])

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
