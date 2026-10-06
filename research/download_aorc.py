#!/usr/bin/env python3
"""Extract AORC v1.1 hourly polygon means and strict UTC daily tables.

Reads only intersecting native Zarr chunks. Annual outputs are resumable;
meteorological chunks are discarded by default after aggregation. Missing
cells never receive zero precipitation or cause spatial renormalization.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time

import numpy as np
import requests
from shapely.geometry import shape

from aggregate_daily import HOURLY_FIELDS, aggregate_daily
from audit_aorc_cache import BASE, VARIABLES, axis, check, decode, metadata, weights
from routing_metadata import routing_metadata, routing_warning

FIELDS = {
    'APCP_surface': ('precipitation_mm', 'mm', 'hour_ending_amount'),
    'TMP_2maboveground': ('temperature_c', 'degC', 'instantaneous'),
    'SPFH_2maboveground': ('specific_humidity_kgkg', 'kg/kg', 'instantaneous'),
    'PRES_surface': ('surface_pressure_pa', 'Pa', 'instantaneous'),
    'DSWRF_surface': ('shortwave_down_wm2', 'W/m2', 'instantaneous'),
    'DLWRF_surface': ('longwave_down_wm2', 'W/m2', 'instantaneous'),
    'UGRD_10maboveground': ('u_wind_ms', 'm/s', 'instantaneous'),
    'VGRD_10maboveground': ('v_wind_ms', 'm/s', 'instantaneous'),
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


class Store:
    """Bounded public HTTP reads with verified cache objects and request hashes."""
    def __init__(self, directory, limit, offline=False):
        self.directory, self.limit, self.offline = directory, limit, offline
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.bytes = 0
        self.refresh_years = set()

    def path(self, url):
        return self.directory / hashlib.sha256(url.encode()).hexdigest()

    def discard(self, url):
        path = self.path(url)
        path.unlink(missing_ok=True)
        path.with_suffix('.json').unlink(missing_ok=True)

    def read(self, url):
        path = self.path(url)
        record_path = path.with_suffix('.json')
        refresh = any(url.startswith(BASE + f'{year}.zarr/') for year in self.refresh_years)
        if not refresh and path.exists() and record_path.exists():
            record = json.loads(record_path.read_text())
            check(record['url'] == url and digest(path) == record['sha256'], 'Cache hash mismatch')
            return path.read_bytes(), path
        check(not self.offline, f'Not available in offline cache: {url}')
        for attempt in range(3):
            try:
                started = time.monotonic()
                with requests.get(url, stream=True, timeout=(15, 45)) as response:
                    if response.status_code == 404:
                        self.log(dict(url=url, status=404, bytes=0))
                        raise FileNotFoundError(url)
                    response.raise_for_status()
                    content = bytearray()
                    for block in response.iter_content(1024 * 1024):
                        if time.monotonic() - started > 60:
                            raise requests.Timeout('Response exceeded the streaming deadline')
                        with self.lock:
                            self.bytes += len(block)
                            check(self.bytes <= self.limit, 'Network download budget exceeded')
                        content.extend(block)
                    payload = bytes(content)
                    record = dict(url=url, status=200, bytes=len(payload),
                                  sha256=hashlib.sha256(payload).hexdigest())
                    temporary = path.with_suffix('.tmp')
                    temporary.write_bytes(payload)
                    temporary.replace(path)
                    atomic_json(record_path, record)
                    self.log(record)
                    return payload, path
            except (requests.RequestException, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def log(self, record):
        record['accessed_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.lock, (self.directory / 'requests.jsonl').open('a') as stream:
            stream.write(json.dumps(record) + '\n')


def requested_hours(start, end, ending):
    first = int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp())
    count = ((end - start).days + 1) * 24
    return first + (np.arange(count, dtype=np.int64) + int(ending)) * 3600


def unpack(raw, attrs, fill, variable):
    invalid = np.zeros(raw.shape, dtype=bool)
    for code in (attrs.get('missing_value'), attrs.get('_FillValue'), fill):
        if code is not None:
            invalid |= raw == code
    data = raw.astype(np.float64) * attrs['scale_factor'] + attrs.get('add_offset', 0.)
    data[invalid] = np.nan
    if variable == 'temperature_c':
        data -= 273.15
    # Broad physical guards detect corrupt decoding without clipping values.
    if variable in ('precipitation_mm', 'shortwave_down_wm2', 'longwave_down_wm2'):
        data[data < 0] = np.nan
    elif variable == 'specific_humidity_kgkg':
        data[(data < 0) | (data >= 1)] = np.nan
    elif variable == 'surface_pressure_pa':
        data[(data < 10000) | (data > 120000)] = np.nan
    elif variable == 'temperature_c':
        data[(data < -100) | (data > 70)] = np.nan
    elif variable in ('u_wind_ms', 'v_wind_ms'):
        data[np.abs(data) > 200] = np.nan
    return data


def accumulate(values, normalized_area):
    valid = np.isfinite(values)
    return np.where(valid, values, 0.) @ normalized_area, valid @ normalized_area


def load_polygons(path, selected, include_metadata=False):
    doc = json.loads(path.read_text())
    check(doc.get('type') == 'FeatureCollection' and not doc.get('crs'), 'Use WGS84 GeoJSON')
    features, parts, project_forcing, references = {}, set(), {}, {}
    for feature in doc['features']:
        identifier = feature['properties']['id']
        check(isinstance(identifier, str) and identifier.strip() == identifier and identifier
              and identifier not in features,
              'GeoJSON requires unique nonempty string properties.id values')
        part = feature['properties'].get('part')
        check(part in ('local', 'total'), 'Declare properties.part as local or total')
        parts.add(part)
        geometry = shape(feature['geometry'])
        check(geometry.is_valid and not geometry.is_empty and
              geometry.geom_type in ('Polygon', 'MultiPolygon'), f'Invalid geometry: {identifier}')
        west, south, east, north = geometry.bounds
        check(-180 <= west < east <= 180 and -90 <= south < north <= 90, 'Invalid lon/lat bounds')
        props = feature['properties']
        group = props.get('forcing_group', identifier)
        check(isinstance(group, str) and group and group.strip() == group,
              f'{identifier}: forcing_group must be a nonempty string')
        if group in references:
            other_geometry, other_part = references[group]
            check(part == other_part and geometry.equals(other_geometry),
                  f'Forcing group {group} must have the same geometry and part')
        references[group] = (geometry, part)
        record = {'forcing_group': group, **routing_metadata(identifier, props), **{key: props[key] for key in
                  ('geometry_status', 'shared_outlet_projects') if key in props}}
        members = record.get('shared_outlet_projects', [identifier])
        check(isinstance(members, list) and (not members or identifier in members) and
              all(isinstance(member, str) and member.strip() for member in members) and
              len(set(members)) == len(members), f'{identifier}: invalid shared_outlet_projects')
        project_forcing[identifier] = record
        features[identifier] = geometry
    check(features, 'No polygons found')
    check(len(parts) == 1, 'Do not mix local and total catchments')
    if selected:
        check(set(selected) <= features.keys(), f'Unknown project IDs: {set(selected) - features.keys()}')
        features = {key: features[key] for key in selected}
    project_forcing = {identifier: project_forcing[identifier] for identifier in sorted(features)}
    groups, warnings = {}, []
    for identifier, record in project_forcing.items():
        groups.setdefault(record['forcing_group'], []).append(identifier)
        if record.get('catchment_role') == 'natural_reach_at_tailrace':
            warnings.append(routing_warning(identifier, record))
    for group, identifiers in sorted(groups.items()):
        shared = len(identifiers) > 1 or any(
            project_forcing[identifier].get('geometry_status') == 'shared_unit_approximation'
            or len(project_forcing[identifier].get('shared_outlet_projects', [])) > 1
            for identifier in identifiers)
        if shared:
            warnings.append(f'Forcing group {group} uses a shared catchment approximation. '
                            'Do not sum member catchment areas or derived water volumes; '
                            'count the forcing group once.')
    forcing = dict(project_forcing=project_forcing, forcing_groups=groups,
                   forcing_group_count=len(groups), warnings=warnings)
    return (features, forcing) if include_metadata else features


def block_weights(polygon_weights):
    blocks = {}
    for project, (yy, xx, area) in polygon_weights.items():
        normalized = area / area.sum()
        labels = np.column_stack((yy // 128, xx // 256))
        for by, bx in np.unique(labels, axis=0):
            select = (labels[:, 0] == by) & (labels[:, 1] == bx)
            blocks.setdefault((int(by), int(bx)), {})[project] = (
                yy[select] % 128, xx[select] % 256, normalized[select])
    return blocks


def extract_series(store, metas, times_by_year, blocks, variable, target, zstd, workers, keep):
    projects = sorted({project for block in blocks.values() for project in block})
    sums = {p: np.zeros(len(target)) for p in projects}
    coverage = {p: np.zeros(len(target)) for p in projects}
    field = FIELDS[variable][0]
    target_years = np.array([dt.datetime.fromtimestamp(int(t), dt.timezone.utc).year for t in target])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for year in sorted(set(target_years)):
            if year not in metas:
                continue
            m, source_times = metas[year], times_by_year[year]
            dest = np.flatnonzero(target_years == year)
            indexes = np.searchsorted(source_times, target[dest])
            check(np.all(indexes < len(source_times)) and np.array_equal(source_times[indexes], target[dest]),
                  f'Requested timestamps absent from time axis: {year}')
            for chunk_t in np.unique(indexes // 144):
                selected = indexes // 144 == chunk_t
                dest_rows, local_time = dest[selected], indexes[selected] % 144
                keys = sorted(blocks)
                # Bound both in-flight downloads and decoded arrays to one small batch.
                for offset in range(0, len(keys), workers):
                    batch = keys[offset:offset + workers]
                    urls = [BASE + f'{year}.zarr/{variable}/{chunk_t}.{by}.{bx}' for by, bx in batch]
                    futures = [pool.submit(store.read, url) for url in urls]
                    for key, url, future in zip(batch, urls, futures):
                        try:
                            payload, _ = future.result()
                        except FileNotFoundError:
                            continue
                        raw, _ = decode(payload, '<i2', zstd)
                        check(raw.size == 144 * 128 * 256, f'Unexpected chunk shape: {url}')
                        raw = raw.reshape(144, 128, 256)
                        attrs, array = m[variable + '/.zattrs'], m[variable + '/.zarray']
                        for project, (yy, xx, area) in blocks[key].items():
                            values = unpack(raw[local_time[:, None], yy, xx], attrs,
                                            array.get('fill_value'), field)
                            subtotal, valid_area = accumulate(values, area)
                            sums[project][dest_rows] += subtotal
                            coverage[project][dest_rows] += valid_area
                        if not keep:
                            store.discard(url)
                print(f'{year} {variable} chunk {int(chunk_t) + 1}/61; '
                      f'network {store.bytes / 1e9:.3f} GB', flush=True)
    return sums, coverage


def run(args):
    check(args.start <= args.end, 'Start must not follow end')
    check(not (args.cache_only and args.refresh_incomplete), 'Refreshing requires online source access')
    check(1 <= args.workers <= 8 and args.max_download_gb > 0, 'Invalid workers or download limit')
    zstd = shutil.which('zstd')
    check(zstd is not None, 'Install the zstd command-line decoder')
    features, forcing = load_polygons(args.geojson, args.projects, include_metadata=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    store = Store(args.cache_dir, args.max_download_gb * 1e9, args.cache_only)
    config = dict(schema=2, geometry_sha256=digest(args.geojson), projects=sorted(features), **forcing,
                  start=str(args.start), end=str(args.end), variables=sorted(args.variables),
                  source=BASE, area_crs='EPSG:6933', day='UTC',
                  code_sha256={name: digest(Path(__file__).with_name(name)) for name in
                           ('download_aorc.py', 'aggregate_daily.py', 'audit_aorc_cache.py', 'routing_metadata.py')})
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    config_path = args.output_dir / 'run.json'
    if config_path.exists():
        check(json.loads(config_path.read_text()) == config,
              'Output configuration differs; use a different output directory')
    else:
        atomic_json(config_path, config)
    for warning in forcing['warnings']:
        print(warning, flush=True)
    metas, times_by_year, reference, blocks = {}, {}, None, None

    def load_year(year, optional=False):
        nonlocal reference, blocks
        if year in metas:
            return
        try:
            m = metadata(store, year)
        except FileNotFoundError:
            if optional:
                print(f'Missing boundary archive: {year}; affected daily values will be blank.', flush=True)
                return
            raise
        times = axis(store, year, 'time', m, zstd)
        first = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        count = (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days * 24
        check(np.array_equal(times, first + np.arange(count) * 3600), f'Incomplete annual time axis: {year}')
        coords = [axis(store, year, name, m, zstd) for name in ('latitude', 'longitude')]
        check(all(np.all(np.diff(values) > 0) for values in coords), 'Coordinates must increase')
        if reference is None:
            reference = coords
            blocks = block_weights(weights(features, *coords))
            print(f'{len(features)} polygons, {len(blocks)} spatial chunks', flush=True)
        else:
            check(all(np.array_equal(a, b) for a, b in zip(reference, coords)), 'Grid changed between years')
        metas[year], times_by_year[year] = m, times

    for year in range(args.start.year, args.end.year + 1):
        hourly = args.output_dir / f'hourly_{year}.csv.gz'
        daily = args.output_dir / f'daily_{year}.csv.gz'
        manifest = args.output_dir / f'year_{year}.json'
        if manifest.exists():
            record = json.loads(manifest.read_text())
            check(record['configuration_sha256'] == config_hash and hourly.exists() and daily.exists()
                  and record['hourly_sha256'] == digest(hourly) and record['daily_sha256'] == digest(daily),
                  f'Completed output verification failed: {year}')
            incomplete = any(row['valid_hours'] != row['hours'] for row in record['hourly_quality'])
            if not (incomplete and args.refresh_incomplete):
                state = 'contains gaps' if incomplete else 'all requested hours valid'
                print(f'{year}: verified existing output ({state}); skipped', flush=True)
                continue
            print(f'{year}: refreshing incomplete output from the source', flush=True)
            # A refresh interruption must not leave old hashes claiming new files.
            manifest.unlink()
        if args.refresh_incomplete:
            store.refresh_years.update((year, year + 1))
            for changed_year in (year, year + 1):
                metas.pop(changed_year, None)
                times_by_year.pop(changed_year, None)
        start, end = max(args.start, dt.date(year, 1, 1)), min(args.end, dt.date(year, 12, 31))
        load_year(year)
        if end.month == 12 and end.day == 31 and 'APCP_surface' in args.variables:
            load_year(year + 1, optional=year == args.end.year)
        series, qc_summary = {}, []
        for variable in args.variables:
            name, units, kind = FIELDS[variable]
            target = requested_hours(start, end, kind == 'hour_ending_amount')
            sums, coverage = extract_series(store, metas, times_by_year, blocks, variable,
                                            target, zstd, args.workers, args.keep_chunks)
            for project in features:
                full = np.abs(coverage[project] - 1) <= 1e-9
                values = np.where(full, sums[project], np.nan)
                series[(project, name)] = target, values, coverage[project], units, kind
                qc_summary.append(dict(project_id=project, variable=name, hours=len(target),
                                       valid_hours=int(full.sum()), min_valid_area_fraction=float(coverage[project].min())))
        temporary = hourly.with_suffix('.tmp')
        with gzip.open(temporary, 'wt', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=HOURLY_FIELDS)
            writer.writeheader()
            for (project, name), (target, values, coverage, units, kind) in sorted(series.items()):
                for stamp, value, area in zip(target, values, coverage):
                    valid = np.isfinite(value)
                    writer.writerow(dict(source='aorc_v1.1', project_id=project,
                        time_utc=dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc).isoformat(),
                        variable=name, value=format(value, '.12g') if valid else '', units=units,
                        temporal_kind=kind, valid_area_fraction=format(min(area, 1.), '.12g'),
                        qc='valid' if valid else ('source_unavailable' if area == 0 else 'incomplete_area')))
        temporary.replace(hourly)
        daily_rows = aggregate_daily(hourly, daily, start, end)
        atomic_json(manifest, dict(configuration_sha256=config_hash,
            hourly_sha256=digest(hourly), daily_sha256=digest(daily), daily_rows=daily_rows,
            status='computed_with_gaps' if any(r['valid_hours'] != r['hours'] for r in qc_summary)
                   else 'computed_all_hours_valid',
            hourly_quality=qc_summary, network_bytes_this_process=store.bytes))
        print(f'{year}: wrote {hourly.name}, {daily.name}; {daily_rows} daily rows', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--start', type=dt.date.fromisoformat, default=dt.date(1996, 1, 1))
    parser.add_argument('--end', type=dt.date.fromisoformat, default=dt.date(2025, 12, 31))
    parser.add_argument('--projects', nargs='+')
    parser.add_argument('--variables', choices=VARIABLES, nargs='+', default=VARIABLES)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--max-download-gb', type=float, default=1.)
    parser.add_argument('--cache-only', action='store_true')
    parser.add_argument('--refresh-incomplete', action='store_true',
                        help='Re-fetch and recompute years containing invalid hours; bypass their cached objects')
    parser.add_argument('--keep-chunks', action='store_true')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
