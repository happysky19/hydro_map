"""Delineate catchments by upstream connectivity on a WGS84 D8 grid."""

import numpy as np
from rasterio.features import shapes
import shapely
from shapely.geometry import shape


# ESRI/HydroSHEDS directions, expressed as (row change, column change).
D8 = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
      16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}


def upstream_mask(direction, outlet_rowcol):
    """Return all cells reaching the outlet, including the outlet cell.

    Zero marks a terminal cell and 255 marks nodata. An upstream cell on
    any array edge is rejected: callers must supply a crop with a complete
    drainage basin and a surrounding halo. Cycles involving the outlet are
    rejected; disconnected cycles cannot contribute to this catchment.
    """
    direction = np.asarray(direction)
    if (direction.ndim != 2 or not direction.size
            or not np.issubdtype(direction.dtype, np.integer)
            or not np.isin(direction, [0, 255, *D8]).all()):
        raise ValueError("Direction must be a nonempty 2D integer ESRI D8 grid")
    try:
        row, col = outlet_rowcol
    except (TypeError, ValueError) as error:
        raise ValueError("Outlet must contain a row and column") from error
    if any(not isinstance(value, (int, np.integer))
           or isinstance(value, (bool, np.bool_)) for value in (row, col)):
        raise ValueError("Outlet row and column must be integers")
    height, width = direction.shape
    if not (0 <= row < height and 0 <= col < width):
        raise ValueError("Outlet lies outside the direction grid")
    if direction[row, col] == 255:
        raise ValueError("Outlet lies on a nodata cell")

    mask = np.zeros(direction.shape, dtype=bool)
    mask[row, col] = True
    rows, cols = np.array([row]), np.array([col])
    while rows.size:
        if ((rows == 0) | (rows == height - 1)
                | (cols == 0) | (cols == width - 1)).any():
            raise ValueError("Upstream basin reaches the grid edge; crop may be truncated")
        next_rows, next_cols = [], []
        for code, (dr, dc) in D8.items():
            rr, cc = rows - dr, cols - dc
            take = (direction[rr, cc] == code) & ~mask[rr, cc]
            if take.any():
                rr, cc = rr[take], cc[take]
                mask[rr, cc] = True
                next_rows.append(rr)
                next_cols.append(cc)
        if not next_rows:
            break
        rows, cols = np.concatenate(next_rows), np.concatenate(next_cols)

    step = D8.get(int(direction[row, col]))
    if step is not None and mask[row + step[0], col + step[1]]:
        raise ValueError("D8 cycle includes the outlet cell")
    return mask


def mask_geometry(mask, transform):
    """Polygonize a nonempty mask using its north-up WGS84 grid transform.

    The reader must verify the raster CRS is WGS84 before calling this helper.
    Four-neighbor polygonization keeps diagonal cell contacts valid without
    filling holes or smoothing the native cell boundaries.
    """
    mask = np.asarray(mask)
    if mask.ndim != 2 or not mask.size or mask.dtype != bool or not mask.any():
        raise ValueError("Catchment mask must be a nonempty 2D boolean mask")
    if (not np.isfinite(tuple(transform)).all() or transform.a <= 0
            or transform.e >= 0 or transform.b != 0 or transform.d != 0):
        raise ValueError("Transform must describe a finite north-up WGS84 grid")
    west, north = transform.c, transform.f
    east = west + mask.shape[1] * transform.a
    south = north + mask.shape[0] * transform.e
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Grid bounds must be WGS84 longitude and latitude")
    geometry = shapely.union_all([
        shape(polygon) for polygon, value in shapes(
            mask.astype(np.uint8), mask=mask, transform=transform, connectivity=4)
        if value
    ])
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if geometry.geom_type == "GeometryCollection":
        geometry = shapely.union_all([
            part for part in geometry.geoms
            if part.geom_type in {"Polygon", "MultiPolygon"}
        ])
    if (geometry.is_empty or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}):
        raise ValueError("Catchment mask cannot be represented by valid polygons")
    return shapely.orient_polygons(geometry)
