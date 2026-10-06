import unittest

import numpy as np
from affine import Affine
from shapely.geometry import box

from hydro_map.flow import mask_geometry, upstream_mask


class FlowTests(unittest.TestCase):
    def test_dam_above_confluence_excludes_the_lower_tributary(self):
        direction = np.zeros((7, 7), dtype=np.uint8)
        direction[1:5, 2] = 4
        direction[1, 1] = 2
        direction[3, 3:5] = 16

        upper = upstream_mask(direction, (2, 2))
        lower = upstream_mask(direction, (4, 2))
        self.assertEqual(set(map(tuple, np.argwhere(upper))),
                         {(1, 1), (1, 2), (2, 2)})
        self.assertEqual(set(map(tuple, np.argwhere(lower))),
                         {(1, 1), (1, 2), (2, 2), (3, 2),
                          (3, 3), (3, 4), (4, 2)})
        self.assertTrue(lower[2, 2])
        self.assertFalse(upper[4, 2])
        self.assertFalse((upper & ~lower).any())
        self.assertEqual(int((lower & ~upper).sum()), 4)

    def test_all_eight_esri_directions_reach_a_terminal_outlet(self):
        direction = np.zeros((5, 5), dtype=np.uint8)
        direction[1:4, 1:4] = [[2, 4, 8], [1, 0, 16], [128, 64, 32]]
        expected = np.zeros((5, 5), dtype=bool)
        expected[1:4, 1:4] = True
        np.testing.assert_array_equal(upstream_mask(direction, (2, 2)), expected)

    def test_reached_grid_edge_is_rejected_as_a_truncated_crop(self):
        direction = np.zeros((5, 5), dtype=np.uint8)
        direction[:2, 2] = 4
        with self.assertRaisesRegex(ValueError, "edge|truncated"):
            upstream_mask(direction, (2, 2))

    def test_outlet_cycle_is_rejected(self):
        direction = np.zeros((5, 5), dtype=np.uint8)
        direction[2, 2], direction[2, 3] = 1, 16
        with self.assertRaisesRegex(ValueError, "cycle"):
            upstream_mask(direction, (2, 2))

    def test_invalid_grid_and_outlet_are_rejected(self):
        valid = np.zeros((5, 5), dtype=np.uint8)
        invalid_code = valid.copy()
        invalid_code[1, 1] = 3
        nodata_outlet = valid.copy()
        nodata_outlet[2, 2] = 255
        for direction, outlet in [
            (valid.ravel(), (2, 2)), (valid.astype(float), (2, 2)),
            (invalid_code, (2, 2)), (nodata_outlet, (2, 2)),
            (valid, (-1, 2)), (valid, (2, 5)), (valid, (2.5, 2)),
        ]:
            with self.subTest(outlet=outlet, shape=direction.shape):
                with self.assertRaises(ValueError):
                    upstream_mask(direction, outlet)

    def test_mask_polygon_preserves_diagonal_cells_and_holes(self):
        mask = np.zeros((5, 5), dtype=bool)
        mask[1:4, 1:4] = True
        mask[2, 2] = False
        mask[0, 0] = True
        transform = Affine(0.1, 0, -120, 0, -0.1, 50)
        geometry = mask_geometry(mask, transform)
        expected = box(-119.9, 49.6, -119.6, 49.9).difference(
            box(-119.8, 49.7, -119.7, 49.8)).union(
            box(-120, 49.9, -119.9, 50))
        self.assertTrue(geometry.is_valid)
        self.assertLess(geometry.symmetric_difference(expected).area, 1e-12)
        self.assertAlmostEqual(geometry.area, 0.09)

    def test_polygon_conversion_rejects_empty_and_non_geographic_grids(self):
        transform = Affine(0.1, 0, -120, 0, -0.1, 50)
        mask = np.ones((3, 3), dtype=bool)
        for invalid_mask, invalid_transform in [
            (np.zeros((3, 3), dtype=bool), transform),
            (mask, Affine(100, 0, 500000, 0, -100, 5000000)),
            (mask, Affine(0.1, 0.01, -120, 0, -0.1, 50)),
            (mask, Affine(0.1, 0, -120, 0, 0.1, 50)),
        ]:
            with self.subTest(transform=invalid_transform):
                with self.assertRaises(ValueError):
                    mask_geometry(invalid_mask, invalid_transform)


if __name__ == "__main__":
    unittest.main()
