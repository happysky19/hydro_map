import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from pyproj import CRS
import rasterio
from rasterio.transform import from_origin
import shapefile

from hydro_map.cli import main


class CliTests(unittest.TestCase):
    def cell_config(self, root):
        config = root / "cell.yaml"
        config.write_text("""
dataset: {region: na, delineation: outlet_cell}
projects:
  - id: DAM
    name: Dam
    outlet: {hybas_id: 1, lon: 0.5, lat: 0.5, grid_lon: 0.5, grid_lat: 0.5, grid_reference: 'checked channel cell'}
""")
        return config

    def test_download_cell_mode_includes_both_flow_rasters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.cell_config(root)
            source, direction, accumulation = [root / name for name in ("units.shp", "dir.tif", "aca.tif")]
            output = io.StringIO()
            with patch("hydro_map.data.download_dataset", return_value=source), \
                    patch("hydro_map.data.download_flow_dataset", return_value=(direction, accumulation)), \
                    contextlib.redirect_stdout(output):
                status = main(["download", "--config", str(config), "--cache-dir", str(root)])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), list(map(str, (source, direction, accumulation))))

    def test_cell_options_and_virtual_connections_are_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.cell_config(root)
            output = root / "result.geojson"
            for options in (["--flow-direction", str(root / "dir.tif")],
                            ["--flow-accumulation", str(root / "aca.tif")],
                            ["--include-virtual"]):
                with self.subTest(options=options), \
                        patch("hydro_map.data.download_dataset", side_effect=AssertionError("invalid options must not download")), \
                        contextlib.redirect_stderr(io.StringIO()):
                    status = main(["build", str(config), "--output", str(output), *options])
                self.assertEqual(status, 2)
                self.assertFalse(output.exists())

    def test_cell_output_cannot_overwrite_either_raster_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.cell_config(root)
            direction, accumulation = root / "dir.tif", root / "aca.tif"
            direction.write_bytes(b"direction input")
            accumulation.write_bytes(b"accumulation input")
            base = ["build", str(config), "--source", str(root / "units.shp"),
                    "--flow-direction", str(direction), "--flow-accumulation", str(accumulation)]
            for raster in (direction, accumulation):
                original = raster.read_bytes()
                for options in (["--output", str(raster)],
                                ["--output", str(root / "out.geojson"), "--csv-output", str(raster)],
                                ["--output", str(root / "out.geojson"), "--table-output", str(raster)]):
                    with self.subTest(raster=raster, options=options), \
                            contextlib.redirect_stderr(io.StringIO()):
                        status = main([*base, *options])
                    self.assertEqual(status, 2)
                    self.assertEqual(raster.read_bytes(), original)
                    self.assertFalse((root / "out.geojson").exists())

    def test_cell_build_uses_explicit_or_automatically_fetched_rasters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.cell_config(root)
            config.write_text(config.read_text().replace('    name: Dam\n',
                '    name: Dam\n    catchment_role: natural_reach_at_tailrace\n'
                '    diversion_intake_project: UPSTREAM\n    routing_requires_operations: true\n') +
                f'  - id: UPSTREAM\n    name: Upstream\n'
                f'    outlet: {{hybas_id: 1, lon: 0.5, lat: {0.5 + 2 / 240}, '
                f'grid_lon: 0.5, grid_lat: {0.5 + 2 / 240}, grid_reference: checked channel cell}}\n')
            source = root / "units.shp"
            with shapefile.Writer(str(source)) as writer:
                for field in ("HYBAS_ID", "NEXT_DOWN", "ENDO"):
                    writer.field(field, "N", 12)
                writer.poly([[(0.48, 0.48), (0.48, 0.52), (0.52, 0.52), (0.52, 0.48), (0.48, 0.48)]])
                writer.record(1, 0, 0)
            source.with_suffix(".prj").write_text(CRS.from_epsg(4326).to_wkt())
            direction, accumulation = root / "dir.tif", root / "aca.tif"
            flow = np.full((7, 7), 255, dtype=np.uint8)
            flow[1:4, 2], flow[4, 2] = 4, 0
            transform = from_origin(0.5 - 2.5 / 240, 0.5 + 3.5 / 240, 1 / 240, 1 / 240)
            for path, array, nodata in ((direction, flow, 255),
                    (accumulation, np.ones((7, 7), dtype=np.uint32), 4294967295)):
                with rasterio.open(path, "w", driver="GTiff", width=7, height=7,
                                   count=1, dtype=array.dtype, crs="EPSG:4326",
                                   transform=transform, nodata=nodata) as raster:
                    raster.write(array, 1)
            for explicit in (True, False):
                output = root / f"cell_{explicit}.geojson"
                messages = io.StringIO()
                options = (["--flow-direction", str(direction), "--flow-accumulation", str(accumulation)]
                           if explicit else [])
                with self.subTest(explicit=explicit), \
                        patch("hydro_map.data.download_flow_dataset", return_value=(direction, accumulation),
                              side_effect=AssertionError("explicit rasters must work offline") if explicit else None), \
                        contextlib.redirect_stdout(messages):
                    status = main(["build", str(config), "--source", str(source),
                                   "--output", str(output), *options])
                self.assertEqual(status, 0)
                feature = json.loads(output.read_text())["features"][0]
                self.assertEqual(feature["properties"]["cell_count_total"], 3)
                self.assertIn('bypassed river', messages.getvalue())
                self.assertIn('UPSTREAM', messages.getvalue())
                self.assertIn('turbine inflow', messages.getvalue())

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
            table_output = root / "catchments.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["build", str(config), "--source", str(source),
                               "--output", str(output), "--include-virtual", "--csv-output", str(csv_output),
                               "--table-output", str(table_output)])
            self.assertEqual(status, 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["features"][0]["properties"]["id"], "DAM")
            self.assertTrue(result["metadata"]["include_virtual_connections"])
            self.assertEqual(result["features"][0]["properties"]["part"], "local")
            with csv_output.open(newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["ProjectCode"], "DAM")
            self.assertEqual(row["AreaKm2"], "12308.8")
            with table_output.open(newline='') as stream:
                table_row = next(csv.DictReader(stream))
            self.assertEqual(table_row['id'], 'DAM')
            self.assertTrue(table_row['geometry'].startswith('POLYGON'))

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
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["build", str(config), "--source", str(source), "--output", str(output),
                               "--table-output", str(output)])
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
