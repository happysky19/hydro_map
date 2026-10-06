import csv
import tempfile
import unittest
from pathlib import Path

from hydro_map.csv_export import write_bbox_csv


class CsvExportTests(unittest.TestCase):
    def collection(self, bounds=(0, 0, 1, 1), part="local"):
        west, south, east, north = bounds
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {
                "id": "EXAMPLE", "project_code": "EX", "name": "Example",
                "part": part, "area": -1, "lat": -80, "lon": -100,
            },
            "geometry": {"type": "Polygon", "coordinates": [[
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ]]},
        }]}

    def test_schema_and_values_come_from_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bbox.csv"
            self.assertEqual(write_bbox_csv(self.collection(), path), 1)
            with path.open(newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0], ["ProjectCode", "PolygonName", "MinLatitude", "MaxLatitude",
                             "MinLongitude", "MaxLongitude", "CentroidLatitude", "CentroidLongitude",
                             "AreaKm2", "BufferDegreesApplied"])
            self.assertEqual(rows[1], ["EX", "Example local incremental catchment", "-0.3000", "1.3000",
                             "-0.3000", "1.3000", "0.5000", "0.5000", "12308.8", "0.3"])

    def test_unbuffered_box_rounds_outward_and_total_name_is_correct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bbox.csv"
            collection = self.collection((0.00006, 0.00004, 1.00004, 1.00006), "total")
            del collection["features"][0]["properties"]["project_code"]
            write_bbox_csv(collection, path, buffer_degrees=0)
            with path.open(newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["ProjectCode"], "EXAMPLE")
            self.assertEqual(row["PolygonName"], "Example total upstream catchment")
            self.assertEqual([row[key] for key in ("MinLongitude", "MinLatitude", "MaxLongitude", "MaxLatitude")],
                             ["0.0000", "0.0000", "1.0001", "1.0001"])

    def test_invalid_buffer_cannot_overwrite_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bbox.csv"
            path.write_text("keep")
            for buffer in (-1, float("nan"), float("inf")):
                with self.subTest(buffer=buffer), self.assertRaises(ValueError):
                    write_bbox_csv(self.collection(), path, buffer_degrees=buffer)
                self.assertEqual(path.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
