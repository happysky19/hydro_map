"""Download verified CDS batches and export strict UTC daily catchment values.

Credentials are loaded by cdsapi.Client() from its normal local configuration.
Neither API keys nor internal delivery paths belong in this script or its outputs.
"""

import argparse
from calendar import monthrange
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from math import ceil, floor
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

import netCDF4
import numpy as np
from pyproj import Transformer

from cds_fields import (ALIASES, ALL_FIELDS, LAND_STATES, LAND_ACCUMULATED, ERA_SURFACE,
                        ERA_PROFILE, PRESSURE_LEVELS, daily_schema, land_daily_fields, era_daily_fields)
from hydro_map.plotting import load_features
from plan_download import forcing_metadata
from probe_daymet_polygons import fractional_weights, strict_area_mean

DAILY_FIELDS = ['date', 'source', 'project_id', 'variable', 'value', 'units',
                'expected_hours', 'valid_hours', 'min_valid_area_fraction', 'qc']
# Covers float32 coordinate rounding up to 360 degrees; never use relative tolerance.
GRID_ATOL = 2e-5


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False); stream.write('\n')
    temporary.replace(path)


def dates(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def date_chunks(start, end, days=7):
    while start <= end:
        month_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        stop = min(start+timedelta(days=days-1), end, month_end)
        yield start, stop
        start = stop+timedelta(days=1)


def make_requests(product, start, area, end=None):
    """Each request uses one calendar month; accumulated dates refer to endpoints."""
    end = end or start
    if start > end or (start.year, start.month) != (end.year, end.month):
        raise ValueError('A request batch must lie within one calendar month')
    groups = [('states', 'reanalysis-era5-land', LAND_STATES, start, end, 24),
              ('accumulated', 'reanalysis-era5-land', LAND_ACCUMULATED,
               start+timedelta(days=1), end+timedelta(days=1), 1)] if product == 'era5-land' else [
              ('surface', 'reanalysis-era5-single-levels', ERA_SURFACE, start, end, 24),
              ('profiles', 'reanalysis-era5-pressure-levels', ERA_PROFILE, start, end, 24)]
    result = []
    for kind, dataset, fields, first, last, hours in groups:
        for a, b in date_chunks(first, last, 31):
            request = dict(product_type=['reanalysis'], variable=[v[0] for v in fields.values()],
                           year=f'{a.year:04d}', month=f'{a.month:02d}',
                           day=[f'{d.day:02d}' for d in dates(a, b)],
                           time=[f'{hour:02d}:00' for hour in range(hours)],
                           area=area, data_format='netcdf', download_format='unarchived')
            if product == 'era5-land':
                request.pop('product_type')
            else:
                request['year'] = [request['year']]
                request['month'] = [request['month']]
            if kind == 'profiles':
                request['pressure_level'] = [str(p) for p in PRESSURE_LEVELS]
            result.append(dict(kind=kind, dataset=dataset, request=request, fields=list(fields)))
    return result


@contextmanager
def netcdf_paths(path):
    """CDS can return a ZIP with separate stepType files despite unarchived mode."""
    if not zipfile.is_zipfile(path):
        yield [Path(path)]; return
    with tempfile.TemporaryDirectory(prefix='cds-netcdf-') as tmp, zipfile.ZipFile(path) as archive:
        entries = [member for member in archive.infolist() if not member.is_dir()]
        if (len(entries) > 32 or sum(member.file_size for member in entries) > 8_000_000_000
                or any(PurePosixPath(m.filename).is_absolute() or '..' in PurePosixPath(m.filename).parts
                       or '\\' in m.filename for m in entries)):
            raise ValueError('Unsafe or oversized CDS archive')
        paths = []
        for index, member in enumerate(entries):
            if Path(member.filename).suffix.lower() not in {'.nc', '.nc4'}:
                raise ValueError('Unexpected non-NetCDF member in CDS archive')
            destination = Path(tmp)/f'{index}.nc'
            with archive.open(member) as source, destination.open('wb') as target:
                for block in iter(lambda: source.read(1024*1024), b''):
                    target.write(block)
            paths.append(destination)
        yield paths


def _units_match(actual, expected):
    canonical = lambda value: value.replace('**', '^').replace(' ', '').lower()
    accepted = {canonical(expected)}
    if expected == '1':
        accepted |= {'(0-1)', '~', 'dimensionless'}
    if expected == 'm of water equivalent':
        accepted |= {'m'}
    return canonical(actual) in accepted


def _regular_axis(values, name):
    if (values.ndim != 1 or len(values) < 2 or not np.all(np.isfinite(values))
            or np.any(np.diff(values) == 0)):
        raise ValueError(f'Expected a unique regular {name} grid')
    regular = np.linspace(values[0], values[-1], len(values))
    if not np.allclose(values, regular, rtol=0, atol=GRID_ATOL):
        raise ValueError(f'Expected a unique regular {name} grid')
    # Uniform cell edges avoid gaps caused by coordinate-encoding jitter.
    return regular


def _same_grid(reference, current):
    return all(a.shape == b.shape and np.allclose(a, b, rtol=0, atol=GRID_ATOL)
               for a, b in zip(reference, current))


def _grid_diagnostics(reference, current):
    def summary(axis):
        return dict(count=len(axis), first_degrees=float(axis[0]), last_degrees=float(axis[-1]),
                    step_degrees=float((axis[-1]-axis[0])/(len(axis)-1)))
    return {name: dict(reference=summary(a), received=summary(b),
                       max_abs_difference_degrees=float(np.max(np.abs(a-b))) if a.shape == b.shape else None)
            for name, a, b in zip(['latitude', 'longitude'], reference, current)}


def _read_variable(ds, variable, selected, times):
    dimensions = list(variable.dimensions)
    time_dim = next((d for d in dimensions if d in {'valid_time', 'time'}), None)
    slices = [slice(None)]*len(dimensions)
    if time_dim:
        # Selecting a contiguous day before reading keeps multiweek files off RAM.
        slices[dimensions.index(time_dim)] = slice(selected[0], selected[-1]+1)
    data = np.ma.asarray(variable[tuple(slices)], dtype=float).filled(np.nan)
    for dimension in list(dimensions):
        if dimension in {'valid_time', 'time', 'latitude', 'longitude', 'pressure_level', 'level'}:
            continue
        axis = dimensions.index(dimension)
        if dimension == 'expver':
            moved = np.moveaxis(data, axis, 0)
            merged = moved[0].copy()
            for version in moved[1:]:
                overlap = np.isfinite(merged) & np.isfinite(version)
                if np.any(merged[overlap] != version[overlap]):
                    raise ValueError('Conflicting expver values')
                merged = np.where(np.isfinite(merged), merged, version)
            data = merged
        elif data.shape[axis] == 1:
            data = np.take(data, 0, axis=axis)
        else:
            raise ValueError(f'Unsupported non-singleton dimension: {dimension}')
        dimensions.remove(dimension)
    if time_dim is None:
        data = np.broadcast_to(data, (len(selected), *data.shape)); dimensions.insert(0, 'valid_time')
        time_dim = 'valid_time'
    level_dim = next((d for d in dimensions if d in {'pressure_level', 'level'}), None)
    order = [time_dim]+([level_dim] if level_dim else [])+['latitude', 'longitude']
    if set(order) != set(dimensions):
        raise ValueError('Unexpected source dimensions')
    data = np.transpose(data, [dimensions.index(d) for d in order])
    lat = np.asarray(ds['latitude'][:], float)
    lon = (np.asarray(ds['longitude'][:], float)+180) % 360-180
    lat_order, lon_order = np.argsort(-lat), np.argsort(lon)
    lat, lon = lat[lat_order], lon[lon_order]
    lat, lon = _regular_axis(lat, 'latitude'), _regular_axis(lon, 'longitude')
    data = np.take(np.take(data, lat_order, axis=-2), lon_order, axis=-1)
    levels = None
    if level_dim:
        levels = np.asarray(ds[level_dim][:], float)
        if getattr(ds[level_dim], 'units', 'hPa') not in {'hPa', 'millibars', 'millibar'}:
            raise ValueError('Expected hPa pressure coordinates')
        level_order = np.argsort(-levels); levels = levels[level_order]
        data = np.take(data, level_order, axis=1)
    return dict(times=times[selected], latitude=lat, longitude=lon, levels=levels, data=data)


def read_response(path, required, day=None):
    """Read one UTC day, normalizing time, grid order and complementary expver."""
    result = {}
    with netcdf_paths(path) as paths:
        for filename in paths:
            with netCDF4.Dataset(filename) as ds:
                time_name = next((name for name in ['valid_time', 'time'] if name in ds.variables), None)
                if time_name is None:
                    raise ValueError('Missing valid-time coordinate')
                coordinate = ds[time_name]
                decoded = netCDF4.num2date(coordinate[:], coordinate.units,
                                           calendar=getattr(coordinate, 'calendar', 'standard'),
                                           only_use_cftime_datetimes=False, only_use_python_datetimes=True)
                times = np.array([datetime(d.year, d.month, d.day, d.hour, d.minute, d.second,
                                            tzinfo=timezone.utc).timestamp() for d in np.atleast_1d(decoded)], dtype=np.int64)
                if np.any(times % 3600) or np.any(np.diff(times) <= 0):
                    raise ValueError('Expected unique increasing exact UTC hours')
                selected = np.arange(len(times))
                if day is not None:
                    first = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
                    selected = selected[(times >= first) & (times < first+86400)]
                if not len(selected):
                    continue
                for raw_name, variable in ds.variables.items():
                    name = ALIASES.get(raw_name, raw_name)
                    if name not in required:
                        continue
                    if not _units_match(getattr(variable, 'units', ''), ALL_FIELDS[name][1]):
                        raise ValueError(f'Unexpected units for {name}: {getattr(variable, "units", "missing")}')
                    expected_step = 'accum' if name in LAND_ACCUMULATED else 'instant'
                    if getattr(variable, 'GRIB_stepType', expected_step) != expected_step:
                        raise ValueError(f'Unexpected stepType for {name}')
                    record = _read_variable(ds, variable, selected, times)
                    if name in result:
                        old = result[name]
                        if (not _same_grid((old['latitude'], old['longitude']),
                                           (record['latitude'], record['longitude']))
                                or any(not np.array_equal(old[key], record[key]) for key in ['times', 'levels'])
                                or not np.array_equal(old['data'], record['data'], equal_nan=True)):
                            raise ValueError(f'Conflicting duplicate field: {name}')
                    result[name] = record
    if set(result) != set(required):
        raise ValueError(f'Missing requested CDS variables: {sorted(set(required)-result.keys())}')
    return result


def align_hours(record, day, count=24):
    first = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    wanted = first + np.arange(count)*3600
    result = np.full((count, *record['data'].shape[1:]), np.nan)
    by_time = {stamp: i for i, stamp in enumerate(record['times'])}
    for index, stamp in enumerate(wanted):
        if stamp in by_time:
            result[index] = record['data'][by_time[stamp]]
    return result


def process_day(fields, weights, day, source):
    rows = []
    for project, weight in sorted(weights.items()):
        for name, (data, units, statistic, flags) in sorted(fields.items()):
            endpoint = statistic == '24h_endpoint'
            arrays = data[None, ...] if endpoint else data
            values, fractions, reasons = [], [], set()
            for hour, field in enumerate(arrays):
                value, fraction, valid = strict_area_mean(field, weight)
                if flags is not None:
                    reasons.update(str(flag) for flag in np.unique(flags[hour][weight > 0]) if flag != 'valid')
                values.append(value if valid else np.nan); fractions.append(fraction)
            count = sum(np.isfinite(values))
            valid_hours = count*24 if endpoint else count
            complete = valid_hours == 24 and len(arrays) == (1 if endpoint else 24)
            value = ''
            if complete:
                value = float({'mean': np.mean, 'minimum': np.min, 'maximum': np.max,
                               '24h_endpoint': np.mean}[statistic](values))
            qc = 'valid' if complete else (';'.join(sorted(reasons)) or 'spatial_or_temporal_missing')
            rows.append(dict(date=day.isoformat(), source=source, project_id=project, variable=name,
                             value=value, units=units, expected_hours=24, valid_hours=valid_hours,
                             min_valid_area_fraction=min(fractions, default=0.), qc=qc))
    return rows


def padded_area(features, spacing):
    bounds = np.array([f['geometry'].bounds for f in features])
    west, south = bounds[:, :2].min(axis=0); east, north = bounds[:, 2:].max(axis=0)
    if east-west > 180:
        raise ValueError('Split antimeridian-crossing catchments into separate extraction runs')
    return [min(90., (ceil(north/spacing)+1)*spacing),
            max(-180., (floor(west/spacing)-1)*spacing),
            max(-90., (floor(south/spacing)-1)*spacing),
            min(180., (ceil(east/spacing)+1)*spacing)]


def fetch_response(spec, cache_dir, manifest, client, cache_only=False):
    identity = {key: spec[key] for key in ['dataset', 'request']}
    request_hash = json_hash(identity)
    path = cache_dir/(request_hash+'.nc')
    cached = manifest.get(request_hash, {})
    if path.exists() and cached.get('request') == identity and cached.get('sha256') == sha256(path):
        return path
    if cache_only:
        raise ValueError(f'Missing or invalid cached CDS response: {request_hash}')
    if client is None:
        raise ValueError('A CDS client is required for uncached requests')
    temporary = path.with_suffix('.part')
    try:
        client.retrieve(spec['dataset'], spec['request'], str(temporary))
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError('CDS returned an empty response')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    manifest[request_hash] = dict(request=identity, request_sha256=request_hash, sha256=sha256(path),
                                  bytes=path.stat().st_size,
                                  retrieved_utc=datetime.now(timezone.utc).isoformat())
    atomic_json(cache_dir/'requests.manifest.json', manifest)
    return path


def run_pipeline(geojson, product, output_dir, cache_dir, start, end, *, projects=None,
                 chunk_days=7, dry_run=False, cache_only=False, client=None):
    """Return run metadata. Existing completed months require verified file hashes."""
    geojson, output_dir, cache_dir = map(Path, [geojson, output_dir, cache_dir])
    if isinstance(start, str): start = date.fromisoformat(start)
    if isinstance(end, str): end = date.fromisoformat(end)
    if start > end or not 1 <= chunk_days <= 31 or product not in {'era5-land', 'era5'}:
        raise ValueError('Invalid product, period or chunk-days (1–31)')
    features = load_features(geojson)
    identifiers = [f['properties'].get('id') for f in features]
    if (not features or len(set(identifiers)) != len(identifiers)
            or any(not isinstance(i, str) or not i.strip() or i != i.strip() for i in identifiers)
            or any(f['properties'].get('part') != 'local' for f in features)):
        raise ValueError('Require unique nonempty project identifiers and explicit local catchments')
    selected = sorted(projects or identifiers)
    if len(set(selected)) != len(selected) or set(selected)-set(identifiers):
        raise ValueError('Selected projects must be unique and present in the geometry file')
    forcing = forcing_metadata(features, selected)
    features = [f for f in features if f['properties']['id'] in selected]
    area = padded_area(features, .1 if product == 'era5-land' else .25)
    source = 'era5_land_cds' if product == 'era5-land' else 'era5_cds'
    dependencies = [Path(__file__), Path(__file__).with_name('cds_fields.py'),
                    Path(__file__).with_name('probe_daymet_polygons.py'),
                    Path(__file__).with_name('plan_download.py'), Path(__file__).with_name('routing_metadata.py')]
    run = dict(source=source, source_id=source, product=product, geometry_sha256=sha256(geojson),
               start=start.isoformat(), end=end.isoformat(), day='UTC', projects=selected,
               **forcing, area_north_west_south_east=area, chunk_days=chunk_days,
               daily_schema=daily_schema(product), code_sha256={p.name: sha256(p) for p in dependencies},
               methods=dict(spatial='WGS84 geodesic polygon/native-grid intersections; full-area coverage required',
                            grid_coordinates='Regular axes reconstructed between endpoints; maximum coordinate residual and cross-field difference 0.00002 degrees; no data interpolation',
                            states='24 instantaneous hours 00–23 UTC; extremes of hourly catchment means',
                            accumulations='ERA5-Land D+1 00 UTC endpoint represents the complete previous 24 hours; valid_hours=24 for a valid endpoint',
                            evapotranspiration='Negative ECMWF evaporation multiplied by -1000; negative values retain condensation',
                            root_zone='Thickness weighting over 0–100 cm: .07*L1+.21*L2+.72*L3; L4 is100–289 cm',
                            freezing_level='One above-ground warm-to-cold 0C crossing in1000–300 hPa profile; geopotential metres; no/multiple crossings remain missing'))
    if dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        plan = dict(run, status='dry_run_no_download', request_count=sum(len(make_requests(product, a, area, b)) for a, b in date_chunks(start, end, chunk_days)),
                    first_requests=make_requests(product, start, area, min(end, next(date_chunks(start, end, chunk_days))[1])))
        atomic_json(output_dir/'request_plan.json', plan)
        return plan
    output_dir.mkdir(parents=True, exist_ok=True); cache_dir.mkdir(parents=True, exist_ok=True)
    run_path = output_dir/'run.json'
    if run_path.exists() and json.loads(run_path.read_text()) != run:
        previous = json.loads(run_path.read_text())
        processing_keys = {'code_sha256', 'methods', 'daily_schema'}
        request_config = lambda value: {key: item for key, item in value.items() if key not in processing_keys}
        if (request_config(previous) != request_config(run)
                or any(output_dir.glob('month_*.json')) or any(output_dir.glob('daily_*.csv'))
                or any(output_dir.glob('daily_*.csv.gz'))):
            raise ValueError('Output configuration differs; use a new output directory and retain the cache directory')
        # No completed CDS output can mix processing versions. Raw request hashes remain unchanged.
        history = output_dir/'run_history'
        history.mkdir(exist_ok=True)
        atomic_json(history/f'{json_hash(previous)}.json', previous)
        print('Updated unfinished CDS processing configuration; retaining verified request cache', flush=True)
    atomic_json(run_path, run)
    manifest_path = cache_dir/'requests.manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    configuration_hash = json_hash(run)
    if client is None and not cache_only:
        import cdsapi
        client = cdsapi.Client()
    weights, grid = None, None
    identity = Transformer.from_crs(4326, 4326, always_xy=True)
    for month_start, month_end in date_chunks(start, end, 31):
        month = month_start.strftime('%Y-%m')
        daily_path, status_path = output_dir/f'daily_{month}.csv.gz', output_dir/f'month_{month}.json'
        if status_path.exists():
            status = json.loads(status_path.read_text())
            if (status.get('status') == 'complete' and status.get('configuration_sha256') == configuration_hash
                    and daily_path.exists() and status.get('daily_sha256') == sha256(daily_path)):
                print(f'{month}: verified existing output', flush=True); continue
        temporary = daily_path.with_suffix('.part')
        source_hashes, count = {}, 0
        try:
            with gzip.open(temporary, 'wt', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=DAILY_FIELDS); writer.writeheader()
                for first, last in date_chunks(month_start, month_end, chunk_days):
                    specs = make_requests(product, first, area, last)
                    responses = [(spec, fetch_response(spec, cache_dir, manifest, client, cache_only)) for spec in specs]
                    source_hashes.update({path.name: sha256(path) for _, path in responses})
                    for day in dates(first, last):
                        records = {}
                        for spec, path in responses:
                            target_day = day+timedelta(days=1) if spec['kind'] == 'accumulated' else day
                            request_days = spec['request']['day']
                            request_days = [request_days] if isinstance(request_days, str) else request_days
                            request_year = spec['request']['year']; request_month = spec['request']['month']
                            if isinstance(request_year, list): request_year = request_year[0]
                            if isinstance(request_month, list): request_month = request_month[0]
                            if (f'{target_day.year:04d}' != request_year or f'{target_day.month:02d}' != request_month
                                    or f'{target_day.day:02d}' not in request_days): continue
                            records[spec['kind']] = read_response(path, spec['fields'], target_day)
                        for kind, fields in records.items():
                            for name, record in fields.items():
                                current = (record['latitude'], record['longitude'])
                                label = f'{day} {kind}/{name}'
                                if grid is None:
                                    grid = current
                                    grid_label = label
                                    weights, info = {}, []
                                    for feature in features:
                                        project = feature['properties']['id']
                                        weights[project], detail = fractional_weights(feature['geometry'], grid[1], grid[0], identity)
                                        info.append(dict(project_id=project, **detail))
                                    atomic_json(output_dir/'polygon_weights.json', info)
                                elif not _same_grid(grid, current):
                                    axes = _grid_diagnostics(grid, current)
                                    diagnostic = output_dir/'grid_mismatch.json'
                                    atomic_json(diagnostic, dict(reference_field=grid_label, received_field=label,
                                                                tolerance_degrees=GRID_ATOL, axes=axes))
                                    detail = '; '.join(f'{axis}: {values}' for axis, values in axes.items())
                                    raise ValueError(f'CDS grid mismatch for {label} versus {grid_label}; '
                                                     f'{detail}. Details: {diagnostic}')
                        if product == 'era5-land':
                            states = {key: align_hours(record, day) for key, record in records['states'].items()}
                            accumulated = {key: align_hours(record, day+timedelta(days=1), 1)[0]
                                           for key, record in records['accumulated'].items()}
                            fields = land_daily_fields(states, accumulated)
                        else:
                            surface = {key: align_hours(record, day) for key, record in records['surface'].items()}
                            profiles = {key: align_hours(record, day) for key, record in records['profiles'].items()}
                            levels = records['profiles']['t']['levels']
                            if (not np.array_equal(levels, PRESSURE_LEVELS)
                                    or not np.array_equal(levels, records['profiles']['z']['levels'])):
                                raise ValueError('Missing or unexpected pressure levels')
                            fields = era_daily_fields(surface, profiles, levels)
                        rows = process_day(fields, weights, day, source)
                        writer.writerows(rows); count += len(rows)
            temporary.replace(daily_path)
            atomic_json(status_path, dict(status='complete', source_id=source, start=month_start.isoformat(), end=month_end.isoformat(),
                                          rows=count, configuration_sha256=configuration_hash,
                                          daily_sha256=sha256(daily_path), source_sha256=source_hashes))
            print(f'{month}: saved {count} daily values', flush=True)
        finally:
            temporary.unlink(missing_ok=True)
    (output_dir/'grid_mismatch.json').unlink(missing_ok=True)
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['geojson', 'output-dir', 'cache-dir']:
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--product', choices=['era5-land', 'era5'], required=True)
    parser.add_argument('--start', required=True); parser.add_argument('--end', required=True)
    parser.add_argument('--projects', nargs='+')
    parser.add_argument('--chunk-days', type=int, default=7)
    parser.add_argument('--dry-run', action='store_true'); parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    run_pipeline(**vars(args))


if __name__ == '__main__':
    main()
