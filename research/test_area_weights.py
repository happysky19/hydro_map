"""Fractional polygon weights preserve area; missing cells are never renormalized away."""

import math
import unittest

import numpy as np
from pyproj import Transformer
from shapely.geometry import box

from area_weights import fractional_weights, strict_area_mean


class AreaWeightTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
