"""Download ECMWF's seasonal forecasts (SEAS5) from the Copernicus Climate Data Store.

    python -m catchment_forecast.seas5 --first 2026-11 --last 2026-11 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]] [--members 51] [--hours 4416]

SEAS5 starts on the first of each month, 51 members to seven months (25 in the
reforecasts of 1993-2016), on a 1 degree grid; the Data Store releases a start
on the 6th of its month at 12 UTC. Each start is one request for VARIABLES, every
6 h to --hours (default 4416, 184 days), cut to the catchment area. Its messages
are sorted into ARCHIVE/YYYYMM0100/seas5_c00.grib (control) and seas5_p001.grib
through seas5_p050.grib; --members N keeps numbers below N, which is enough for fitting
a mapping and keeps the hindcast small. The key is read from ~/.cdsapirc, and the
licence of the dataset must have been accepted on the Data Store. With --geojson
and --output each start is aggregated into OUTPUT/seas5, and --delete then
removes its GRIB files. A start whose request fails is skipped and the command
ends with status 1; a rerun takes it up.
"""

import argparse
from datetime import date, timedelta
from pathlib import Path
import shutil
import sys
import time

from .s2s import AREA, split
from .tigge import client, retrieve, say


URL = 'https://cds.climate.copernicus.eu/api'
DATASET = 'seasonal-original-single-levels'
SYSTEM = '51'
HOURS = 4416
VARIABLES = ('2m_temperature', '2m_dewpoint_temperature', '10m_u_component_of_wind', '10m_v_component_of_wind',
             'total_precipitation', 'surface_solar_radiation_downwards', 'surface_net_solar_radiation',
             'surface_net_thermal_radiation', 'maximum_2m_temperature_in_the_last_24_hours',
             'minimum_2m_temperature_in_the_last_24_hours', 'soil_temperature_level_1')


def body(month, hours=HOURS):
    return dict(originating_centre='ecmwf', system=SYSTEM, variable=list(VARIABLES), year=[f'{month:%Y}'],
                month=[f'{month:%m}'], day=['01'], leadtime_hour=[str(hour) for hour in range(6, hours + 1, 6)],
                area=AREA, data_format='grib')


def keep_members(raw, members):
    """raw without the members numbered members or above."""
    import eccodes
    kept = raw.with_suffix('.kept')
    with open(raw, 'rb') as source, open(kept, 'wb') as target:
        while (handle := eccodes.codes_grib_new_from_file(source)) is not None:
            if eccodes.codes_get(handle, 'number') < members:
                target.write(eccodes.codes_get_message(handle))
            eccodes.codes_release(handle)
    kept.replace(raw)


def months(first, last):
    out, month = [], first.replace(day=1)
    while month <= last:
        out.append(month)
        month = month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)
    return out


def latest_month(day):
    """Latest monthly start released by day at 00 UTC (CDS releases ECMWF on the 6th at 12 UTC)."""
    first = day.replace(day=1)
    return first if day.day >= 7 else (first - timedelta(days=1)).replace(day=1)


def run(starts, archive, members=None, hours=HOURS, catchments=None, output=None, delete=False):
    """Fetch, sort and aggregate each start; returns the starts skipped because their request failed."""
    from .aggregate import run_start
    api = client(URL)
    raw_folder = archive / 'raw'
    raw_folder.mkdir(parents=True, exist_ok=True)
    skipped = []
    for month in starts:
        name = f'{month:%Y%m}0100'
        if output is not None and (output / f'{name}.parquet').exists():
            continue
        raw = raw_folder / f'{name}.grib'
        if raw.exists() or not list((archive / name).glob('seas5_*.grib')):
            begin = time.time()
            if not raw.exists() and not retrieve(api, body(month, hours), raw, attempts=3, wait=300,
                                                 dataset=DATASET):
                say(f'{name}: skipped, the request failed')
                skipped.append(month)
                continue
            if members:
                keep_members(raw, members)
            if split([raw], archive, 'seas5') != [name]:
                raise ValueError(f'{raw} holds other starts than {name}')
            say(f'{name}: {raw.stat().st_size / 1e6:.0f} MB in {(time.time() - begin) / 60:.1f} min')
            raw.unlink()
        if output is not None:
            run_start('seas5', archive / name, catchments, output)
            if delete and (output / f'{name}.parquet').exists():
                shutil.rmtree(archive / name)
    return skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    month = lambda text: date.fromisoformat(text + '-01')
    parser.add_argument('--first', required=True, type=month, help='First start month, YYYY-MM')
    parser.add_argument('--last', required=True, type=month, help='Last start month, YYYY-MM')
    parser.add_argument('--archive', required=True, type=Path, help='Folder of start folders YYYYMMDDHH')
    parser.add_argument('--geojson', type=Path, help='Catchment polygons: aggregate each start')
    parser.add_argument('--output', type=Path, help='Folder whose seas5 subfolder receives the tables')
    parser.add_argument('--delete', action='store_true', help='Remove a start\'s GRIB files once aggregated')
    parser.add_argument('--members', type=int, help='Keep members numbered below this (default all)')
    parser.add_argument('--hours', type=int, default=HOURS, help=f'Last step (default {HOURS})')
    args = parser.parse_args(argv)
    if (args.geojson is None) != (args.output is None) or (args.delete and args.output is None):
        parser.error('--geojson and --output go together, and --delete needs them')
    if args.hours < 24 or args.hours % 24:
        parser.error('--hours must be a multiple of 24')
    catchments = output = None
    if args.output is not None:
        from .aggregate import Catchments
        catchments, output = Catchments(args.geojson), args.output / 'seas5'
        output.mkdir(parents=True, exist_ok=True)
    starts = months(args.first, args.last)
    say(f'{len(starts)} SEAS5 starts to {args.hours} h into {args.archive}')
    skipped = run(starts, args.archive, args.members, args.hours, catchments, output, args.delete)
    if skipped:
        names = ' '.join(f'{month:%Y-%m}' for month in skipped)
        say(f'{len(skipped)} start{"s" if len(skipped) > 1 else ""} skipped: {names}')
    return 1 if skipped else 0


if __name__ == '__main__':
    sys.exit(main())
