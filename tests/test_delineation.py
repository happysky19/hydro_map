import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
from pyproj import CRS
import rasterio
from rasterio.transform import from_origin
import shapefile
from shapely.geometry import shape

from hydro_map.basins import build_catchments
from hydro_map.config import Config, Dataset, Project


class DelineationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transform = from_origin(-120, 50, 1 / 240, 1 / 240)
        self.source = self.root / 'basins.shp'
        with shapefile.Writer(str(self.source)) as writer:
            for name in ('HYBAS_ID', 'NEXT_DOWN', 'ENDO'):
                writer.field(name, 'N', 12)
            writer.poly([[(-120, 49.95), (-120, 50), (-119.95, 50),
                          (-119.95, 49.95), (-120, 49.95)]])
            writer.record(1, 0, 0)
        self.source.with_suffix('.prj').write_text(CRS.from_epsg(4326).to_wkt())
        direction = np.full((13, 13), 255, dtype=np.uint8)
        direction[2:7, 5] = 4
        direction[7, 5] = 0
        direction[4, 2:5] = 1
        self.dir = self.root / 'dir.tif'
        self.aca = self.root / 'aca.tif'
        for path, array, nodata in ((self.dir, direction, 255),
                (self.aca, np.ones((13, 13), dtype=np.uint32), 4294967295)):
            with rasterio.open(path, 'w', driver='GTiff', width=13, height=13,
                    count=1, dtype=array.dtype, crs='EPSG:4326',
                    transform=self.transform, nodata=nodata) as raster:
                raster.write(array, 1)
        self.config = Config(Dataset('na', delineation='outlet_cell'),
                             (self.project('UPPER', 3), self.project('LOWER', 6)))

    def project(self, name, row, group=None):
        lon, lat = rasterio.transform.xy(self.transform, row, 5)
        return Project(name, name, 1, lon, lat, metadata=(
            {'outlet_group': group} if group else {}),
            grid_lon=lon, grid_lat=lat, grid_reference='Test river cell')

    def build(self, **kwargs):
        return build_catchments(self.config, self.source,
            flow_direction=self.dir, flow_accumulation=self.aca, **kwargs)

    def test_dams_in_same_unit_split_at_cells_and_keep_downstream_tributary(self):
        result = self.build()
        upper, lower = result['features']
        self.assertEqual(upper['properties']['cell_count_total'], 2)
        self.assertEqual(lower['properties']['cell_count_total'], 8)
        self.assertEqual(lower['properties']['cell_count_local'], 6)
        self.assertEqual(lower['properties']['up'], ['UPPER'])
        self.assertEqual(upper['properties']['down'], 'LOWER')
        a, b = shape(upper['geometry']), shape(lower['geometry'])
        self.assertEqual(a.intersection(b).area, 0)
        self.assertAlmostEqual(a.area, 2 / 240 ** 2)
        self.assertAlmostEqual(b.area, 6 / 240 ** 2)
        filtered = self.build(project_ids=['LOWER'])['features'][0]
        self.assertEqual(filtered['geometry'], lower['geometry'])
        total = self.build(part='total')['features'][1]
        self.assertAlmostEqual(shape(total['geometry']).area, 8 / 240 ** 2)

    def test_diversion_return_keeps_natural_network_and_requires_upstream_intake(self):
        upper, lower = self.config.projects
        metadata = dict(catchment_role='natural_reach_at_tailrace',
                        diversion_intake_project='UPPER', routing_requires_operations=True)
        self.config = replace(self.config, projects=(upper, replace(lower, metadata=metadata)))
        props = self.build()['features'][1]['properties']
        self.assertEqual(props['up'], ['UPPER'])
        self.assertEqual(props['cell_count_local'], 6)
        self.assertEqual(props['diversion_intake_project'], 'UPPER')
        self.assertTrue(props['routing_requires_operations'])
        self.config = replace(self.config, projects=(replace(upper, metadata=dict(
            metadata, diversion_intake_project='LOWER')), lower))
        with self.assertRaisesRegex(ValueError, 'upstream'):
            self.build()

    def test_shared_group_uses_downstream_member_and_subtracts_upper_once(self):
        self.config = replace(self.config, projects=(self.config.projects[0],
            self.project('LOWER', 6, 'PAIR'), self.project('LOWER_B', 7, 'PAIR')))
        result = self.build()
        a, b = result['features'][1:]
        self.assertEqual(a['geometry'], b['geometry'])
        self.assertEqual(a['properties']['cell_count_total'], 9)
        self.assertEqual(a['properties']['cell_count_local'], 7)
        self.assertEqual(a['properties']['forcing_outlet_project'], 'LOWER_B')
        self.assertEqual(a['properties']['geometry_status'], 'shared_unit_approximation')
        self.assertEqual(result['metadata']['forcing_group_count'], 2)

    def test_shared_group_rejects_sibling_tributaries_even_with_downstream_member(self):
        branch_lon, branch_lat = rasterio.transform.xy(self.transform, 4, 3)
        branch = replace(self.project('BRANCH', 4, 'PAIR'),
            lon=branch_lon, lat=branch_lat, grid_lon=branch_lon, grid_lat=branch_lat)
        self.config = replace(self.config, projects=(
            self.project('UPPER', 3, 'PAIR'), branch, self.project('LOWER', 6, 'PAIR')))
        with self.assertRaisesRegex(ValueError, 'one flow path'):
            self.build()

    def test_same_cell_needs_explicit_group_and_virtual_links_are_rejected(self):
        self.config = replace(self.config, projects=(self.project('A', 3), self.project('B', 3)))
        with self.assertRaisesRegex(ValueError, 'same.*cell'):
            self.build()
        self.config = replace(self.config, dataset=replace(
            self.config.dataset, include_virtual_connections=True))
        with self.assertRaisesRegex(ValueError, 'virtual'):
            self.build()

    def test_source_edge_is_rejected_even_when_search_envelope_extends_beyond_it(self):
        with rasterio.open(self.dir, 'r+') as raster:
            flow = raster.read(1)
            flow[:2, 5] = 4
            raster.write(flow, 1)
        with self.assertRaisesRegex(ValueError, 'edge'):
            self.build()

    def test_missing_flow_and_noncenter_grid_point_fail(self):
        with self.assertRaisesRegex(ValueError, 'flow'):
            build_catchments(self.config, self.source)
        upper = self.config.projects[0]
        self.config = replace(self.config, projects=(replace(upper,
            grid_lon=upper.grid_lon + 0.001), self.config.projects[1]))
        with self.assertRaisesRegex(ValueError, 'center'):
            self.build()


if __name__ == '__main__':
    unittest.main()
