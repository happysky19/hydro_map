import tempfile
import unittest
from pathlib import Path

import shapefile
from pyproj import CRS
from shapely.geometry import shape

from hydro_map.basins import build_catchments
from hydro_map.config import load_config


class BasinTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "basins.shp"
        # Two tributaries meet at unit 3. Unit 4 has a virtual link to 3.
        self.rows = [(1, 3, 0, 0, 1), (2, 3, 0, 1, 1),
                     (3, 0, 0, 0, 0), (4, 3, 2, 1, 0)]
        self.write_source()
        self.yaml = self.root / "projects.yaml"
        self.yaml.write_text("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: UPPER, name: Upper, outlet: {hybas_id: 1, lon: 0.5, lat: 1.5}}
  - {id: LOWER, name: Lower, outlet: {hybas_id: 3, lon: 0.5, lat: 0.5}}
""")

    def write_source(self):
        with shapefile.Writer(str(self.source)) as writer:
            writer.field("HYBAS_ID", "N", 12)
            writer.field("NEXT_DOWN", "N", 12)
            writer.field("ENDO", "N", 3)
            for unit, downstream, endo, x, y in self.rows:
                writer.poly([[(x, y), (x, y + 1), (x + 1, y + 1),
                              (x + 1, y), (x, y)]])
                writer.record(unit, downstream, endo)
        self.source.with_suffix(".prj").write_text(CRS.from_epsg(4326).to_wkt())

    def build(self, **kwargs):
        return build_catchments(load_config(self.yaml), self.source, **kwargs)

    def test_local_area_removes_selected_upstream_but_keeps_other_branch(self):
        collection = self.build()
        features = {f["id"]: f for f in collection["features"]}
        lower = features["LOWER"]["properties"]
        self.assertEqual(lower["hybas_ids"], [2, 3])
        self.assertEqual(lower["up"], ["UPPER"])
        self.assertEqual(lower["above"], ["UPPER"])
        self.assertIsNone(lower["down"])
        self.assertEqual(features["UPPER"]["properties"]["down"], "LOWER")
        upper_geom = shape(features["UPPER"]["geometry"])
        lower_geom = shape(features["LOWER"]["geometry"])
        self.assertAlmostEqual(upper_geom.intersection(lower_geom).area, 0)
        self.assertAlmostEqual(lower_geom.area, 2)
        self.assertAlmostEqual(lower["area"], 24613.9071125, places=4)

    def test_virtual_link_is_an_explicit_topology_choice(self):
        from dataclasses import replace
        config = load_config(self.yaml)
        config = replace(config, dataset=replace(config.dataset,
                         include_virtual_connections=True))
        result = build_catchments(config, self.source)
        self.assertEqual(result["features"][1]["properties"]["hybas_ids"], [2, 3, 4])
        self.assertTrue(result["metadata"]["include_virtual_connections"])

    def test_output_filter_preserves_upstream_cutoffs(self):
        result = self.build(project_ids=["lower"])
        self.assertEqual(len(result["features"]), 1)
        self.assertEqual(result["features"][0]["properties"]["hybas_ids"], [2, 3])

    def test_total_area_includes_selected_upstream_dam(self):
        result = self.build(part="total", project_ids=["LOWER"])
        props = result["features"][0]["properties"]
        self.assertEqual(props["hybas_ids"], [1, 2, 3])
        self.assertGreater(props["area_total"], props["area_local"])
        self.assertEqual(props["area"], props["area_total"])

    def test_nested_projects_are_not_subtracted_twice(self):
        self.rows[2] = (3, 5, 0, 0, 0)
        self.rows.append((5, 0, 0, 0, -1))
        self.write_source()
        self.yaml.write_text(self.yaml.read_text() +
            "  - {id: LAST, name: Last, outlet: {hybas_id: 5, lon: 0.5, lat: -0.5}}\n")
        result = self.build(project_ids=["LAST"])["features"][0]["properties"]
        self.assertEqual(result["hybas_ids"], [5])
        self.assertEqual(result["up"], ["LOWER"])
        self.assertEqual(result["above"], ["UPPER", "LOWER"])
        self.assertAlmostEqual(result["area_local"], 12308.7783615, places=4)

    def test_wrong_outlet_point_fails_instead_of_snapping(self):
        self.yaml.write_text(self.yaml.read_text().replace("lon: 0.5, lat: 1.5", "lon: 5, lat: 5"))
        with self.assertRaisesRegex(ValueError, "UPPER.*outside"):
            self.build()

    def test_missing_unit_and_unknown_project_fail(self):
        with self.assertRaises(ValueError):
            self.build(project_ids=["TYPO"])
        self.yaml.write_text(self.yaml.read_text().replace("hybas_id: 1,", "hybas_id: 999,"))
        with self.assertRaisesRegex(ValueError, "999"):
            self.build()

    def test_cycle_is_rejected(self):
        self.rows[2] = (3, 1, 0, 0, 0)
        self.write_source()
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.build()

    def test_dangling_downstream_id_is_rejected(self):
        self.rows[2] = (3, 999, 0, 0, 0)
        self.write_source()
        with self.assertRaisesRegex(ValueError, "999"):
            self.build()


if __name__ == "__main__":
    unittest.main()
