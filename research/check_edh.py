"""Check the DestinE Earth Data Hub ERA5-Land store before a long run.

    python research/check_edh.py                 # layout, units and hourly values at one point
    python research/check_edh.py --compare-cds   # also compare one small request with the CDS

Reads the personal access token from ~/.netrc
(machine data.earthdatahub.destine.eu password <token>); the token is never
printed. The daily pipeline expects accumulated fields as totals since 00 UTC,
so that the 00 UTC value is the previous day's 24-hour total, as in the CDS; the
hourly values at one point show whether the store follows that convention.
--compare-cds requests the same day and area from both providers (one small
CDS request) and compares every value through the pipeline's own reader.
"""

import argparse
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import time

import numpy as np

from cds_fields import LAND_ACCUMULATED, LAND_STATES
from download_cds import _units_match, accumulated_requests, make_requests, read_response
from edh_era5_land import EPOCH, EdhClient


POINT = (47.5, -123.5)        # Olympic Mountains: rain and a clear daily solar cycle
DAY = date(2024, 11, 19)
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


def compare_cds(client, folder):
    import cdsapi
    area = [POINT[0] + .3, POINT[1] - .3, POINT[0] - .3, POINT[1] + .3]
    specs = make_requests('era5-land', DAY, area, DAY) + accumulated_requests(DAY, DAY, area)
    providers = {'CDS': cdsapi.Client(progress=False), 'EDH': client}
    problems = []
    for spec in specs:
        responses = {}
        for name, provider in providers.items():
            path, started = folder / f"{spec['kind']}_{name}.nc", time.monotonic()
            provider.retrieve(spec['dataset'], spec['request'], str(path))
            responses[name] = read_response(path, spec['fields'])
            print(f"\n{spec['kind']}: {name} answered in {time.monotonic() - started:.0f} s")
        print('field  hours  cells  same grid  same blanks  largest |value|  max |difference| / largest')
        for short in spec['fields']:
            cds, edh = responses['CDS'][short], responses['EDH'][short]
            grid = (cds['data'].shape == edh['data'].shape and np.array_equal(cds['times'], edh['times'])
                    and all(np.allclose(cds[a], edh[a], atol=2e-5) for a in ('latitude', 'longitude')))
            blanks = grid and np.array_equal(np.isnan(cds['data']), np.isnan(edh['data']))
            scale = float(np.nanmax(np.abs(cds['data']))) if np.isfinite(cds['data']).any() else 0.
            difference = float(np.nanmax(np.abs(cds['data'] - edh['data']))) / (scale or 1) if blanks else np.nan
            if not (blanks and difference <= TOLERANCE):
                problems.append(f'{spec["kind"]} {short} differs')
            hours, *cells = cds['data'].shape
            print(f'{short:6} {hours:5} {cells[0]}x{cells[1]:<4} {str(grid):10} {str(blanks):12} '
                  f'{scale:15.4g} {difference:12.2e}')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--compare-cds', action='store_true',
                        help='Also compare one small request with the CDS (needs ~/.cdsapirc)')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='edh-check-') as folder:
        client = EdhClient(Path(folder) / 'chunks')
        problems = layout(client) + point_series(client)
        if args.compare_cds:
            problems += compare_cds(client, Path(folder))
    print('\nRESULT:', 'all checks passed' if not problems else '; '.join(problems))
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
