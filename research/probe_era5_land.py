"""Verify one UTC day of ERA5-Land from the public NCAR GDEX subset."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import netCDF4
import numpy as np
from pyproj import Transformer
import requests

from hydro_map.plotting import load_features
from probe_daymet_polygons import fractional_weights, strict_area_mean

BASE = 'https://tds.gdex.ucar.edu/thredds/ncss/grid/files/d633008/'
FIELDS = {'t2m': ('instan', 167, 'K'), 'tp': ('accumu', 228, 'm'),
          'sd': ('instan', 141, 'm of water equivalent')}
DAY = datetime(2024, 12, 31, tzinfo=timezone.utc)


def daily_fields(arrays, times):
    """Use the next 00Z cumulative precipitation; average 00–23Z states."""
    expected = np.array([(DAY + timedelta(hours=h)).timestamp() for h in range(25)])
    if not np.array_equal(times, expected):
        raise ValueError('Expected 25 consecutive hours including the next 00Z boundary')
    return {'temperature_c': np.mean(arrays['t2m'][:24], axis=0) - 273.15,
            'precipitation_mm': arrays['tp'][24] * 1000,
            'swe_mm': np.mean(arrays['sd'][:24], axis=0) * 1000}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result_path = args.output / 'summary.json'
    result_path.write_text(json.dumps({'status': 'running'}) + '\n')
    features = load_features(args.geojson)
    ids = [f['properties'].get('id') for f in features]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Catchments require unique nonempty identifiers')
    # Fixed research window: reject geometries outside the requested native cell edges.
    bounds = (-122.35, 40.95, -109.55, 53.05)
    for feature in features:
        w, s, e, n = feature['geometry'].bounds
        if not (bounds[0] <= w < e <= bounds[2] and bounds[1] <= s < n <= bounds[3]):
            raise ValueError('Catchment exceeds the fixed Columbia pilot subset')
    manifest_path = args.output / 'requests.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    arrays, transferred = {}, 0
    lat = lon = times = None
    for variable, (group, param, units) in FIELDS.items():
        name = f'{variable}_20241231.nc'
        path = args.output / name
        collection = f'e5land.oper.fc.sfc.{group}'
        file = f'{collection}.{variable}_{param}.2024122601-2025010100.nc'
        query = dict(var=variable, north=53, south=41, west=237.7, east=250.4,
                     horizStride=1, time_start='2024-12-31T00:00:00Z',
                     time_end='2025-01-01T00:00:00Z', timeStride=1, accept='netcdf')
        url = BASE + collection + '/202412/' + file + '?' + urlencode(query)
        cached = manifest.get(name, {})
        valid_cache = (path.exists() and cached.get('url') == url
                       and cached.get('sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
                       and cached.get('status') == 200)
        if not valid_cache:
            if args.cache_only:
                raise ValueError(f'Missing or invalid cached subset: {name}')
            with requests.get(url, timeout=45, stream=True) as response:
                response.raise_for_status()
                content = bytearray()
                for block in response.iter_content(65536):
                    content.extend(block)
                    transferred += len(block)
                    if len(content) > 8_000_000 or transferred > 24_000_000:
                        raise ValueError('Subset download cap exceeded')
            path.write_bytes(content)
            manifest[name] = dict(url=url, status=200, bytes=len(content),
                                  sha256=hashlib.sha256(content).hexdigest(),
                                  retrieved_utc=datetime.now(timezone.utc).isoformat())
            manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        with netCDF4.Dataset(path) as ds:
            field = ds[variable]
            if (field.units != units or field.GRIB_paramId != param
                    or field.GRIB_stepType != ('accum' if variable == 'tp' else 'instant')
                    or field.dimensions != ('valid_time', 'latitude', 'longitude')):
                raise ValueError(f'Unexpected field definition: {variable}')
            time = ds['valid_time']
            if time.units != 'seconds since 1970-01-01' or time.calendar != 'proleptic_gregorian':
                raise ValueError('Unexpected time encoding')
            current = (np.asarray(ds['latitude'][:]), np.asarray(ds['longitude'][:]) - 360,
                       np.asarray(time[:]))
            if lat is None:
                lat, lon, times = current
            elif not all(np.array_equal(a, b) for a, b in zip((lat, lon, times), current)):
                raise ValueError('Grid or timestamps differ between variables')
            arrays[variable] = np.ma.asarray(field[:], dtype=float).filled(np.nan)
    if not np.allclose(np.diff(lon), .1) or not np.allclose(np.diff(lat), -.1):
        raise ValueError('Expected the native regular 0.1-degree grid')
    daily = daily_fields(arrays, times)
    identity = Transformer.from_crs(4326, 4326, always_xy=True)
    rows = []
    for feature in features:
        weights, info = fractional_weights(feature['geometry'], lon, lat, identity)
        for variable, data in daily.items():
            value, fraction, complete = strict_area_mean(data, weights)
            rows.append(dict(project_id=feature['properties']['id'], variable=variable,
                             value=value if complete else None, valid_area_fraction=fraction,
                             qc='valid' if complete else 'spatial_missing', **info))
    result = dict(status='bounded_polygon_pilot_complete', date=DAY.date().isoformat(),
                  input_sha256=hashlib.sha256(args.geojson.read_bytes()).hexdigest(),
                  source='NCAR GDEX d633008', downloaded_new_bytes=transferred,
                  scope='One UTC day; three fields; no full-period coverage or skill claim.',
                  precipitation_method='Next 00Z endpoint of the daily forecast accumulation',
                  snow_definition='Water equivalent; not physical snow depth', rows=rows)
    result_path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(f'Checked {len(features)} complete polygons and three fields for one UTC day.')


if __name__ == '__main__':
    main()
