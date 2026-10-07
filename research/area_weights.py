"""Area weights of catchment polygons on regular native grids, and strict area means."""
import math

import numpy as np
from pyproj import Geod
import shapely
from shapely.ops import transform

GEOD = Geod(ellps='WGS84')


def geodesic_area(geometry):
    if geometry.is_empty:
        return 0.0
    if geometry.geom_type == 'GeometryCollection':
        return sum(geodesic_area(g) for g in geometry.geoms)
    if geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
        return 0.0
    return abs(GEOD.geometry_area_perimeter(shapely.orient_polygons(geometry))[0])


def fractional_weights(geometry, xs, ys, inverse):
    """Native-grid intersections weighted by their WGS84 geodesic area."""
    dx, dy = abs(xs[1] - xs[0]), abs(ys[1] - ys[0])
    xx, yy = np.meshgrid(xs, ys)
    cells = shapely.box(xx - dx / 2, yy - dy / 2, xx + dx / 2, yy + dy / 2)
    shapely.prepare(geometry)
    full = shapely.covers(geometry, cells)
    edge = shapely.intersects(geometry, cells) & ~full
    weights = np.zeros(xx.shape, dtype=float)
    xe = np.r_[xs - dx / 2, xs[-1] + dx / 2]
    ye = np.r_[ys + dy / 2, ys[-1] - dy / 2]
    corner_lon, corner_lat = inverse.transform(*np.meshgrid(xe, ye))
    for j, i in np.argwhere(full):
        jj, ii = [j, j+1, j+1, j], [i, i, i+1, i+1]
        weights[j, i] = abs(GEOD.polygon_area_perimeter(corner_lon[jj, ii], corner_lat[jj, ii])[0])
    fractional_count = 0
    for j, i in np.argwhere(edge):
        clipped = cells[j, i].intersection(geometry)
        if clipped.area > 0:
            weights[j, i] = geodesic_area(transform(inverse.transform, clipped))
            fractional_count += 1
    expected = geodesic_area(transform(inverse.transform, geometry))
    if not np.isclose(weights.sum(), expected, rtol=2e-6):
        raise ValueError('Fractional weights do not cover the full catchment')
    return weights, {'weighted_area_m2': float(weights.sum()), 'native_geometry_geodesic_area_m2': expected,
                     'intersecting_cells': int((weights > 0).sum()), 'fractional_edge_cells': fractional_count}


def strict_area_mean(data, weights):
    data = np.ma.asarray(data, dtype=float)
    valid = ~np.ma.getmaskarray(data) & np.isfinite(data.filled(np.nan))
    used = weights > 0
    valid_area = float(weights[valid].sum() / weights.sum())
    complete = bool(valid[used].all())
    value = float(np.sum(data.filled(0)[used] * weights[used]) / weights.sum()) if complete else math.nan
    return value, valid_area, complete
