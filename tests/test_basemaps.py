import importlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from matplotlib.figure import Figure
from pyproj import CRS, Transformer


class BasemapTests(unittest.TestCase):
    def setUp(self):
        module_path = Path(__file__).parents[1] / 'src/hydro_map/basemaps.py'
        self.assertTrue(module_path.exists(), 'The optional basemap API must exist')
        self.api = importlib.import_module('hydro_map.basemaps')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name) / 'tiles'

    def test_none_needs_no_optional_dependency_or_cache(self):
        with patch.dict(sys.modules, {'contextily': None}):
            self.assertEqual(self.api.add_basemap(None, None, 'none', self.cache), '')
        self.assertFalse(self.cache.exists())

    def test_unknown_style_is_rejected_before_optional_import(self):
        with patch.dict(sys.modules, {'contextily': None}):
            with self.assertRaisesRegex(ValueError, 'style'):
                self.api.add_basemap(None, 'EPSG:4326', 'unknown', self.cache)

    def test_missing_dependency_explains_installation(self):
        with patch.dict(sys.modules, {'contextily': None}):
            with self.assertRaisesRegex(ValueError, r"pip install -e '\.\[maps\]'"):
                self.api.add_basemap(None, 'EPSG:4326', 'terrain', self.cache)

    @unittest.skipUnless(importlib.util.find_spec('contextily'), 'requires maps extra')
    def test_downloaded_tiles_warp_to_axes_crs_and_reuse_cache(self):
        import contextily as cx
        from PIL import Image
        from requests import Response

        stream = io.BytesIO()
        Image.new('RGB', (32, 32), '#d9e4cb').save(stream, format='PNG')
        response = Response()
        response.status_code = 200
        response._content = stream.getvalue()
        local = CRS.from_proj4('+proj=laea +lat_0=50.5 +lon_0=-118.5 +datum=WGS84 +units=m')
        for style, crs, provider, host in (
            ('terrain', 'EPSG:4326', cx.providers.Esri.WorldTopoMap, 'server.arcgisonline.com'),
            ('light', local, cx.providers.Esri.WorldGrayCanvas, 'server.arcgisonline.com'),
        ):
            with self.subTest(style=style):
                axes = Figure().subplots()
                bounds = Transformer.from_crs(4326, crs, always_xy=True).transform_bounds(-120, 49, -117, 52)
                axes.set_xlim(bounds[0], bounds[2])
                axes.set_ylim(bounds[1], bounds[3])
                with patch('requests.get', return_value=response) as request:
                    attribution = self.api.add_basemap(axes, crs, style, self.cache)
                self.assertEqual(attribution, provider.attribution)
                self.assertTrue(request.called)
                for call in request.call_args_list:
                    self.assertIn(host, call.args[0])
                    self.assertEqual(call.kwargs['timeout'], 30)
                    headers = {key.lower(): value for key, value in call.kwargs['headers'].items()}
                    self.assertTrue(headers['user-agent'].startswith('hydro-map/'))
                self.assertEqual(tuple(axes.get_xlim()), (bounds[0], bounds[2]))
                self.assertEqual(tuple(axes.get_ylim()), (bounds[1], bounds[3]))
                self.assertEqual(len(axes.images), 1)
                image = axes.images[0]
                west, east, south, north = image.get_extent()
                self.assertLess(west, (bounds[0] + bounds[2]) / 2)
                self.assertGreater(east, (bounds[0] + bounds[2]) / 2)
                self.assertLess(south, (bounds[1] + bounds[3]) / 2)
                self.assertGreater(north, (bounds[1] + bounds[3]) / 2)
                self.assertEqual(image.get_zorder(), 0)
                self.assertEqual(image.get_alpha(), .85)
                self.assertFalse(axes.texts)
                with patch('requests.get', side_effect=AssertionError('cached tiles must not redownload')):
                    self.api.add_basemap(axes, crs, style, self.cache)

    @unittest.skipUnless(importlib.util.find_spec('contextily'), 'requires maps extra')
    def test_network_failure_is_actionable(self):
        from requests import Timeout

        axes = Figure().subplots()
        axes.set_xlim(-120, -117)
        axes.set_ylim(49, 52)
        with patch('requests.get', side_effect=Timeout('server did not respond')):
            with self.assertRaisesRegex(OSError, r'Retry|retry') as error:
                self.api.add_basemap(axes, 'EPSG:4326', 'terrain', self.cache)
        self.assertIn('--basemap none', str(error.exception))
        self.assertFalse(axes.images)


if __name__ == '__main__':
    unittest.main()
