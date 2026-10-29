"""Read the public NOAA AORC v1.1 Zarr archive: metadata, axes, chunks and cell weights."""
import json
import subprocess

import numpy as np
import shapely
from pyproj import Transformer

BASE = 'https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/'
VARIABLES = ['APCP_surface', 'TMP_2maboveground', 'SPFH_2maboveground',
             'PRES_surface', 'DSWRF_surface', 'DLWRF_surface',
             'UGRD_10maboveground', 'VGRD_10maboveground']
UNITS = ['kg/m^2', 'K', 'kg/kg', 'Pa', 'W/m^2', 'W/m^2', 'm/s', 'm/s']


def check(condition, message):
    if not condition:
        raise ValueError(message)


def decode(payload, dtype, zstd, allow_prefix=False):
    result = subprocess.run([zstd, '-dc'], input=payload, capture_output=True, timeout=30)
    truncated = result.returncode == 1 and b'premature end' in result.stderr
    check(result.returncode == 0 or (allow_prefix and truncated),
          f'Unexpected Zstd decoder failure: {result.stderr.decode(errors="replace")}')
    return np.frombuffer(result.stdout, dtype=dtype), result.returncode


def metadata(store, year):
    payload, _ = store.read(BASE + f'{year}.zarr/.zmetadata')
    document = json.loads(payload)
    check(document.get('zarr_consolidated_format') == 1, 'Unsupported metadata format')
    m = document['metadata']
    check(m['time/.zattrs']['units'] == 'seconds since 1970-01-01' and
          m['time/.zattrs']['calendar'] == 'proleptic_gregorian', 'Unsupported time encoding')
    lengths = [m[axis + '/.zarray']['shape'][0] for axis in ('time', 'latitude', 'longitude')]
    for variable, units in zip(VARIABLES, UNITS):
        a, attrs = m[variable + '/.zarray'], m[variable + '/.zattrs']
        check(a['zarr_format'] == 2 and a['order'] == 'C' and a['dtype'] == '<i2' and
              a['compressor']['id'] == 'zstd' and a.get('filters') is None and
              a.get('dimension_separator', '.') == '.' and a['chunks'] == [144, 128, 256],
              f'Unsupported array encoding: {year}/{variable}')
        check(a['shape'] == lengths and attrs['_ARRAY_DIMENSIONS'] == ['time', 'latitude', 'longitude'],
              f'Unexpected array shape/order: {year}/{variable}')
        check(attrs['units'] == units and attrs['missing_value'] == -32767 and
              np.isfinite(attrs['scale_factor']) and attrs['scale_factor'] > 0 and
              np.isfinite(attrs.get('add_offset', 0)), f'Unsupported units/packing: {variable}')
    return m


def axis(store, year, name, m, zstd, first_chunk_only=False):
    a = m[name + '/.zarray']
    check(a['dtype'] == '<f8' and a['order'] == 'C' and a['compressor']['id'] == 'zstd' and
          m[name + '/.zattrs']['_ARRAY_DIMENSIONS'] == [name], f'Unsupported {name} axis')
    count = (a['shape'][0] + a['chunks'][0] - 1) // a['chunks'][0]
    pieces = []
    for k in range(1 if first_chunk_only else count):
        payload, _ = store.read(BASE + f'{year}.zarr/{name}/{k}')
        values, _ = decode(payload, '<f8', zstd)
        check(len(values) == a['chunks'][0], 'Unexpected decoded axis chunk length')
        pieces.append(values)
    return np.concatenate(pieces)[:a['shape'][0]]


def weights(features, lat, lon):
    check(np.allclose(np.diff(lat), np.diff(lat)[0], rtol=0, atol=1e-10) and
          np.allclose(np.diff(lon), np.diff(lon)[0], rtol=0, atol=1e-10), 'Grid must be regular')
    transform = Transformer.from_crs(4326, 6933, always_xy=True)
    dy, dx = lat[1] - lat[0], lon[1] - lon[0]
    x = transform.transform(np.r_[lon - dx/2, lon[-1] + dx/2], np.zeros(len(lon)+1))[0]
    y = transform.transform(np.zeros(len(lat)+1), np.r_[lat - dy/2, lat[-1] + dy/2])[1]
    project = lambda points: np.column_stack(transform.transform(points[:, 0], points[:, 1]))
    result = {}
    for identifier, geometry in features.items():
        polygon = shapely.transform(geometry, project)
        shapely.prepare(polygon)
        west, south, east, north = polygon.bounds
        xs = np.flatnonzero((x[:-1] < east) & (x[1:] > west))
        ys = np.flatnonzero((y[:-1] < north) & (y[1:] > south))
        yy, xx = np.meshgrid(ys, xs, indexing='ij')
        yy, xx = yy.ravel(), xx.ravel()
        cells = shapely.box(x[xx], y[yy], x[xx+1], y[yy+1])
        hit = shapely.intersects(polygon, cells)
        yy, xx, cells = yy[hit], xx[hit], cells[hit]
        full = shapely.covers(polygon, cells)
        area = (x[xx+1]-x[xx]) * (y[yy+1]-y[yy])
        area[~full] = shapely.area(shapely.intersection(polygon, cells[~full]))
        positive = area > 0
        yy, xx, area = yy[positive], xx[positive], area[positive]
        check(abs(area.sum()/polygon.area - 1) < 1e-8, f'Grid does not cover polygon: {identifier}')
        result[identifier] = (yy, xx, area)
    return result
