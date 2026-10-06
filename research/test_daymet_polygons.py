import math
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from pyproj import Transformer
from shapely.geometry import box
from probe_daymet_polygons import fractional_weights, strict_area_mean, main


class DaymetPolygonTests(unittest.TestCase):
    def test_fractional_edges_and_hole_preserve_constant_field(self):
        polygon = box(250, 250, 1500, 1500).difference(box(700, 700, 800, 800))
        inverse = Transformer.from_crs(6933, 4326, always_xy=True)
        weights, info = fractional_weights(polygon, np.array([500., 1500.]), np.array([1500., 500.]), inverse)
        self.assertEqual(info['fractional_edge_cells'], 4)
        self.assertEqual(info['intersecting_cells'], 4)
        self.assertAlmostEqual(weights.sum(), 1552500., delta=0.1)
        value, area, complete = strict_area_mean(np.full((2, 2), 7.), weights)
        self.assertTrue(complete)
        self.assertEqual(area, 1.)
        self.assertAlmostEqual(value, 7.)

    def test_missing_intersecting_cell_never_renormalizes(self):
        weights = np.array([[1., 3.]])
        value, area, complete = strict_area_mean(np.array([[2., 10.]]), weights)
        self.assertEqual(value, 8.)
        data = np.ma.array([[2., 10.]], mask=[[True, False]])
        value, area, complete = strict_area_mean(data, weights)
        self.assertFalse(complete)
        self.assertEqual(area, .75)
        self.assertTrue(math.isnan(value))
        value, area, complete = strict_area_mean(data, np.array([[0., 3.]]))
        self.assertTrue(complete)
        self.assertEqual(value, 10.)

    def test_cache_only_refuses_missing_and_changed_cache_without_network(self):
        for changed_cache in [False, True]:
            with self.subTest(changed_cache=changed_cache), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                geo = root/'catchments.geojson'
                geo.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
                    'properties':{'id':'OXBOW','part':'local'},'geometry':{'type':'Polygon',
                    'coordinates':[[[-117,45],[-116,45],[-116,46],[-117,46],[-117,45]]]}}]}))
                output = root/'output'; output.mkdir()
                (output/'summary.json').write_text('{"status":"bounded_polygon_pilot_complete"}')
                (output/'polygon_daily_values.csv').write_text('stale output')
                if changed_cache:
                    from probe_daymet_polygons import BASE
                    (output/'grid_2024.dmr').write_text('changed response')
                    (output/'requests.json').write_text(json.dumps({'grid_2024.dmr':{
                        'url':BASE.format(variable='prcp',year=2024)+'.dmr','http_status':'200',
                        'curl_exit_code':0,'sha256':'wrong_hash'}}))
                argv = ['probe','--geojson',str(geo),'--output',str(output),'--projects','OXBOW','--cache-only']
                with patch('sys.argv',argv), patch('probe_daymet_polygons.subprocess.run') as network:
                    with self.assertRaisesRegex(RuntimeError,'cached response'):
                        main()
                    network.assert_not_called()
                self.assertEqual(json.loads((output/'summary.json').read_text())['status'],'running')
                self.assertFalse((output/'polygon_daily_values.csv').exists())

    def test_reject_non_polygon_before_any_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); geo = root/'catchments.geojson'
            geo.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
                'properties':{'id':'OXBOW','part':'local'},'geometry':{'type':'Point','coordinates':[-117,45]}}]}))
            argv = ['probe','--geojson',str(geo),'--output',str(root/'output'),'--projects','OXBOW','--cache-only']
            with patch('sys.argv',argv), patch('probe_daymet_polygons.subprocess.run') as network:
                with self.assertRaisesRegex(ValueError,'Polygon'):
                    main()
                network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
