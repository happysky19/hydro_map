"""Grid cells of the forecast systems and catchment area weights on them.

A regular latitude-longitude grid (WeatherNext 2, GEFS, IFS from TIGGE) and a
reduced Gaussian grid (AIFS, N320) are both described cell by cell as
longitude-latitude boxes: on the reduced grid each row has its own number of
points, and rows meet halfway between their latitudes. A catchment's weight on
a cell is the geodesic area of their intersection, as in the history tables,
so a catchment mean is the same kind of number whichever grid it comes from.
"""

from catchment_history.area_weights import geodesic_area
import numpy as np
import shapely


def east(longitude):
    """Longitudes in -180..180 (the files use 0..360)."""
    return (np.asarray(longitude, dtype=float) + 180) % 360 - 180


def regular_cells(latitudes, longitudes):
    """Point latitude, longitude and cell bounds of a regular grid, in file order (rows of latitudes)."""
    latitudes, longitudes = np.asarray(latitudes, float), east(longitudes)
    dy, dx = abs(latitudes[1] - latitudes[0]), abs(np.diff(np.sort(longitudes))[0])
    lat, lon = (values.ravel() for values in np.meshgrid(latitudes, longitudes, indexing='ij'))
    south, north = np.maximum(lat - dy / 2, -90), np.minimum(lat + dy / 2, 90)
    return lat, lon, (lon - dx / 2, south, lon + dx / 2, north)


def reduced_gaussian_cells(row_latitudes, points_per_row):
    """The same for a reduced Gaussian grid: rows north to south, each from longitude 0 eastwards."""
    rows = np.asarray(row_latitudes, float)
    counts = np.asarray(points_per_row, int)
    edges = np.r_[90., (rows[:-1] + rows[1:]) / 2, -90.]
    lat = np.repeat(rows, counts)
    north, south = np.repeat(edges[:-1], counts), np.repeat(edges[1:], counts)
    spacing = np.repeat(360. / counts, counts)
    position = np.concatenate([np.arange(n) for n in counts])
    lon = east(position * spacing)
    return lat, lon, (lon - spacing / 2, south, lon + spacing / 2, north)


def weights(geometries, cells, margin=1.):
    """(points used, matrix of catchment-by-point areas in m2) for the cells near the catchments.

    Each row sums to the geodesic area of its catchment; a mismatch means the grid does
    not cover it and raises. The tolerance allows for cell edges along parallels, which
    the geodesic area treats as great circles (about 1e-5 for 0.25 degree cells); a
    missing cell is at least 1e-2 of the largest catchment.
    """
    _, _, (west, south, east_, north) = cells
    lon_min, lat_min, lon_max, lat_max = shapely.union_all(geometries).bounds
    near = np.flatnonzero((east_ >= lon_min - margin) & (west <= lon_max + margin)
                          & (north >= lat_min - margin) & (south <= lat_max + margin))
    boxes = shapely.box(west[near], south[near], east_[near], north[near])
    matrix = np.zeros((len(geometries), len(near)))
    for row, geometry in enumerate(geometries):
        shapely.prepare(geometry)
        full = shapely.covers(geometry, boxes)
        edge = shapely.intersects(geometry, boxes) & ~full
        for i in np.flatnonzero(full):
            matrix[row, i] = geodesic_area(boxes[i])
        for i in np.flatnonzero(edge):
            matrix[row, i] = geodesic_area(boxes[i].intersection(geometry))
        expected = geodesic_area(geometry)
        if not np.isclose(matrix[row].sum(), expected, rtol=1e-4):
            raise ValueError(f'Catchment {row}: cells cover {matrix[row].sum():.6g} m2 of {expected:.6g} m2')
    used = matrix.any(axis=0)
    return near[used], matrix[:, used]


def land_mean(matrix, values):
    """Area-weighted means of point VALUES over the points that have a value (land fields are missing over
    water); NaN for a catchment with none."""
    values = np.asarray(values, float)
    present = np.isfinite(values)
    area = matrix * present
    total = area.sum(axis=1)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(total > 0, np.where(present, values, 0.) @ area.T / total, np.nan)


def area_mean(matrix, values):
    """Area-weighted means of point VALUES (last axis); a catchment touching a missing value gets NaN."""
    values = np.asarray(values, float)
    filled = np.where(np.isfinite(values), values, 0.)
    means = filled @ matrix.T / matrix.sum(axis=1)
    touched = np.isnan(values).astype(float) @ (matrix > 0).T.astype(float)
    return np.where(touched > 0, np.nan, means)
