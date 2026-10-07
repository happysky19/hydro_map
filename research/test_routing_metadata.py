"""Routing and shared forcing-group metadata stay explicit through extraction."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from hydro_map.plotting import load_features
from routing_metadata import forcing_metadata


class ForcingMetadataTests(unittest.TestCase):
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

    def plan(self, features):
        self.path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}))
        return forcing_metadata(load_features(self.path))

    def test_duplicate_identifiers(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.plan([self.feature, self.feature])

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
        self.assertEqual(result['project_forcing']['B']['forcing_group'], 'AB')
        self.assertIn('Do not sum', result['warnings'][0])
        second['geometry']['coordinates'][0][1][0] = -118.5
        with self.assertRaisesRegex(ValueError, 'same geometry and part'):
            self.plan([self.feature, second])

    def test_tailrace_metadata_retains_intake_outside_filtered_file(self):
        self.feature['properties'].update(catchment_role='natural_reach_at_tailrace',
            diversion_intake_project='UPSTREAM', routing_requires_operations=True)
        result = self.plan([self.feature])
        record = result['project_forcing']['A']
        self.assertEqual(record['catchment_role'], 'natural_reach_at_tailrace')
        self.assertEqual(record['diversion_intake_project'], 'UPSTREAM')
        self.assertIs(record['routing_requires_operations'], True)
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


if __name__ == '__main__':
    unittest.main()
