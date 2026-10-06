import tempfile
import unittest
from pathlib import Path

from hydro_map.config import load_config


class ConfigTests(unittest.TestCase):
    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.yaml"
            path.write_text(body)
            return load_config(path)

    def test_cell_delineation_requires_explicit_grid_points_and_allows_same_unit(self):
        body = """
dataset: {region: na, delineation: outlet_cell}
projects:
  - {id: A, name: A, outlet: {hybas_id: 101, lon: -118, lat: 52, grid_lon: -118.001, grid_lat: 52.001, grid_reference: Reviewed river cell}}
  - {id: B, name: B, outlet: {hybas_id: 101, lon: -118, lat: 51, grid_lon: -118.001, grid_lat: 51.001, grid_reference: Reviewed river cell}}
"""
        with self.assertRaisesRegex(ValueError, 'consistent spelling'):
            self.load(body.replace('name: A,', 'name: A, outlet_group: PAIR,').replace(
                'name: B,', 'name: B, outlet_group: pair,'))
        result = self.load(body)
        self.assertEqual(result.dataset.delineation, 'outlet_cell')
        self.assertEqual(result.projects[0].grid_lon, -118.001)
        with self.assertRaisesRegex(ValueError, 'grid'):
            self.load(body.replace(', grid_lat: 52.001', ''))
        with self.assertRaisesRegex(ValueError, 'virtual'):
            self.load(body.replace('delineation: outlet_cell', 'delineation: outlet_cell, include_virtual_connections: true'))
        with self.assertRaisesRegex(ValueError, 'delineation'):
            self.load(body.replace('outlet_cell', 'unknown_method'))

    def test_diversion_return_requires_explicit_role_intake_and_operations(self):
        body = """
dataset: {region: na, delineation: outlet_cell}
projects:
  - {id: UPPER, name: Upper, outlet: {hybas_id: 1, lon: -118, lat: 52, grid_lon: -118, grid_lat: 52, grid_reference: River}}
  - id: RETURN
    name: Return
    catchment_role: natural_reach_at_tailrace
    diversion_intake_project: UPPER
    routing_requires_operations: true
    outlet: {hybas_id: 1, lon: -118, lat: 51, grid_lon: -118, grid_lat: 51, grid_reference: River return}
"""
        result = self.load(body)
        self.assertTrue(result.projects[1].metadata['routing_requires_operations'])
        for original, replacement in [
            ('routing_requires_operations: true', 'routing_requires_operations: false'),
            ('routing_requires_operations: true', "routing_requires_operations: 'true'"),
            ('diversion_intake_project: UPPER', 'diversion_intake_project: MISSING'),
            ('diversion_intake_project: UPPER', 'diversion_intake_project: RETURN'),
            ('catchment_role: natural_reach_at_tailrace', 'catchment_role: dam_outlet'),
        ]:
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.load(body.replace(original, replacement))

    def test_duplicate_outlet_units_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "same.*unit"):
            self.load("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: UPPER, name: Upper, outlet: {hybas_id: 101, lon: -118, lat: 52}}
  - {id: LOWER, name: Lower, outlet: {hybas_id: 101, lon: -118, lat: 51}}
""")

    def test_shared_unit_requires_one_explicit_group_for_every_member(self):
        result = self.load("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: A, name: A, outlet_group: AB, outlet: {hybas_id: 101, lon: -118, lat: 52}}
  - {id: B, name: B, outlet_group: AB, outlet: {hybas_id: 101, lon: -118, lat: 51}}
""")
        self.assertEqual([p.metadata['outlet_group'] for p in result.projects], ['AB', 'AB'])
        for second in ('', 'outlet_group: OTHER, '):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'same.*unit'):
                self.load(f"""
dataset: {{region: na}}
projects:
  - {{id: A, name: A, outlet_group: AB, outlet: {{hybas_id: 101, lon: -118, lat: 52}}}}
  - {{id: B, name: B, {second}outlet: {{hybas_id: 101, lon: -118, lat: 51}}}}
""")

    def test_group_cannot_span_different_units(self):
        with self.assertRaisesRegex(ValueError, 'group.*different'):
            self.load("""
dataset: {region: na}
projects:
  - {id: A, name: A, outlet_group: AB, outlet: {hybas_id: 101, lon: -118, lat: 52}}
  - {id: B, name: B, outlet_group: AB, outlet: {hybas_id: 102, lon: -118, lat: 51}}
""")

    def test_string_false_cannot_enable_virtual_connections(self):
        with self.assertRaises(ValueError):
            self.load("""
dataset: {region: na, level: 12, version: '1c', include_virtual_connections: 'false'}
projects:
  - {id: A, name: A, outlet: {hybas_id: 101, lon: -118, lat: 52}}
""")

    def test_unknown_field_does_not_silently_change_selection(self):
        with self.assertRaises(ValueError):
            self.load("""
dataset: {region: na, level: 12, version: '1c', include_virtal_connections: true}
projects:
  - {id: A, name: A, outlet: {hybas_id: 101, lon: -118, lat: 52}}
""")

    def test_default_excludes_virtual_connections_and_keeps_source(self):
        result = self.load("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - id: A
    project_code: AX
    name: Example Dam
    river: Example River
    outlet: {hybas_id: 101, lon: -118.5, lat: 52, source: 'https://example.org/data'}
""")
        self.assertFalse(result.dataset.include_virtual_connections)
        self.assertEqual(result.projects[0].hybas_id, 101)
        self.assertEqual(result.projects[0].source, "https://example.org/data")
        self.assertEqual(result.projects[0].metadata["project_code"], "AX")

    def test_duplicate_project_codes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "project code"):
            self.load("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: A, project_code: SAME, name: A, outlet: {hybas_id: 101, lon: -118, lat: 52}}
  - {id: B, project_code: SAME, name: B, outlet: {hybas_id: 102, lon: -118, lat: 51}}
""")


if __name__ == "__main__":
    unittest.main()
