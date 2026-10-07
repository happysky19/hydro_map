"""Check the DestinE Earth Data Hub ERA5-Land store before a long run.

    python research/check_edh.py                 # layout, units and hourly values at one point
    python research/check_edh.py --compare-cds   # also compare one small request with the CDS
    python research/check_edh.py --compare-cds --geojson outputs/projects43_independent/dam_catchments.geojson
    python research/check_edh.py --scan          # blanks in one land chunk per year since 1996
    python research/check_edh.py --field str     # when and where one field is blank on land

Reads the personal access token from ~/.netrc
(machine data.earthdatahub.destine.eu password <token>); the token is never
printed. The daily pipeline expects accumulated fields as totals since 00 UTC,
so that the 00 UTC value is the previous day's 24-hour total, as in the CDS; the
hourly values at one point show whether the store follows that convention.
--compare-cds requests the same day and area from both providers and compares
every value through the pipeline's own reader; with --geojson the area is the
one a run over those catchments requests. Fields the store lacks (FROM_CDS) are
read from the CDS in runs and are not compared. --scan looks for gaps in the
store over the period of a long run.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import time

import numpy as np

from cds_fields import LAND_ACCUMULATED, LAND_STATES
from download_cds import _units_match, accumulated_requests, make_requests, padded_area, read_response
from edh_era5_land import EPOCH, FROM_CDS, EdhClient
from hydro_map.plotting import load_features


POINT = (47.5, -123.5)        # Olympic Mountains: rain and a clear daily solar cycle
DAY = date(2024, 11, 19)
SCAN_POINT = (48.0, -117.0)   # inside the 43 catchments' area; its 5 x 10 degree chunk is land
GAP_TIME = datetime(2024, 11, 20, tzinfo=timezone.utc)   # str blank over the catchments, CDS has it
REGIONS = {'catchments': SCAN_POINT, 'US Midwest': (40.0, -90.0), 'Canada': (55.0, -105.0),
           'Europe': (50.0, 10.0), 'Russia': (55.0, 40.0), 'India': (25.0, 80.0), 'China': (45.0, 115.0),
           'Amazon': (-10.0, -60.0), 'Africa': (0.0, 20.0), 'Australia': (-25.0, 135.0)}
TOLERANCE = 2e-3              # of each field's largest magnitude; both copies are packed differently
LAND = {**LAND_STATES, **LAND_ACCUMULATED}


def layout(client):
    client._load_metadata()
    valid_time, latitude, longitude = (client.coordinates[name] for name in ('valid_time', 'latitude', 'longitude'))
    first, last = (EPOCH + timedelta(hours=int(hour)) for hour in valid_time[[0, -1]])
    print(f'time axis {first:%Y-%m-%d %H} to {last:%Y-%m-%d %H} UTC, {len(valid_time)} hours')
    print(f'grid latitude {latitude[0]:g} to {latitude[-1]:g}, longitude {longitude[0]:g} to {longitude[-1]:g}')
    print('chunk keys', client.arrays['t2m'].get('chunk_key_encoding', 'default'))
    problems = []
    for short, (_, expected) in LAND.items():
        meta = client.arrays.get(short)
        if meta is None:
            problems.append(f'{short} missing'); print(f'{short:6} MISSING'); continue
        attributes = meta.get('attributes', {})
        units, step = attributes.get('units', '?'), attributes.get('GRIB_stepType', '?')
        expected_step = 'accum' if short in LAND_ACCUMULATED else 'instant'
        ok = _units_match(units, expected) and step == expected_step
        if not ok:
            problems.append(f'{short} units {units!r} step {step!r}')
        chunks = 'x'.join(map(str, meta['chunk_grid']['configuration']['chunk_shape']))
        codecs = '+'.join(codec['name'] for codec in meta['codecs'])
        print(f'{short:6} {units:22} {step:8} {chunks:12} {codecs:40} {"ok" if ok else "MISMATCH"}')
    return problems


def point_series(client):
    """Hourly values from 01 UTC on DAY to 03 UTC the next day at POINT."""
    latitude, longitude = client.coordinates['latitude'], client.coordinates['longitude']
    row = int(np.argmin(np.abs(latitude - POINT[0])))
    column = int(np.argmin(np.abs((longitude + 180) % 360 - 180 - POINT[1])))
    midnight = datetime(DAY.year, DAY.month, DAY.day, tzinfo=timezone.utc)
    stamps = [midnight + timedelta(hours=hour) for hour in range(1, 28)]
    positions = client._positions(stamps)
    started = time.monotonic()
    values = {short: client.series(short, positions, np.array([row]), np.array([column]))[:, 0, 0]
              for short in ('t2m', 'tp', 'sf', 'e', 'ssrd')}
    seconds = time.monotonic() - started
    sizes = [path.stat().st_size for path in client.chunk_dir.iterdir()]
    print(f'\nhourly values at {latitude[row]:.1f}, {(longitude[column] + 180) % 360 - 180:.1f}')
    print('UTC            t2m degC   tp mm   sf mm    e mm  ssrd MJ/m2')
    for i, stamp in enumerate(stamps):
        print(f'{stamp:%Y-%m-%d %H}  {values["t2m"][i] - 273.15:8.2f} {values["tp"][i] * 1e3:7.2f} '
              f'{values["sf"][i] * 1e3:7.2f} {values["e"][i] * 1e3:7.2f} {values["ssrd"][i] / 1e6:10.2f}')
    print(f'{len(sizes)} chunks, {np.mean(sizes) / 1e6:.1f} MB each, {seconds / len(sizes):.1f} s each one at a time')
    # 01 UTC to 00 UTC next day is stamps[0:24]; the following 01 UTC is stamps[24].
    problems = []
    for short in ('tp', 'ssrd'):
        if np.isnan(values[short]).any():
            problems.append(f'{short} has blanks at the point'); continue
        day, scale = values[short][:24], np.max(np.abs(values[short][:24])) or 1
        rising = np.all(np.diff(day) >= -1e-3 * scale)
        restarts = values[short][24] < day[-1] or day[-1] == 0
        if not (rising and restarts):
            problems.append(f'{short} is not a total since 00 UTC')
    print('accumulated fields:', 'totals since 00 UTC, as the pipeline expects' if not problems else
          'NOT totals since 00 UTC; do not use --land-source edh')
    return problems


def catchment_cells(features, latitude, longitude):
    """Grid cells of 0.1 degree that touch any catchment."""
    import shapely
    union = shapely.union_all([feature['geometry'] for feature in features])
    shapely.prepare(union)
    xx, yy = np.meshgrid(longitude, latitude)
    return shapely.intersects(union, shapely.box(xx - .05, yy - .05, xx + .05, yy + .05))


def compare_cds(client, folder, geojson=None):
    """Compare one day from both providers, over a small box or the catchments' own area.

    Values are compared where both are defined; cells blank in only one source are
    listed, and with catchments they fail the check only when they touch one.
    """
    import cdsapi
    features = load_features(geojson) if geojson else None
    area = (padded_area(features, .1) if features else
            [POINT[0] + .3, POINT[1] - .3, POINT[0] - .3, POINT[1] + .3])
    specs = make_requests('era5-land', DAY, area, DAY) + accumulated_requests(DAY, DAY, area, FROM_CDS)
    print(f'\n{", ".join(FROM_CDS)}: read from the CDS in runs, not compared')
    specs = [spec for spec in specs if spec.get('provider') != 'cds']
    providers = {'CDS': cdsapi.Client(progress=False), 'EDH': client}
    problems = []
    for spec in specs:
        responses = {}
        for name, provider in providers.items():
            path, started = folder / f"{spec['kind']}_{name}.nc", time.monotonic()
            provider.retrieve(spec['dataset'], spec['request'], str(path))
            responses[name] = read_response(path, spec['fields'])
            print(f"\n{spec['kind']}: {name} answered in {time.monotonic() - started:.0f} s")
        print('field  hours  cells    blank only in EDH / CDS  largest |value|  max |difference| / largest')
        for short in spec['fields']:
            cds, edh = responses['CDS'][short], responses['EDH'][short]
            if not (cds['data'].shape == edh['data'].shape and np.array_equal(cds['times'], edh['times'])
                    and all(np.allclose(cds[a], edh[a], atol=2e-5) for a in ('latitude', 'longitude'))):
                problems.append(f'{spec["kind"]} {short} grids differ'); print(f'{short:6} grids differ'); continue
            a, b = cds['data'], edh['data']
            only_edh, only_cds = np.isnan(b) & ~np.isnan(a), np.isnan(a) & ~np.isnan(b)
            both = ~np.isnan(a) & ~np.isnan(b)
            scale = float(np.abs(a[both]).max()) if both.any() else 0.
            difference = float(np.abs(a - b)[both].max()) / (scale or 1) if both.any() else 0.
            inside = (catchment_cells(features, cds['latitude'], cds['longitude']) if features
                      else np.ones(a.shape[1:], bool))
            relevant = (only_edh | only_cds) & inside
            if difference > TOLERANCE or relevant.any():
                problems.append(f'{spec["kind"]} {short} differs')
            hours, rows, columns = a.shape
            print(f'{short:6} {hours:5} {rows:3}x{columns:<4} {only_edh.sum():12} / {only_cds.sum():<10} '
                  f'{scale:15.4g} {difference:12.2e}')
            for t, r, c in np.argwhere(only_edh | only_cds)[:4]:
                stamp = datetime.fromtimestamp(int(cds['times'][t]), timezone.utc)
                print(f'         {stamp:%Y-%m-%d %H} UTC at {cds["latitude"][r]:.1f}, {cds["longitude"][c]:.1f}: '
                      f'CDS {a[t, r, c]:.4g}, EDH {b[t, r, c]:.4g}'
                      + (', touches a catchment' if features and inside[r, c] else ''))
    if client.absent:
        print('chunks missing from the store:', ', '.join(sorted(client.absent)[:10]))
    return problems


def scan(client, first_year=1996):
    """Blank values in one land chunk, one 60-day chunk per year, for every field.

    Cells blank in every sampled hour of 2 m temperature are water and are ignored.
    Sampled dates move through the year, so every 60-day chunk position is seen.
    """
    latitude, longitude = client.coordinates['latitude'], client.coordinates['longitude']
    row = int(np.argmin(np.abs(latitude - SCAN_POINT[0])))
    column = int(np.argmin(np.abs((longitude + 180) % 360 - 180 - SCAN_POINT[1])))
    last = EPOCH + timedelta(hours=int(client.coordinates['valid_time'][-1]))
    stamps = [stamp for stamp in (datetime(year, 1 + 2 * (year % 6), 15, tzinfo=timezone.utc)
                                  for year in range(first_year, last.year + 1)) if stamp <= last]
    positions = client._positions(stamps)

    def blanks(short):
        """Blank masks of the sampled chunks and their first hours; raw chunks are deleted after use."""
        chunk_t, chunk_y, chunk_x = client.arrays[short]['chunk_grid']['configuration']['chunk_shape']
        indexes = sorted({(int(position // chunk_t), row // chunk_y, column // chunk_x) for position in positions})
        with ThreadPoolExecutor(client.workers) as pool:
            paths = list(pool.map(lambda index: client._fetch(short, index), indexes))
        masks = [np.isnan(client._decode(client.arrays[short], path.read_bytes() or None)) for path in paths]
        client.release()
        valid_time = client.coordinates['valid_time']
        starts = [EPOCH + timedelta(hours=int(valid_time[t * chunk_t])) for t, _, _ in indexes]
        return masks, starts

    started = time.monotonic()
    temperature = blanks('t2m')
    water = np.logical_and.reduce([mask.all(axis=0) for mask in temperature[0]])
    print(f'\nblank values at {SCAN_POINT[0]}, {SCAN_POINT[1]} (5 x 10 degree chunk, {water.mean():.0%} water), '
          f'one 60-day chunk per year from {first_year}')
    problems = []
    for short in LAND:
        masks, starts = temperature if short == 't2m' else blanks(short)
        found = []
        for mask, start in zip(masks, starts):
            gap = mask & ~water
            if gap.any():
                hours = sorted({(start.hour + i) % 24 for i in np.flatnonzero(gap.any(axis=(1, 2)))})
                found.append(f'{start:%Y-%m-%d} {gap.mean():.0%}' + ('' if len(hours) == 24 else f' at {hours} UTC'))
        note = ' (read from the CDS in runs)' if short in FROM_CDS else ''
        print(f'{short:6}', (f'gaps in {len(found)} of {len(masks)} chunks: ' + '; '.join(found[:5])
                             if found else f'no gaps in {len(masks)} chunks') + note)
        if found and short not in FROM_CDS:
            problems.append(f'{short} has gaps in the store')
    print(f'scan took {(time.monotonic() - started) / 60:.0f} min')
    return problems


def inspect(client, short):
    """When and where one field is blank on land: its whole record at the catchments' chunk,
    and ten regions at GAP_TIME. Land is where 2 m temperature is defined."""
    latitude, longitude = client.coordinates['latitude'], client.coordinates['longitude']
    valid_time = client.coordinates['valid_time']
    chunk_t, chunk_y, chunk_x = client.arrays[short]['chunk_grid']['configuration']['chunk_shape']

    def index(point, position):
        row = int(np.argmin(np.abs(latitude - point[0])))
        column = int(np.argmin(np.abs((longitude + 180) % 360 - 180 - point[1])))
        return int(position // chunk_t), row // chunk_y, column // chunk_x

    def blocks(items):
        """Decoded chunks for (field, chunk index) pairs, downloaded in parallel."""
        with ThreadPoolExecutor(client.workers) as pool:
            paths = list(pool.map(lambda item: client._fetch(*item), items))
        for (name, _), path in zip(items, paths):
            yield client._decode(client.arrays[name], path.read_bytes() or None)

    client.verbose = False   # absent chunks are counted below instead
    for name in (short, 'ssr'):
        meta = client.arrays[name]
        print(name, json.dumps({key: meta.get(key) for key in ('data_type', 'fill_value', 'attributes')})[:300])
        print('   ', json.dumps(meta['codecs'])[:300])

    position = int(client._positions([GAP_TIME])[0])
    hour = position % chunk_t
    print(f'\n{short} blank on land at {GAP_TIME:%Y-%m-%d %H} UTC, and hours mostly blank in its 60-day chunk')
    for region, point in REGIONS.items():
        temperature, values = blocks([('t2m', index(point, position)), (short, index(point, position))])
        land = ~np.isnan(temperature[hour])
        if not land.any():
            print(f'{region:11} no land in this chunk'); continue
        shares = np.isnan(values)[:, land].mean(axis=1)
        blank = np.flatnonzero(shares > .5)
        start = EPOCH + timedelta(hours=int(valid_time[position - hour]))
        print(f'{region:11} {shares[hour]:4.0%} of {land.sum():4} land cells; {len(blank):4} of {chunk_t} hours '
              f'mostly blank' + (f', {start + timedelta(hours=int(blank[0])):%Y-%m-%d %H} to '
                                 f'{start + timedelta(hours=int(blank[-1])):%Y-%m-%d %H}' if len(blank) else ''))
    client.release()

    temperature, = blocks([('t2m', index(SCAN_POINT, position))])
    land = ~np.isnan(temperature).any(axis=0)
    _, y, x = index(SCAN_POINT, position)
    items = [(short, (t, y, x)) for t in range(-(-len(valid_time) // chunk_t))]
    started, shares = time.monotonic(), []
    for first in range(0, len(items), 50):
        shares += [np.isnan(block)[:, land].mean(axis=1) for block in blocks(items[first:first + 50])]
        client.release()
    shares = np.concatenate(shares)[:len(valid_time)]
    stamps = np.datetime64('1950-01-01T00', 'h') + valid_time.astype('timedelta64[h]')
    label = lambda i: str(stamps[i])[:13].replace('T', ' ')
    gap = shares > 0
    print(f'\n{short} over its whole record at the catchments ({land.sum()} land cells), '
          f'{len(items)} chunks in {(time.monotonic() - started) / 60:.0f} min')
    absent = sum(key.startswith(f'{short}/') for key in client.absent)
    print(f'{gap.sum()} of {len(gap)} hours have blank land cells, {(shares > .99).sum()} are blank everywhere; '
          f'{absent} chunks absent from the store')
    if gap.any():
        hours_of_day = np.bincount(valid_time[gap] % 24, minlength=24)
        print('by UTC hour:', ' '.join(f'{h:02d}:{n}' for h, n in enumerate(hours_of_day) if n))
        years = np.bincount(stamps[gap].astype('datetime64[Y]').astype(int) + 1970)
        print('by year:', ' '.join(f'{year}:{n}' for year, n in enumerate(years) if n)[:400])
        edges = np.flatnonzero(np.diff(np.r_[0, gap.astype(int), 0]))
        runs = sorted(zip(edges[::2], edges[1::2]), key=lambda run: run[0] - run[1])
        print(f'{len(runs)} blank stretches; longest:')
        for begin, end in runs[:5]:
            print(f'   {label(begin)} to {label(end - 1)} UTC, {end - begin} h, '
                  f'{shares[begin:end].mean():.0%} of land cells')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--compare-cds', action='store_true',
                        help='Also compare one small request with the CDS (needs ~/.cdsapirc)')
    parser.add_argument('--geojson', type=Path,
                        help='Compare over the area of these catchments instead of a small box')
    parser.add_argument('--scan', action='store_true',
                        help='Look for gaps in one land chunk per year since 1996 (about 2.5 GB)')
    parser.add_argument('--field', choices=sorted(LAND),
                        help='Only show when and where this field is blank on land (about 1.5 GB)')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='edh-check-') as folder:
        client = EdhClient(Path(folder) / 'chunks')
        if args.field:
            client._load_metadata()
            inspect(client, args.field)
            return
        problems = layout(client) + point_series(client)
        if args.scan:
            problems += scan(client)
        if args.compare_cds:
            problems += compare_cds(client, Path(folder), args.geojson)
    print('\nRESULT:', 'all checks passed' if not problems else '; '.join(problems))
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
