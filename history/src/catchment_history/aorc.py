"""Extract AORC v1.1 hourly polygon means and strict UTC daily tables.

Reads only intersecting native Zarr chunks. Annual outputs are resumable;
meteorological chunks are discarded by default after aggregation. Missing
cells never receive zero precipitation or cause spatial renormalization.
RH/VPD, liquid-water wet bulb, rain/snow water equivalent and 10m wind speed
are calculated at synchronized native cells before averaging. Daily Hargreaves
PET uses the mean/min/max of hourly catchment temperatures.
"""
from collections import deque
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

from .aggregate import HOURLY_FIELDS, aggregate_daily, daily_schema
from .aorc_zarr import BASE, VARIABLES, axis, check, decode, metadata, weights
from .catchments import forcing_metadata, read_catchments
from .files import atomic_json, digest, json_hash
from .meteorology import DERIVED_FIELDS, DERIVED_METHODS, derive_native

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
DERIVED_INPUTS = {'APCP_surface', 'TMP_2maboveground', 'SPFH_2maboveground',
                  'PRES_surface', 'UGRD_10maboveground', 'VGRD_10maboveground'}


class Store:
    """Bounded public HTTP reads with verified cache objects and request hashes."""
    def __init__(self, directory, limit, offline=False):
        self.directory, self.limit, self.offline = directory, limit, offline
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.bytes = 0
        # Offline runs still know which objects the source reported as absent.
        self.absent = set()
        log = directory / 'requests.jsonl'
        if offline and log.exists():
            with log.open() as stream:
                self.absent = {record['url'] for record in map(json.loads, stream) if record.get('status') == 404}

    def path(self, url):
        return self.directory / hashlib.sha256(url.encode()).hexdigest()

    def discard(self, url):
        path = self.path(url)
        path.unlink(missing_ok=True)
        path.with_suffix('.json').unlink(missing_ok=True)

    def read(self, url):
        path = self.path(url)
        record_path = path.with_suffix('.json')
        if path.exists() and record_path.exists():
            record = json.loads(record_path.read_text())
            check(record['url'] == url and digest(path) == record['sha256'], 'Cache hash mismatch')
            return path.read_bytes(), path
        if self.offline and url in self.absent:
            raise FileNotFoundError(url)
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
    features = read_catchments(path)
    polygons = {feature['properties']['id']: feature['geometry'] for feature in features}
    if selected:
        check(set(selected) <= polygons.keys(), f'Unknown project IDs: {set(selected) - polygons.keys()}')
        polygons = {key: polygons[key] for key in selected}
    forcing = forcing_metadata(features, sorted(polygons))
    return (polygons, forcing) if include_metadata else polygons

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


def read_chunk(store, url, zstd):
    """Fetch and decode one Zarr chunk in a worker thread; None if the object is absent."""
    try:
        payload, _ = store.read(url)
    except FileNotFoundError:
        return None
    raw, _ = decode(payload, '<i2', zstd)
    check(raw.size == 144 * 128 * 256, f'Unexpected chunk shape: {url}')
    return raw.reshape(144, 128, 256)


def extract_native_series(store, metas, times_by_year, blocks, variables,
                          start, end, zstd, workers, keep):
    """Read each synchronized source chunk once, then aggregate native diagnostics.

    Worker threads fetch and decode the next few spatial blocks while the main
    thread aggregates the current one, so network transfer, decompression and
    area averaging overlap. Memory holds the decoded fields of those blocks and at
    most 24 selected hours of cells per polygon, plus annual catchment series.
    Raw files are discarded only after all polygons and diagnostics consumed them.
    """
    variables = sorted(set(variables))
    check(DERIVED_INPUTS <= set(variables), 'Derived fields require precipitation, temperature, q, p, u and v')
    definitions = {FIELDS[variable][0]: FIELDS[variable][1:] for variable in variables}
    definitions.update(DERIVED_FIELDS)
    targets = {name: requested_hours(start, end, kind == 'hour_ending_amount')
               for name, (_, kind) in definitions.items()}
    target = np.union1d(requested_hours(start, end, False), requested_hours(start, end, True))
    projects = sorted({project for block in blocks.values() for project in block})
    sums = {name: {p: np.zeros(len(target)) for p in projects} for name in definitions}
    coverage = {name: {p: np.zeros(len(target)) for p in projects} for name in definitions}
    target_years = np.array([dt.datetime.fromtimestamp(int(t), dt.timezone.utc).year for t in target])
    # Enough blocks in flight to keep every worker busy while one block is aggregated.
    window = max(1, workers // len(variables)) + 1
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
                items = sorted(blocks.items())
                pending = deque()

                def submit(position):
                    (by, bx), block = items[position]
                    urls = {v: BASE + f'{year}.zarr/{v}/{chunk_t}.{by}.{bx}' for v in variables}
                    pending.append((block, urls, {v: pool.submit(read_chunk, store, url, zstd)
                                                  for v, url in urls.items()}))

                ahead = 0
                for position in range(len(items)):
                    while ahead < len(items) and ahead <= position + window:
                        submit(ahead)
                        ahead += 1
                    block, urls, futures = pending.popleft()
                    raw_fields = {v: future.result() for v, future in futures.items()}
                    raw_fields = {v: raw for v, raw in raw_fields.items() if raw is not None}
                    consumed_urls = [urls[v] for v in raw_fields]
                    for project, (yy, xx, area) in block.items():
                        for offset in range(0, len(local_time), 24):
                            times = local_time[offset:offset + 24]
                            destination = dest_rows[offset:offset + 24]
                            values = {}
                            for variable in variables:
                                name = FIELDS[variable][0]
                                if variable in raw_fields:
                                    values[name] = unpack(raw_fields[variable][times[:, None], yy, xx],
                                        m[variable + '/.zattrs'], m[variable + '/.zarray'].get('fill_value'), name)
                                else:
                                    values[name] = np.full((len(times), len(yy)), np.nan)
                            values.update(derive_native(values))
                            for name, cells in values.items():
                                subtotal, valid_area = accumulate(cells, area)
                                sums[name][project][destination] += subtotal
                                coverage[name][project][destination] += valid_area
                    if not keep:
                        for url in consumed_urls:
                            store.discard(url)
                print(f'{year} synchronized native fields chunk {int(chunk_t) + 1}/61; '
                      f'network {store.bytes / 1e9:.3f} GB', flush=True)
    result = {}
    for name, stamps in targets.items():
        positions = np.searchsorted(target, stamps)
        result[name] = (stamps, {p: sums[name][p][positions] for p in projects},
                        {p: coverage[name][p][positions] for p in projects}, *definitions[name])
    return result


def run(geojson, start, end, output_dir, cache_dir, *, projects=None, workers=16, max_download_gb=2000,
        cache_only=False, keep_chunks=False, newest_first=False):
    """Write hourly series, daily values and a manifest for each year; finished years are verified and skipped."""
    check(start <= end, 'Start must not follow end')
    check(1 <= workers <= 32 and max_download_gb > 0, 'Invalid workers or download limit')
    zstd = shutil.which('zstd')
    check(zstd is not None, 'Install the zstd command-line decoder')
    features, forcing = load_polygons(geojson, projects, include_metadata=True)
    variables = sorted(VARIABLES)
    output_fields = [FIELDS[variable][0] for variable in variables] + list(DERIVED_FIELDS)
    pet_latitudes = {project: geometry.centroid.y for project, geometry in features.items()}
    output_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cache_dir, max_download_gb * 1e9, cache_only)
    config = dict(schema=2, geometry_sha256=digest(geojson), projects=sorted(features), **forcing,
                  start=str(start), end=str(end), variables=variables,
                  source=BASE, source_id='aorc_v1.1', area_crs='EPSG:6933', day='UTC',
                  derive=True, derived_methods=DERIVED_METHODS, pet_centroid_latitudes=pet_latitudes,
                  daily_schema=daily_schema(output_fields, include_pet=True),
                  code_sha256={name: digest(Path(__file__).with_name(name)) for name in
                           ('aorc.py', 'aggregate.py', 'aorc_zarr.py',
                            'catchments.py', 'meteorology.py')})
    config_hash = json_hash(config)
    config_path = output_dir / 'run.json'
    if config_path.exists():
        check(json.loads(config_path.read_text()) == config,
              'Output configuration differs; use a different output directory')
    else:
        atomic_json(config_path, config)
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

    def finalize(year, start, end, extracted):
        """Write one year's hourly series, daily statistics and manifest (manifest last)."""
        hourly = output_dir / f'hourly_{year}.csv.gz'
        daily = output_dir / f'daily_{year}.csv.gz'
        manifest = output_dir / f'year_{year}.json'
        series, qc_summary = {}, []
        for name, (target, sums, coverage, units, kind) in extracted.items():
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
        daily_rows = aggregate_daily(hourly, daily, start, end, pet_latitudes=pet_latitudes)
        atomic_json(manifest, dict(configuration_sha256=config_hash,
            hourly_sha256=digest(hourly), daily_sha256=digest(daily), daily_rows=daily_rows,
            status='computed_with_gaps' if any(r['valid_hours'] != r['hours'] for r in qc_summary)
                   else 'computed_all_hours_valid',
            hourly_quality=qc_summary, network_bytes_this_process=store.bytes))
        print(f'{year}: wrote {hourly.name}, {daily.name}; {daily_rows} daily rows', flush=True)

    # A finished year is written in the background while the next year downloads;
    # at most one year waits to be written, which bounds memory.
    background, pending = ThreadPoolExecutor(max_workers=1), None
    try:
        years = range(start.year, end.year + 1)
        for year in (reversed(years) if newest_first else years):
            hourly = output_dir / f'hourly_{year}.csv.gz'
            daily = output_dir / f'daily_{year}.csv.gz'
            manifest = output_dir / f'year_{year}.json'
            if manifest.exists():
                record = json.loads(manifest.read_text())
                check(record['configuration_sha256'] == config_hash and hourly.exists() and daily.exists()
                      and record['hourly_sha256'] == digest(hourly) and record['daily_sha256'] == digest(daily),
                      f'Completed output verification failed: {year}')
                incomplete = any(row['valid_hours'] != row['hours'] for row in record['hourly_quality'])
                state = 'contains gaps' if incomplete else 'all requested hours valid'
                print(f'{year}: verified existing output ({state}); skipped', flush=True)
                continue
            first, last = max(start, dt.date(year, 1, 1)), min(end, dt.date(year, 12, 31))
            load_year(year)
            if last.month == 12 and last.day == 31:
                load_year(year + 1, optional=year == end.year)
            extracted = extract_native_series(store, metas, times_by_year, blocks, variables,
                                               first, last, zstd, workers, keep_chunks)
            if pending is not None:
                pending.result()
            pending = background.submit(finalize, year, first, last, extracted)
        if pending is not None:
            pending.result()
    finally:
        background.shutdown(wait=True)
