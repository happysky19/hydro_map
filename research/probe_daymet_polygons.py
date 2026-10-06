"""Bounded native Daymet polygon pilot; requires numpy, netCDF4, pyproj, shapely and curl."""
import argparse
from datetime import date, datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import netCDF4
import numpy as np
from pyproj import CRS, Geod, Transformer
import shapely
from shapely.geometry import box, shape
from shapely.ops import transform

from daymet_calendar import align_daymet_series, validate_window

BASE = 'https://opendap.earthdata.nasa.gov/collections/C2532426483-ORNL_CLOUD/granules/Daymet_Daily_V4R1.daymet_v4_daily_na_{variable}_{year}.nc'
VARIABLES = ('prcp', 'tmin', 'tmax')
WINDOWS = ((1996, 58, 60), (1996, 364, 364), (1997, 0, 0))
GEOD = Geod(ellps='WGS84')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def geodesic_area(geometry):
    if geometry.is_empty:
        return 0.0
    if geometry.geom_type == 'GeometryCollection':
        return sum(geodesic_area(g) for g in geometry.geoms)
    if geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
        return 0.0
    return abs(GEOD.geometry_area_perimeter(shapely.orient_polygons(geometry))[0])


def index_bounds(geometry, x, y):
    west, south, east, north = geometry.bounds
    dx, dy = abs(float(x[1] - x[0])), abs(float(y[1] - y[0]))
    i0 = max(0, math.floor((west - x[0] - dx / 2) / dx))
    i1 = min(len(x) - 1, math.ceil((east - x[0] + dx / 2) / dx))
    j0 = max(0, math.floor((y[0] - north - dy / 2) / dy))
    j1 = min(len(y) - 1, math.ceil((y[0] - south + dy / 2) / dy))
    return i0, i1, j0, j1


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--projects', nargs='+', default=['OXBOW', 'MICA'])
    parser.add_argument('--max-download-mb', type=float, default=80)
    parser.add_argument('--cache-only', action='store_true', help='Require URL- and hash-verified cached responses; never contact NASA')
    args = parser.parse_args()
    if not 0 < args.max_download_mb <= 100:
        parser.error('Download cap must be positive and at most 100 MB')
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps({'status':'running', 'started_utc':datetime.now(timezone.utc).isoformat(), 'cache_only':args.cache_only}, indent=2) + '\n')
    for name in ['polygon_daily_values.csv', 'year_boundary_aligned.csv', 'polygon_weights.json', 'all_project_grid_bounds.json']:
        (args.output/name).unlink(missing_ok=True)
    manifest_path = args.output / 'requests.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    transferred = 0
    doc = json.loads(args.geojson.read_text())
    if doc.get('crs'):
        raise ValueError('GeoJSON must use WGS84 without a legacy CRS member')
    if doc.get('type') != 'FeatureCollection' or not doc.get('features'):
        raise ValueError('Input must be a nonempty GeoJSON FeatureCollection')
    features = doc['features']
    for feature in features:
        identifier = feature.get('properties', {}).get('id')
        if not isinstance(identifier, str) or not identifier or not all(c.isalnum() or c in '_-' for c in identifier):
            raise ValueError('Project identifiers must be nonempty strings containing letters, numbers, underscores or hyphens')
        geographic = shape(feature['geometry'])
        if geographic.geom_type not in {'Polygon', 'MultiPolygon'} or geographic.is_empty or not geographic.is_valid:
            raise ValueError(f'{identifier}: expected a valid nonempty Polygon or MultiPolygon')
        west, south, east, north = geographic.bounds
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError(f'{identifier}: coordinates outside WGS84 bounds')
    by_id = {f['properties']['id']: f for f in features}
    if len(by_id) != len(features) or set(args.projects) - by_id.keys():
        raise ValueError('Catchment identifiers must be unique and contain the selected projects')
    with tempfile.TemporaryDirectory(prefix='daymet-auth-') as private:
        cookie = Path(private) / 'cookies'
        cookie.touch(mode=0o600)

        def fetch(name, url, cap=6_000_000):
            nonlocal transferred
            path = args.output / name
            cached = manifest.get(name, {})
            if path.exists() and cached.get('url') == url and cached.get('http_status') == '200' and cached.get('curl_exit_code') == 0 and cached.get('sha256') == sha256(path):
                return path
            if args.cache_only:
                raise RuntimeError(f'{name}: no matching successful URL- and hash-verified cached response')
            remaining = int(args.max_download_mb * 1_000_000) - transferred
            if remaining <= 0:
                raise RuntimeError('Total download cap reached')
            path.unlink(missing_ok=True)
            result = subprocess.run(['curl', '--silent', '--show-error', '--netrc', '--location', '--proto', '=https',
                '--proto-redir', '=https', '--cookie', str(cookie), '--cookie-jar', str(cookie), '--connect-timeout', '8',
                '--max-time', '60', '--max-filesize', str(min(cap, remaining)), '--output', str(path),
                '--write-out', '%{http_code}', url], capture_output=True, text=True)
            size = path.stat().st_size if path.exists() else 0
            transferred += size
            record = {'url': url, 'checked_utc': datetime.now(timezone.utc).isoformat(), 'http_status': result.stdout,
                      'curl_exit_code': result.returncode, 'bytes': size, 'sha256': sha256(path) if path.exists() else None}
            manifest[name] = record
            manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
            if result.returncode or result.stdout != '200':
                raise RuntimeError(f'{name}: HTTP {result.stdout}, curl exit {result.returncode}; no credentials or headers logged')
            return path

        metadata = fetch('grid_2024.dmr', BASE.format(variable='prcp', year=2024) + '.dmr', 100_000)
        ns = {'d': 'http://xml.opendap.org/ns/DAP/4.0#'}
        root = ET.fromstring(metadata.read_bytes())
        dimensions = {e.attrib['name']: int(e.attrib['size']) for e in root.findall('d:Dimension', ns)}
        constraint = f'/x[0:1:{dimensions["x"]-1}];/y[0:1:{dimensions["y"]-1}];/time[0:1:364];/yearday[0:1:364];/lambert_conformal_conic'
        axes = fetch('grid_axes_2024.nc', BASE.format(variable='prcp', year=2024) + '.dap.nc4?' + urlencode({'dap4.ce': constraint}), 500_000)
        with netCDF4.Dataset(axes) as ds:
            x, y = np.asarray(ds['x'][:]), np.asarray(ds['y'][:])
            if not np.all(np.diff(x) == 1000) or not np.all(np.diff(y) == -1000):
                raise ValueError('Expected a regular 1-km native Daymet grid')
            projection = {k: ds['lambert_conformal_conic'].getncattr(k) for k in ds['lambert_conformal_conic'].ncattrs()}
            crs = CRS.from_cf(projection)
            t = ds['time']; decoded = netCDF4.num2date(t[:], t.units, calendar=t.calendar)
            if t.calendar != 'standard' or decoded[-1].strftime('%Y-%m-%d') != '2024-12-30':
                raise ValueError('Unexpected Daymet calendar')
        forward, inverse = Transformer.from_crs(4326, crs, always_xy=True), Transformer.from_crs(crs, 4326, always_xy=True)
        grid_rectangle = box(x[0]-500, y[-1]-500, x[-1]+500, y[0]+500)
        bounds_check, geometries = [], {}
        for identifier, feature in by_id.items():
            geographic = shape(feature['geometry'])
            if not geographic.is_valid or geographic.is_empty:
                raise ValueError(f'{identifier}: invalid or empty geometry')
            native = transform(forward.transform, geographic)
            geometries[identifier] = native
            bounds_check.append({'project_id': identifier, 'fully_inside_grid_rectangle': bool(grid_rectangle.covers(native)),
                                 'native_bounds_m': list(native.bounds), 'scope': 'coordinate extent only; not data mask or temporal coverage'})
        (args.output/'all_project_grid_bounds.json').write_text(json.dumps(bounds_check, indent=2) + '\n')
        summary, values = [], []
        for identifier in args.projects:
            feature, native = by_id[identifier], geometries[identifier]
            if feature['properties'].get('part') != 'local':
                raise ValueError('Pilot requires explicit local catchments')
            i0, i1, j0, j1 = index_bounds(native, x, y)
            xs, ys = x[i0:i1+1], y[j0:j1+1]
            weights, info = fractional_weights(native, xs, ys, inverse)
            np.savez_compressed(args.output/f'{identifier}_weights.npz', area_m2=weights, x=xs, y=ys)
            info.update(project_id=identifier, part='local', source_polygon_geodesic_area_m2=geodesic_area(shape(feature['geometry'])),
                        indices={'x_start':i0,'x_end':i1,'y_start':j0,'y_end':j1}, bbox_cells=int(weights.size),
                        geometry_sha256=hashlib.sha256(native.normalize().wkb).hexdigest())
            summary.append(info)
            for year, first, last in WINDOWS:
                times = f'{first}:1:{last}'
                for variable in VARIABLES:
                    name = f'{identifier}_{variable}_{year}_{first}_{last}.nc'
                    constraint = f'/x[{i0}:1:{i1}];/y[{j0}:1:{j1}];/time[{times}];/yearday[{times}];/time_bnds[{times}][0:1:1];/lambert_conformal_conic;/{variable}[{times}][{j0}:1:{j1}][{i0}:1:{i1}]'
                    path = fetch(name, BASE.format(variable=variable, year=year) + '.dap.nc4?' + urlencode({'dap4.ce': constraint}))
                    with netCDF4.Dataset(path) as ds:
                        if not np.array_equal(ds['x'][:], xs) or not np.array_equal(ds['y'][:], ys):
                            raise ValueError(f'{name}: grid coordinates changed')
                        source_crs = CRS.from_cf({k: ds['lambert_conformal_conic'].getncattr(k) for k in ds['lambert_conformal_conic'].ncattrs()})
                        if not crs.equals(source_crs):
                            raise ValueError(f'{name}: CRS changed')
                        time = ds['time']; dates = netCDF4.num2date(time[:], time.units, calendar=time.calendar)
                        data = ds[variable][:]; units = ds[variable].units
                        days = [date(stamp.year, stamp.month, stamp.day) for stamp in dates]
                        validate_window(year, first, last, days, ds['yearday'][:])
                        if data.shape != (last - first + 1, len(ys), len(xs)):
                            raise ValueError(f'{name}: data shape does not match the requested window')
                        if (variable == 'prcp' and units != 'mm/day') or (variable != 'prcp' and units != 'degrees C'):
                            raise ValueError(f'{name}: unexpected units {units}')
                        for k, day in enumerate(days):
                            value, valid_area, complete = strict_area_mean(data[k], weights)
                            values.append({'project_id': identifier, 'date': day.isoformat(), 'variable': variable,
                                           'value': value, 'units': units, 'valid_area_fraction': valid_area,
                                           'qc': 'valid' if complete else 'spatial_missing', 'source_file': name})
                    print(name, 'decoded', len(dates), 'days', flush=True)
        (args.output/'polygon_weights.json').write_text(json.dumps(summary, indent=2) + '\n')
        with (args.output/'polygon_daily_values.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(values[0])); writer.writeheader(); writer.writerows(values)
        aligned = []
        for identifier in args.projects:
            for variable in VARIABLES:
                selected = [v for v in values if v['project_id'] == identifier and v['variable'] == variable and v['date'] >= '1996-12-30']
                rows = align_daymet_series([date.fromisoformat(v['date']) for v in selected], [v['value'] for v in selected], date(1996,12,30), date(1997,1,1))
                for row in rows:
                    aligned.append({'project_id':identifier,'variable':variable,**row})
        with (args.output/'year_boundary_aligned.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(aligned[0])); writer.writeheader(); writer.writerows(aligned)
        final = {'status':'bounded_polygon_pilot_complete','geojson_sha256':sha256(args.geojson),'projects':args.projects,
                 'variables':list(VARIABLES),'downloaded_new_bytes':transferred,
                 'response_bytes_on_disk':sum(v['bytes'] for v in manifest.values()),'cache_only':args.cache_only,'valid_rows':sum(v['qc']=='valid' for v in values),
                 'total_rows':len(values),'all_project_grid_bounds_only':bounds_check,'source_crs_wkt':crs.to_wkt(),
                 'scope':'Selected full local polygons and five native dates only; no full-period/all-project mask validation.',
                 'day_boundary':'Native local days; no UTC conversion or imputation.',
                 'area_method':'Native grid-cell intersections with WGS84 geodesic areas; all positive-area intersecting cells required.'}
        (args.output/'summary.json').write_text(json.dumps(final, indent=2) + '\n')
    print('Saved polygon pilot; temporary authentication cookies removed.', flush=True)


if __name__ == '__main__':
    main()
