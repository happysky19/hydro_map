import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from pyproj import CRS
import shapefile

from hydro_map.cli import main


class CliTests(unittest.TestCase):
    def test_offline_build_writes_selected_geojson_with_recorded_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "units.shp"
            with shapefile.Writer(str(source)) as writer:
                writer.field("HYBAS_ID", "N", 12)
                writer.field("NEXT_DOWN", "N", 12)
                writer.field("ENDO", "N", 3)
                writer.poly([[(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]])
                writer.record(1, 0, 0)
            source.with_suffix(".prj").write_text(CRS.from_epsg(4326).to_wkt())
            config = root / "project.yaml"
            config.write_text("""
dataset: {region: na, level: 12, version: '1c'}
projects:
  - {id: DAM, name: Dam, outlet: {hybas_id: 1, lon: 0.5, lat: 0.5}}
""")
            output = root / "result.geojson"
            csv_output = root / "bbox.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["build", str(config), "--source", str(source),
                               "--output", str(output), "--include-virtual", "--csv-output", str(csv_output)])
            self.assertEqual(status, 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["features"][0]["properties"]["id"], "DAM")
            self.assertTrue(result["metadata"]["include_virtual_connections"])
            self.assertEqual(result["features"][0]["properties"]["part"], "local")
            with csv_output.open(newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["ProjectCode"], "DAM")
            self.assertEqual(row["AreaKm2"], "12308.8")

            component = source.with_suffix(".dbf")
            original = component.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = main(["build", str(config), "--source", str(source), "--output", str(component)])
            self.assertNotEqual(status, 0)
            self.assertEqual(component.read_bytes(), original)
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["build", str(config), "--source", str(source), "--output", str(output),
                               "--csv-output", str(component)])
            self.assertNotEqual(status, 0)
            self.assertEqual(component.read_bytes(), original)
            original_geojson = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["build", str(config), "--source", str(source), "--output", str(output),
                               "--csv-output", str(output)])
            self.assertNotEqual(status, 0)
            self.assertEqual(output.read_bytes(), original_geojson)

    def test_build_error_does_not_create_an_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.geojson"
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["build", str(Path(directory) / "missing.yaml"), "--output", str(output)])
            self.assertNotEqual(status, 0)
            self.assertFalse(output.exists())

    def test_overview_does_not_replace_a_project_with_the_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "projects.geojson"
            source.write_text(json.dumps({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"id": "overview", "name": "Overview Dam"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            }]}))
            output = root / "figures"
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["plot", str(source), "--by-project", "--output-dir", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(len(list(output.glob("*.png"))), 2)


if __name__ == "__main__":
    unittest.main()
