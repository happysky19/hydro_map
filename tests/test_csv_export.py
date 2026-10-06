import csv
import tempfile
import unittest
from pathlib import Path

from hydro_map.csv_export import write_bbox_csv, write_geometry_csv
from shapely import from_wkt
from shapely.geometry import shape


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

    def test_geometry_table_preserves_attributes_and_polygon_in_one_file(self):
        collection = self.collection()
        props = collection['features'][0]['properties']
        props.update(kind='storage', mw=120, area=20000, area_total=20000,
                     area_local=12308.7783615, area_geometry=12308.7783615,
                     forcing_group='EXAMPLE', geometry_status='hydrobasins_delineation',
                     shared_outlet_projects=[])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'catchments.csv'
            write_geometry_csv(collection, output)
            with output.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['id'], 'EXAMPLE')
            self.assertEqual(row['kind'], 'storage')
            self.assertEqual(float(row['mw']), 120)
            self.assertEqual(float(row['area']), 20000)
            self.assertGreater(float(row['area']), float(row['area_local']))
            self.assertTrue(from_wkt(row['geometry']).equals(shape(collection['features'][0]['geometry'])))
            self.assertEqual(row['shared_outlet_projects'], '[]')

    def test_bbox_table_labels_shared_areas_without_extra_columns(self):
        collection = self.collection()
        collection['features'][0]['properties'].update(
            forcing_group='PAIR', geometry_status='shared_unit_approximation')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'bbox.csv'
            write_bbox_csv(collection, output)
            with output.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(len(row), 10)
            self.assertEqual(row['PolygonName'], 'Example shared local catchment [PAIR]')

    def test_diversion_metadata_survives_csv_and_labels_the_natural_reach(self):
        collection = self.collection()
        collection['features'][0]['properties'].update(
            catchment_role='natural_reach_at_tailrace', diversion_intake_project='UPSTREAM',
            routing_requires_operations=True)
        with tempfile.TemporaryDirectory() as directory:
            table, bbox = Path(directory) / 'catchments.csv', Path(directory) / 'bbox.csv'
            write_geometry_csv(collection, table)
            with table.open(newline='') as stream:
                reader = csv.DictReader(stream)
                row = next(reader)
            self.assertEqual(reader.fieldnames[:10],
                             ['id', 'name', 'kind', 'mw', 'lat', 'lon', 'area', 'area_local', 'part', 'geometry'])
            self.assertEqual(reader.fieldnames[-3:],
                             ['catchment_role', 'diversion_intake_project', 'routing_requires_operations'])
            self.assertEqual(row['catchment_role'], 'natural_reach_at_tailrace')
            self.assertEqual(row['diversion_intake_project'], 'UPSTREAM')
            self.assertEqual(row['routing_requires_operations'], 'true')
            write_bbox_csv(collection, bbox)
            with bbox.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(len(row), 10)
            self.assertEqual(row['PolygonName'], 'Example local natural reach catchment at tailrace')


if __name__ == "__main__":
    unittest.main()
