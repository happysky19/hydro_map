#!/usr/bin/env python3
"""Offline AORC evidence audit: calendar, two spatial samples, and native comparison.

Requires numpy, shapely, pyproj, zstd; a Python with netCDF4 is selected with
--netcdf-python. No networking is implemented. Raw evidence must include the
request manifests and payloads recorded by the bounded discovery probes.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import shape

BASE = 'https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/'
NATIVE = 'https://hydrology.nws.noaa.gov/pub/AORC/V1.1/NWRFC_1km/'
VARIABLES = ['APCP_surface', 'TMP_2maboveground', 'SPFH_2maboveground',
             'PRES_surface', 'DSWRF_surface', 'DLWRF_surface',
             'UGRD_10maboveground', 'VGRD_10maboveground']
UNITS = ['kg/m^2', 'K', 'kg/kg', 'Pa', 'W/m^2', 'W/m^2', 'm/s', 'm/s']


def check(condition, message):
    if not condition:
        raise ValueError(message)


class Cache:
    """Resolve logged public requests; verify each payload against its recorded SHA."""
    def __init__(self, directories):
        self.entries = []
        self.used = {}
        for directory in directories:
            aliases = directory / 'request_file_aliases.json'
            aliases = json.loads(aliases.read_text()) if aliases.exists() else {}
            records = []
            log = directory / 'requests.jsonl'
            if log.exists():
                records.extend(json.loads(line) for line in log.read_text().splitlines())
            log = directory / 'sample_requests.json'
            if log.exists():
                records.extend(json.loads(log.read_text()))
            for row in records:
                if row.get('error') or row.get('status') not in (200, 206) or not row.get('sha256') or not row.get('url'):
                    continue
                name = row.get('name') or Path(row.get('path', '')).name
                name = aliases.get(name, name)
                path = directory / name
                if path.is_file():
                    self.entries.append((row, path))
        check(self.entries, 'No manifested payloads found in cache directories')

    def read(self, url, byte_range=None, prefix=False):
        found = []
        for row, path in self.entries:
            if row['url'] != url:
                continue
            interval = row.get('headers', {}).get('Range')
            if prefix:
                if interval is not None and not interval.startswith('bytes=0-'):
                    continue
            elif interval != byte_range:
                continue
            found.append((row, path))
        check(found, f'Missing manifested cache object: {url} {byte_range or ""}')
        row, path = max(found, key=lambda item: item[1].stat().st_size)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        check(digest == row['sha256'], f'Cached payload SHA-256 mismatch: {path.name}')
        self.used[str(path)] = {'url': url, 'range': row.get('headers', {}).get('Range'),
                                'bytes': len(payload), 'sha256': digest,
                                'source_file': path.name}
        return payload, path


def decode(payload, dtype, zstd, allow_prefix=False):
    result = subprocess.run([zstd, '-dc'], input=payload, capture_output=True, timeout=30)
    truncated = result.returncode == 1 and b'premature end' in result.stderr
    check(result.returncode == 0 or (allow_prefix and truncated),
          f'Unexpected Zstd decoder failure: {result.stderr.decode(errors="replace")}')
    return np.frombuffer(result.stdout, dtype=dtype), result.returncode


def metadata(cache, year):
    payload, _ = cache.read(BASE + f'{year}.zarr/.zmetadata')
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


def axis(cache, year, name, m, zstd, first_chunk_only=False):
    a = m[name + '/.zarray']
    check(a['dtype'] == '<f8' and a['order'] == 'C' and a['compressor']['id'] == 'zstd' and
          m[name + '/.zattrs']['_ARRAY_DIMENSIONS'] == [name], f'Unsupported {name} axis')
    count = (a['shape'][0] + a['chunks'][0] - 1) // a['chunks'][0]
    pieces = []
    for k in range(1 if first_chunk_only else count):
        payload, _ = cache.read(BASE + f'{year}.zarr/{name}/{k}')
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


def native_file(cache, month, python):
    """Replay only cached TAR headers and one member; validate its decoded time."""
    url = NATIVE + f'aorc-1km-nwrfc_{month}.tar'
    offset = 0
    for _ in range(16):
        header, _ = cache.read(url, f'bytes={offset}-{offset+511}')
        member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
        if member.isfile():
            break
        offset += 512 + ((member.size + 511)//512)*512
    else:
        raise ValueError('No regular native TAR member among inspected headers')
    payload, path = cache.read(url, f'bytes={offset+512}-{offset+511+member.size}')
    check(len(payload) == member.size, 'Native member length differs from TAR header')
    program = ('import sys,json,numpy as np;from netCDF4 import Dataset;'
               'd=Dataset(sys.argv[1]);d.set_auto_maskandscale(False);'
               'a={k:v[:] for k,v in d.variables.items()};'
               'm={k:{n:getattr(v,n) for n in v.ncattrs()} for k,v in d.variables.items()};'
               'a["_metadata_json"]=np.array(json.dumps(m,default=lambda x:x.item()));'
               'np.savez_compressed(sys.stdout.buffer,**a);d.close()')
    result = subprocess.run([python, '-c', program, str(path)], capture_output=True, timeout=45)
    check(result.returncode == 0, 'Native decode failed; select --netcdf-python with netCDF4: ' +
          result.stderr.decode(errors='replace'))
    with np.load(io.BytesIO(result.stdout)) as archive:
        data = {k: archive[k] for k in archive.files}
    attributes = json.loads(str(data.pop('_metadata_json')))
    check(attributes['time']['units'] == 'seconds since 1970-01-01 00:00:00.0 0:00',
          'Unexpected native time units/timezone')
    data['_attributes'] = attributes
    return {'url': url, 'member': member.name, 'range': [offset+512, offset+511+member.size],
            'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}, data


def run(args, cache):
    zstd = shutil.which('zstd')
    check(zstd is not None, 'zstd CLI not found')
    document = json.loads(args.geojson.read_text())
    check(document.get('type') == 'FeatureCollection' and not document.get('crs'), 'Use WGS84 GeoJSON')
    features = {}
    for feature in document['features']:
        identifier = feature['properties']['id']
        geometry = shape(feature['geometry'])
        check(identifier not in features and geometry.is_valid and not geometry.is_empty and
              geometry.geom_type in ('Polygon', 'MultiPolygon'), 'Invalid or duplicate polygon')
        features[identifier] = geometry
    check(len(features) == 26, 'This evidence audit expects the 26 study catchments')
    report = {'input_sha256': hashlib.sha256(args.geojson.read_bytes()).hexdigest(),
              'calendar': [], 'sampled_hours': {}, 'native_samples': [], 'limitations': [
        'Only sampled-hour values were checked; full-period spatial/physical quality is unverified.',
        'Zstd prefixes verify complete decoded initial blocks, not complete frames or later times.',
        'Passing samples do not establish a global resolution of the NOAA masking notice.',
        'Areas use EPSG:6933 cell intersections; hydrologic skill has not been tested.']}
    metas = {}
    for year in range(1995, 2026):
        m = metadata(cache, year); metas[year] = m
        times = axis(cache, year, 'time', m, zstd, first_chunk_only=year == 2025)
        origin = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        expected_hours = int((dt.datetime(year+1, 1, 1)-dt.datetime(year, 1, 1)).total_seconds()/3600)
        check(np.array_equal(times, origin + np.arange(len(times))*3600), f'Noncontiguous time axis: {year}')
        check(m['time/.zarray']['shape'] == [expected_hours], 'Unexpected calendar-year length')
        check(year == 2025 or len(times) == expected_hours, 'Incomplete requested calendar audit')
        report['calendar'].append({'year': year, 'hours_checked': len(times),
                                  'complete_year_checked': year < 2025, 'all8_shapes_valid': True})
    coords = {year: {name: axis(cache, year, name, metas[year], zstd)
                    for name in ('latitude', 'longitude')} for year in (1995, 2025)}
    for name in ('latitude', 'longitude'):
        check(np.allclose(coords[1995][name], coords[2025][name], rtol=0, atol=1e-10), 'Sample grids differ')
    lat, lon = coords[2025]['latitude'], coords[2025]['longitude']
    polygon_weights = weights(features, lat, lon)
    blocks = set()
    for yy, xx, _ in polygon_weights.values():
        blocks.update(zip((yy//128).tolist(), (xx//256).tolist()))
    native = {}
    for month in ('199601', '202501'):
        provenance, data = native_file(cache, month, args.netcdf_python)
        provenance['timestamp'] = dt.datetime.fromtimestamp(float(data['time'][0]), dt.timezone.utc).isoformat()
        year = int(month[:4])
        check(data['time'].shape == (1,), 'Expected one native timestamp')
        if month == '202501':
            check(float(data['time'][0]) == dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp(),
                  'Native comparison requires 2025-01-01 00Z')
        for variable in VARIABLES[:4]:
            n = data['_attributes'][variable]; z = metas[year][variable+'/.zattrs']
            check(n['units'] == z['units'] and n['_FillValue'] == n['missing_value'] == z['missing_value'] and
                  float(n['scale_factor']) == float(z['scale_factor']) and
                  float(n.get('add_offset', 0)) == float(z.get('add_offset', 0)),
                  f'Native/Zarr units, fill, scale or offset differ: {variable}')
        provenance['native_attributes'] = data['_attributes']
        provenance['packing_units_match_zarr'] = True
        y0 = int(np.abs(lat-data['latitude'][0]).argmin()); x0 = int(np.abs(lon-data['longitude'][0]).argmin())
        check(np.allclose(lat[y0:y0+len(data['latitude'])], data['latitude'], rtol=0, atol=1e-10) and
              np.allclose(lon[x0:x0+len(data['longitude'])], data['longitude'], rtol=0, atol=1e-10),
              'Native and Zarr coordinate arrays do not align')
        provenance['coverage'] = {}
        for identifier, (yy, xx, area) in polygon_weights.items():
            check(yy.min() >= y0 and xx.min() >= x0 and yy.max() < y0+len(data['latitude']) and
                  xx.max() < x0+len(data['longitude']), 'Polygon outside native subset')
            provenance['coverage'][identifier] = {v: float(area[data[v][0, yy-y0, xx-x0] != -32767].sum()/area.sum())
                                                  for v in VARIABLES[:4]}
            check(all(np.all(data[v][0, yy-y0, xx-x0] != -32767) for v in VARIABLES[:4]),
                  f'Missing native sampled cells: {month}/{identifier}')
        report['native_samples'].append(provenance)
        native[month] = (data, y0, x0)
    for year in (1995, 2025):
        hour = {'blocks': len(blocks), 'projects': {}, 'ranges': {}, 'native_comparison_performed': year == 2025}
        decoded = {}
        prefix_comparisons = 0
        for variable in VARIABLES:
            for cy, cx in sorted(blocks):
                url = BASE + f'{year}.zarr/{variable}/0.{cy}.{cx}'
                payload, _ = cache.read(url, prefix=True)
                values, code = decode(payload, '<i2', zstd, allow_prefix=True)
                check(len(values) >= 128*256, 'Prefix does not contain a complete first hour')
                if code == 0 and len(payload) > 131072:
                    prefix, _ = decode(payload[:131072], '<i2', zstd, allow_prefix=True)
                    check(len(prefix) >= 128*256 and np.array_equal(prefix[:128*256], values[:128*256]),
                          'Prefix decoding differs from full-frame first hour')
                    prefix_comparisons += 1
                decoded[variable, cy, cx] = values[:128*256].reshape(128, 256).copy()
        for identifier, (yy, xx, area) in polygon_weights.items():
            fields = {}
            for variable in VARIABLES:
                raw = np.empty(len(yy), dtype=np.int16)
                for cy, cx in set(zip((yy//128).tolist(), (xx//256).tolist())):
                    selected = (yy//128 == cy) & (xx//256 == cx)
                    raw[selected] = decoded[variable, cy, cx][yy[selected]%128, xx[selected]%256]
                attrs = metas[year][variable+'/.zattrs']; valid = raw != attrs['missing_value']
                field = {'valid_area_fraction': float(area[valid].sum()/area.sum()), 'missing_cells': int((~valid).sum())}
                check(valid.all(), f'Missing sampled cells: {year}/{identifier}/{variable}')
                values = raw.astype(float)*attrs['scale_factor'] + attrs.get('add_offset', 0)
                check(np.isfinite(values).all(), 'Nonfinite decoded sample')
                bounds = hour['ranges'].setdefault(variable, {'units': attrs['units'], 'min': float('inf'), 'max': float('-inf')})
                bounds['min'] = min(bounds['min'], float(values.min())); bounds['max'] = max(bounds['max'], float(values.max()))
                if year == 2025 and variable in VARIABLES[:4]:
                    data, y0, x0 = native['202501']; original = data[variable][0, yy-y0, xx-x0]
                    field['native_raw_mismatches'] = int((raw != original).sum())
                    check(np.array_equal(raw, original), 'Native and Zarr raw data disagree')
                fields[variable] = field
            hour['projects'][identifier] = fields
        hour['full_frame_prefix_crosschecks'] = prefix_comparisons
        hour['basic_physical_checks'] = {
            'precipitation_nonnegative': hour['ranges']['APCP_surface']['min'] >= 0,
            'specific_humidity_0_to_1': 0 <= hour['ranges']['SPFH_2maboveground']['min'] <= hour['ranges']['SPFH_2maboveground']['max'] <= 1,
            'pressure_positive': hour['ranges']['PRES_surface']['min'] > 0}
        check(all(hour['basic_physical_checks'].values()), 'Basic physical sample check failed')
        report['sampled_hours'][f'{year}-01-01T00:00:00Z'] = hour
        print(f'{year}: all 26 polygons and 8 fields passed sampled-hour checks.', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--cache-dir', required=True, action='append', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cache-only', required=True, action='store_true')
    parser.add_argument('--netcdf-python', default=sys.executable)
    args = parser.parse_args()
    check(args.output.resolve() not in {p.resolve() for p in args.cache_dir}, 'Use a separate report output directory')
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output/'audit_results.json'; report_path.unlink(missing_ok=True)
    status = {'status': 'running', 'network_bytes': 0}
    status_path = args.output/'audit_status.json'; status_path.write_text(json.dumps(status))
    cache = None
    try:
        cache = Cache(args.cache_dir)
        report = run(args, cache)
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
        status['status'] = 'passed'
        return 0
    except Exception as error:
        report_path.unlink(missing_ok=True)
        status.update(status='failed', error=str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        (args.output/'audit_manifest.json').write_text(json.dumps(list(cache.used.values()) if cache else [], indent=2)+'\n')
        status_path.write_text(json.dumps(status, indent=2)+'\n')


if __name__ == '__main__':
    raise SystemExit(main())
