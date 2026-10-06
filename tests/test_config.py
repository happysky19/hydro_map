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

    def test_duplicate_outlet_units_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "same.*unit"):
            self.load("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: UPPER, name: Upper, outlet: {hybas_id: 101, lon: -118, lat: 52}}
  - {id: LOWER, name: Lower, outlet: {hybas_id: 101, lon: -118, lat: 51}}
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
