"""Download a bounded public NCAR ERA5-Land demo and export polygon hourly data."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import ceil, floor
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import netCDF4
import numpy as np
from pyproj import Transformer

from hydro_map.plotting import load_features
from probe_daymet_polygons import fractional_weights, strict_area_mean
from probe_era5_land import FIELDS
from plan_download import forcing_metadata

HOST = 'https://tds.gdex.ucar.edu/thredds/'
SOURCE = 'era5_land_ncar'
NEGATIVE_TOLERANCE_MM = 1e-4
COLUMNS = ('source', 'project_id', 'time_utc', 'variable', 'value', 'units',
           'temporal_kind', 'valid_area_fraction', 'qc')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def subset_query(features):
    """Pad the combined geometry by one native cell and request 0–360 longitude."""
    if not features or any(f['properties'].get('part') != 'local' for f in features):
        raise ValueError('The demo requires explicit local catchments')
    bounds = np.array([feature['geometry'].bounds for feature in features])
    west, south = bounds[:, :2].min(axis=0)
    east, north = bounds[:, 2:].max(axis=0)
    west, east = round((floor(west*10)-1)/10 % 360, 1), round((ceil(east*10)+1)/10 % 360, 1)
    if west >= east:
        raise ValueError('Subset crosses the 0-degree longitude seam; split the input geometries')
    return dict(west=west, south=max(-90, (floor(south*10)-1)/10),
                east=east, north=min(90, (ceil(north*10)+1)/10))


def catalog_segments(content, variable, start, end):
    """Select observed catalog paths whose inclusive valid-time ranges overlap."""
    group, param, _ = FIELDS[variable]
    prefix = f'files/d633008/e5land.oper.fc.sfc.{group}/'
    pattern = re.compile(rf'\.{variable}_{param}\.(\d{{10}})-(\d{{10}})\.nc$')
    result = []
    for node in ET.fromstring(content).iter():
        path = node.get('urlPath', '')
        match = pattern.search(path)
        if not path.startswith(prefix) or not match:
            continue
        first, last = [datetime.strptime(s, '%Y%m%d%H').replace(tzinfo=timezone.utc)
                       for s in match.groups()]
        if first <= end and last >= start:
            result.append((path, max(first, start), min(last, end)))
    return sorted(set(result))


def merge_segments(segments):
    """Deduplicate identical boundary records; reject conflicting duplicates."""
    records = {}
    for times, data in segments:
        if len(times) != len(data):
            raise ValueError('Segment data and timestamps differ in length')
        for stamp, field in zip(times, data):
            if not np.isfinite(stamp) or stamp % 3600:
                raise ValueError('Expected exact UTC hourly timestamps')
            stamp = int(stamp)
            if stamp in records and not np.array_equal(records[stamp], field, equal_nan=True):
                raise ValueError('Conflicting duplicate timestamp across source segments')
            records[stamp] = field
    times = np.array(sorted(records), dtype=np.int64)
    return times, np.stack([records[t] for t in times])


def request_windows(segments, hours=6):
    """Bound each request duration, retaining shared boundary timestamps."""
    for path, first, last in segments:
        if first == last:
            yield path, first, last
        while first < last:
            stop = min(first+timedelta(hours=hours), last)
            yield path, first, stop
            first = stop


def deaccumulate_precipitation(cumulative_m, times):
    """01Z is forecast step 1; 02Z through next 00Z are successive differences.

    Negative increments below -1e-4 mm are invalid. Smaller negative rounding
    residuals are clipped to zero. Missing predecessors remain NaN.
    """
    data = np.ma.asarray(cumulative_m, dtype=float).filled(np.nan) * 1000
    if len(data) != len(times) or np.any(np.diff(times) <= 0) or np.any(np.asarray(times) % 3600):
        raise ValueError('Precipitation requires matching, unique, increasing UTC hours')
    hourly = np.full(data.shape, np.nan)
    missing_previous = np.zeros(len(times), dtype=bool)
    for i, stamp in enumerate(times):
        hour = datetime.fromtimestamp(int(stamp), timezone.utc).hour
        if hour == 1:
            hourly[i] = data[i]
        elif i and stamp - times[i-1] == 3600:
            hourly[i] = data[i] - data[i-1]
        else:
            missing_previous[i] = True
    negative = hourly < -NEGATIVE_TOLERANCE_MM
    hourly[negative] = np.nan
    hourly[(hourly < 0) & ~negative] = 0
    return hourly, negative, missing_previous


def validate_metadata(field, time, variable):
    _, param, units = FIELDS[variable]
    data_types = {'fc'} if variable == 'tp' else {'an','fc'}
    if (field.units != units or field.GRIB_paramId != param or field.GRIB_dataType not in data_types
            or field.GRIB_stepType != ('accum' if variable == 'tp' else 'instant')
            or field.dimensions != ('valid_time', 'latitude', 'longitude')
            or time.units != 'seconds since 1970-01-01' or time.calendar != 'proleptic_gregorian'):
        raise ValueError(f'Unexpected source metadata for {variable}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--start-date', default='2025-12-29')
    parser.add_argument('--end-date', default='2025-12-31')
    parser.add_argument('--cache-only', action='store_true')
    parser.add_argument('--max-download-mb', type=float, default=50)
    parser.add_argument('--request-hours', type=int, default=6)
    args = parser.parse_args()
    start = datetime.strptime(args.start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)
    if not 0 < (end-start).days <= 7 or not 0 < args.max_download_mb <= 50 or not 1 <= args.request_hours <= 24:
        parser.error('This demo accepts 1–7 days, 1–24 hours per request, and at most 50 MB of new downloads')
    args.output.mkdir(parents=True, exist_ok=True)
    summary_path = args.output/'summary.json'
    summary_path.write_text(json.dumps({'status':'running', 'started_utc':datetime.now(timezone.utc).isoformat()})+'\n')
    for name in ('hourly.csv', 'polygon_weights.json'):
        (args.output/name).unlink(missing_ok=True)
    features = load_features(args.geojson)
    ids = [f['properties'].get('id') for f in features]
    if not features or any(not isinstance(i, str) or not i.strip() or i != i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Catchments require unique nonempty identifiers')
    bounds = subset_query(features)
    forcing = forcing_metadata(features)
    for warning in forcing['warnings']:
        print(warning, flush=True)
    manifest_path = args.output/'requests.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    transferred = 0

    def fetch(name, url, cap=10_000_000):
        nonlocal transferred
        path = args.output/name
        record = manifest.get(name, {})
        if path.exists() and record.get('url') == url and record.get('status') == 200 and record.get('sha256') == sha256(path):
            return path
        if args.cache_only:
            raise ValueError(f'Missing or invalid cached response: {name}')
        cap = min(cap, int(args.max_download_mb*1_000_000)-transferred)
        if cap <= 0:
            raise ValueError('Total download cap reached')
        with tempfile.TemporaryDirectory(prefix='.era5-download-',dir=args.output) as private:
            temporary = Path(private)/'response'
            response = subprocess.run(['curl','--silent','--show-error','--location',
                '--proto','=https','--proto-redir','=https','--connect-timeout','8','--max-time','45',
                '--max-filesize',str(cap),'--output',str(temporary),'--write-out','%{http_code}',url],
                capture_output=True,text=True,timeout=50)
            size = temporary.stat().st_size if temporary.exists() else 0
            transferred += size
            record = dict(url=url,status=int(response.stdout) if response.stdout.isdigit() else 0,
                          curl_exit_code=response.returncode,bytes=size,sha256=None,
                          retrieved_utc=datetime.now(timezone.utc).isoformat())
            if not response.returncode and record['status'] == 200 and size <= cap:
                temporary.replace(path)
                record['sha256'] = sha256(path)
            manifest[name] = record
            manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
            if record['sha256'] is None:
                raise ValueError(f'{name}: HTTP {record["status"]}, curl exit {response.returncode}')
        return path

    months = set()
    cursor = start-timedelta(hours=1)
    while cursor < end:
        months.add(cursor.strftime('%Y%m'))
        cursor = cursor.replace(day=1, hour=0)+timedelta(days=32)
        cursor = cursor.replace(day=1)
    catalogs = {}
    for group in sorted({field[0] for field in FIELDS.values()}):
        for month in sorted(months):
            url = HOST+f'catalog/files/d633008/e5land.oper.fc.sfc.{group}/{month}/catalog.xml'
            catalogs[group, month] = fetch(f'catalog_{group}_{month}.xml', url, 1_000_000).read_bytes()
    arrays, metadata = {}, []
    lat = lon = expected_times = None
    for variable, (group, param, units) in FIELDS.items():
        segments = sorted({segment for month in months
                           for segment in catalog_segments(catalogs[group, month], variable, start, end)})
        if not segments:
            raise ValueError(f'No observed catalog segment covers {variable}')
        parts = []
        for source_path, first, last in request_windows(segments,args.request_hours):
            query = dict(var=variable, **bounds, horizStride=1,
                         time_start=first.strftime('%Y-%m-%dT%H:%M:%SZ'), time_end=last.strftime('%Y-%m-%dT%H:%M:%SZ'),
                         timeStride=1, accept='netcdf')
            url = HOST+'ncss/grid/'+source_path+'?'+urlencode(query)
            name = f'{variable}_{first:%Y%m%d%H}_{last:%Y%m%d%H}.nc'
            path = fetch(name, url)
            with netCDF4.Dataset(path) as ds:
                field, time = ds[variable], ds['valid_time']
                validate_metadata(field,time,variable)
                current_lat = np.asarray(ds['latitude'][:])
                current_lon = (np.asarray(ds['longitude'][:])+180) % 360-180
                if lat is None:
                    lat, lon = current_lat, current_lon
                elif not np.array_equal(lat, current_lat) or not np.array_equal(lon, current_lon):
                    raise ValueError('Grid differs between source files')
                times = np.asarray(time[:], dtype=float)
                data = np.ma.asarray(field[:], dtype=float).filled(np.nan)
                requested = np.arange(first.timestamp(), last.timestamp()+1, 3600)
                if not np.array_equal(times, requested) or data.shape != (len(times),len(lat),len(lon)):
                    raise ValueError('Source subset omits requested timestamps or grid cells')
                parts.append((times, data))
                metadata.append(dict(file=name, source_path=source_path, variable=variable, units=units,
                                     parameter_id=param, step_type=field.GRIB_stepType, data_type=field.GRIB_dataType,
                                     calendar=time.calendar,
                                     first_time_utc=first.isoformat(), last_time_utc=last.isoformat()))
        times, data = merge_segments(parts)
        expected = np.arange(start.timestamp(), end.timestamp()+1, 3600)
        if not np.array_equal(times, expected):
            raise ValueError(f'Missing hourly coverage for {variable}')
        expected_times = times
        arrays[variable] = data
    if not np.allclose(np.diff(lon), .1) or not np.allclose(np.diff(lat), -.1):
        raise ValueError('Expected native regular 0.1-degree grid')
    precipitation, negative, missing_previous = deaccumulate_precipitation(arrays['tp'], expected_times)
    fields = {'precipitation_mm':(precipitation, 'mm', 'hour_ending_amount'),
              'temperature_c':(arrays['t2m']-273.15, 'degC', 'instantaneous'),
              'snow_water_equivalent_mm':(arrays['sd']*1000, 'mm', 'instantaneous')}
    identity = Transformer.from_crs(4326,4326,always_xy=True)
    rows, weight_info = [], []
    for feature in features:
        identifier = feature['properties']['id']
        weights, info = fractional_weights(feature['geometry'], lon, lat, identity)
        weight_info.append(dict(project_id=identifier, **info))
        for variable, (data, units, kind) in fields.items():
            for i, stamp in enumerate(expected_times):
                value, fraction, complete = strict_area_mean(data[i], weights)
                qc = 'valid' if complete else 'spatial_missing'
                if variable == 'precipitation_mm':
                    if missing_previous[i]:
                        qc = 'missing_predecessor'
                    elif np.any(negative[i][weights > 0]):
                        qc = 'negative_accumulation_increment'
                rows.append(dict(source=SOURCE,project_id=identifier,
                                 time_utc=datetime.fromtimestamp(int(stamp),timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                 variable=variable,value=value,units=units,temporal_kind=kind,
                                 valid_area_fraction=fraction,qc=qc))
    rows.sort(key=lambda row:tuple(row[key] for key in ('source','project_id','variable','time_utc')))
    with (args.output/'hourly.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=COLUMNS);writer.writeheader();writer.writerows(rows)
    (args.output/'polygon_weights.json').write_text(json.dumps(weight_info,indent=2)+'\n')
    result = dict(status='bounded_hourly_demo_complete', source=SOURCE, projects=sorted(ids), **forcing,
                  start_date=args.start_date,end_date=args.end_date,raw_hours=len(expected_times),
                  input_sha256=sha256(args.geojson),downloaded_new_bytes=transferred,cache_only=args.cache_only,
                  request_hours=args.request_hours,subset_bounds_0_to_360=bounds,
                  script_sha256=sha256(__file__),
                  grid=dict(crs='EPSG:4326',latitude_centers=[float(lat.min()),float(lat.max())],
                            longitude_centers=[float(lon.min()),float(lon.max())],
                            latitude_count=len(lat),longitude_count=len(lon),spacing_degrees=.1),
                  response_bytes_on_disk=sum((args.output/name).stat().st_size for name in manifest
                                             if (args.output/name).is_file()),
                  rows=len(rows),qc_counts=dict(Counter(row['qc'] for row in rows)),metadata=metadata,
                  hourly_sha256=sha256(args.output/'hourly.csv'),negative_tolerance_mm=NEGATIVE_TOLERANCE_MM,
                  precipitation_method='01Z raw accumulation; 02Z through next 00Z successive differences; < -1e-4 mm invalid, [-1e-4,0) rounded to zero',
                  initial_boundary='Initial 00Z precipitation lacks preceding 23Z and is NaN/missing_predecessor; it belongs to the preceding UTC day',
                  area_method='Fractional native-cell intersection geodesic areas; any missing positive-area cell invalidates polygon mean',
                  snow_definition='Snow water equivalent, not physical snow depth',
                  scope='Bounded public NCAR mirror demo only; no complete 1996–2025 coverage or soil-state claim')
    summary_path.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(f'Saved {len(rows)} hourly records for {len(features)} polygons; {transferred} new bytes.')


if __name__ == '__main__':
    main()
