import importlib
import json
import tempfile
import unittest
from pathlib import Path

import shapefile
from pyproj import CRS, Geod, Transformer
from shapely.geometry import Polygon, mapping
from shapely.ops import transform


class PlottingTests(unittest.TestCase):
    def setUp(self):
        module_path = Path(__file__).parents[1] / 'src/hydro_map/plotting.py'
        self.assertTrue(module_path.exists(), 'The plotting API must exist')
        self.api = importlib.import_module('hydro_map.plotting')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.poly = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)],
                            [[(.5, .5), (.5, 1.5), (1.5, 1.5), (1.5, .5)]])

    def source(self, filename='a.geojson', features=None):
        features = features or [{'type': 'Feature', 'properties': {'id': 'MICA', 'name': 'Mica', 'area': -1}, 'geometry': mapping(self.poly)}]
        path = self.root / filename
        path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}))
        return path

    def test_holes_and_metadata_are_preserved(self):
        feature = self.api.load_features(self.source())[0]
        self.assertEqual(len(feature['geometry'].interiors), 1)
        self.assertEqual(feature['properties']['area'], -1)
        patch = self.api._polygon_patch(feature['geometry'], 'red')
        self.assertEqual(list(patch.get_path().codes).count(1), 2)
        self.assertLess(feature['geometry'].area, 4)

    def test_area_is_computed_and_output_is_saved(self):
        output = self.root / 'map.svg'
        report = self.api.plot_comparison([self.source()], output, project='mica', labels=['Reference'])
        expected = abs(Geod(ellps='WGS84').geometry_area_perimeter(self.poly)[0]) / 1e6
        self.assertAlmostEqual(report['areas_km2']['Reference'], expected, places=6)
        self.assertTrue(output.exists())
        self.assertIn('Mica', output.read_text())

    def test_coordinate_display_changes_axes_but_not_area_or_input(self):
        source = self.source()
        original = source.read_bytes()
        geographic = self.root / 'geographic.svg'
        projected = self.root / 'projected.svg'
        a = self.api.plot_comparison([source], geographic, coordinates='lonlat')
        b = self.api.plot_comparison([source], projected, coordinates='projected')
        self.assertEqual(a['areas_km2'], b['areas_km2'])
        self.assertEqual(source.read_bytes(), original)
        self.assertIn('Longitude', geographic.read_text())
        self.assertIn('Latitude', geographic.read_text())
        self.assertIn('Easting (km)', projected.read_text())
        self.assertIn('Northing (km)', projected.read_text())
        self.assertEqual(a['coordinate_display'], 'lonlat')

    def test_invalid_coordinate_display_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'coordinates'):
            self.api.plot_comparison([self.source()], self.root / 'out.png', coordinates='unknown')

    def test_project_must_exist_in_every_overlay(self):
        a = self.source()
        b = self.source('b.geojson', [{'type':'Feature','properties':{'id':'other','name':'Other'},'geometry':mapping(self.poly)}])
        with self.assertRaisesRegex(ValueError, 'project|Project'):
            self.api.plot_comparison([a, b], self.root / 'out.png', project='MICA')

    def test_label_count_is_validated(self):
        with self.assertRaisesRegex(ValueError, 'label'):
            self.api.plot_comparison([self.source()], self.root / 'out.png', labels=['one', 'two'])

    def test_shapefile_is_reprojected_and_requires_prj(self):
        path = self.root / 'projected.shp'
        forward = Transformer.from_crs(4326, 3857, always_xy=True).transform
        projected = transform(forward, Polygon([(0,0),(1,0),(1,1),(0,1)]))
        with shapefile.Writer(str(path)) as writer:
            writer.field('id', 'C')
            writer.field('name', 'C')
            writer.poly([list(projected.exterior.coords)[::-1]])
            writer.record('MICA','Mica')
        with self.assertRaisesRegex(ValueError, 'prj'):
            self.api.load_features(path)
        path.with_suffix('.prj').write_text(CRS.from_epsg(3857).to_wkt())
        g = self.api.load_features(path)[0]['geometry']
        for actual, expected in zip(g.bounds, (0,0,1,1)):
            self.assertAlmostEqual(actual, expected, places=7)

    def test_non_polygon_is_rejected(self):
        path = self.root / 'point.geojson'
        path.write_text(json.dumps({'type':'Point','coordinates':[0,0]}))
        with self.assertRaisesRegex(ValueError, 'olygon'):
            self.api.load_features(path)

    def test_batch_rejects_unmatched_overlay_and_filename_collision(self):
        a = self.source()
        b = self.source('b.geojson', [{'type':'Feature','properties':{'id':'other'},'geometry':mapping(self.poly)}])
        with self.assertRaisesRegex(ValueError, 'match|project|Project'):
            self.api.plot_by_project([a,b], self.root / 'batch')
        generated = self.api.plot_by_project([a], self.root / 'batch', labels=['Reference'])
        self.assertEqual(len(generated),1)
        self.assertTrue(generated[0].exists())
        with self.assertRaises(FileExistsError):
            self.api.plot_by_project([a], self.root / 'batch', labels=['Reference'])


if __name__ == '__main__':
    unittest.main()
