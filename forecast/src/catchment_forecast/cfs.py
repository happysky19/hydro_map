"""Download CFSv2 forecasts from NOAA's archive on AWS, only the fields the catchment tables use.

    python -m catchment_forecast.cfs --start 2024-01-01 --end 2024-01-31 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]] [--every-day] [--cycles 0 6 12 18] [--hours 4392] \\
        [--source ADDRESS]

CFSv2 runs four times a day; member 01 of each run goes to about nine months
on a T126 Gaussian grid (about 0.94 degrees). The archive keeps every run from
April 2023 as time series, one file per variable holding every 6 h step, with a
GRIB index beside it. Of each variable in VARIABLES, the steps to --hours (default
4392, 183 days) are one byte range from the start of its file, about 0.4 GB a
run in all, written to ARCHIVE/YYYYMMDDHH/cfs_01.grib2.

The starts are those of the AI hindcasts (days 1, 5, ..., 29) or, with
--every-day, every day, at each of --cycles (UTC hours, default 0). A 06, 12 or
18 UTC run's first lead day is the next whole UTC day (see aggregate), so a
lagged ensemble of the four runs of each day keeps the UTC days of the history
tables. With --geojson and --output each run is aggregated into OUTPUT/cfs as soon
as it is down, and with --delete its GRIB file is then removed. A run whose files
are not all there, or not yet to --hours, is reported and skipped.
"""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import shutil
import sys
import time

from .gefs import fetch, starts


BUCKET = 'https://noaa-cfs-pds.s3.amazonaws.com'
# The time series used: 2 m temperature, its 6 h extremes, specific humidity and surface pressure (humidity),
# 10 m wind (u and v in one file), the precipitation rate, the four radiation fluxes and the top soil layer.
VARIABLES = ('tmp2m', 'tmax', 'tmin', 'q2m', 'pressfc', 'wnd10m', 'prate', 'dswsfc', 'uswsfc', 'dlwsfc', 'ulwsfc',
             'soilt1')
MEMBER = '01'
HOURS = 4392


def url(start, variable, member=MEMBER):
    return (f'{BUCKET}/cfs.{start:%Y%m%d}/{start:%H}/time_grib_{member}/'
            f'{variable}.{member}.{start:%Y%m%d%H}.daily.grb2')


def step_ranges(index, hours):
    """Byte ranges (first, last or None for the end) of the messages up to hours in a time series' GRIB index,
    consecutive ones together (wnd10m holds every step of u, then every step of v); each field in the file must
    reach hours."""
    lines = [line.split(':') for line in index.splitlines() if line.strip()]
    offsets = [int(line[1]) for line in lines] + [None]
    step = lambda line: int(line[5].split()[0]) if line[5].endswith('hour fcst') else -1
    for field in {line[3] for line in lines}:
        reach = max(step(line) for line in lines if line[3] == field)
        if reach < hours:
            raise ValueError(f'{field} reaches {reach} h, not {hours}')
    merged = []
    for i, line in enumerate(lines):
        if 0 <= step(line) <= hours:
            if merged and merged[-1][1] == offsets[i] - 1:
                merged[-1] = (merged[-1][0], None if offsets[i + 1] is None else offsets[i + 1] - 1)
            else:
                merged.append((offsets[i], None if offsets[i + 1] is None else offsets[i + 1] - 1))
    return merged


def download_run(start, folder, hours=HOURS, member=MEMBER):
    """ARCHIVE/YYYYMMDDHH/cfs_MEMBER.grib2 with every variable to hours; returns its size (0 if it was there)."""
    path = folder / f'cfs_{member}.grib2'
    if path.exists():
        return 0
    parts = []
    for variable in VARIABLES:
        address = url(start, variable, member)
        for first, last in step_ranges(fetch(address + '.idx').decode(), hours):
            data = fetch(address, first, last)
            if data[:4] != b'GRIB' or (last is not None and len(data) != last - first + 1):
                raise ValueError(f'{address}: bytes {first}-{last} are not whole GRIB messages')
            parts.append(data)
    partial = path.with_suffix('.part')
    with partial.open('wb') as stream:
        for data in parts:
            stream.write(data)
    partial.replace(path)
    return path.stat().st_size


def runs(first, last, cycles, every_day=False):
    return [datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            for day in starts(first, last, every_day) for hour in cycles]


def run(starts_, archive, hours=HOURS, catchments=None, output=None, delete=False):
    from .aggregate import run_start
    for start in starts_:
        folder = archive / f'{start:%Y%m%d%H}'
        table = None if output is None else output / f'{folder.name}.parquet'
        if table is not None and table.exists():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        begin = time.time()
        try:
            size = download_run(start, folder, hours)
        except (FileNotFoundError, ValueError) as error:
            print(f'{folder.name}: skipped, {error}', flush=True)
            if not any(folder.iterdir()):
                folder.rmdir()
            continue
        if size:
            print(f'{folder.name}: {size / 1e6:.0f} MB in {time.time() - begin:.0f} s', flush=True)
        if output is not None:
            run_start('cfs', folder, catchments, output)
            if delete and table.exists():
                shutil.rmtree(folder)


def main(argv=None):
    global BUCKET
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', required=True, type=date.fromisoformat, help='First start date')
    parser.add_argument('--end', required=True, type=date.fromisoformat, help='Last start date')
    parser.add_argument('--archive', required=True, type=Path, help='Folder of run folders YYYYMMDDHH')
    parser.add_argument('--geojson', type=Path, help='Catchment polygons: aggregate each run when downloaded')
    parser.add_argument('--output', type=Path, help='Folder whose cfs subfolder receives the tables')
    parser.add_argument('--delete', action='store_true', help='Remove a run\'s GRIB file once its table exists')
    parser.add_argument('--every-day', action='store_true', help='Every day, not only the AI hindcast days')
    parser.add_argument('--cycles', type=int, nargs='+', default=[0], choices=[0, 6, 12, 18],
                        help='Runs of each day, UTC hours (default 0)')
    parser.add_argument('--hours', type=int, default=HOURS, help=f'Last step (default {HOURS})')
    parser.add_argument('--source', default=BUCKET, help=f'Address or folder holding cfs.YYYYMMDD/ (default {BUCKET})')
    args = parser.parse_args(argv)
    if (args.geojson is None) != (args.output is None) or (args.delete and args.output is None):
        parser.error('--geojson and --output go together, and --delete needs them')
    if args.hours < 48 or args.hours % 6:
        parser.error('--hours must be a multiple of 6 from 48')
    catchments = output = None
    if args.output is not None:
        from .aggregate import Catchments
        catchments, output = Catchments(args.geojson), args.output / 'cfs'
        output.mkdir(parents=True, exist_ok=True)
    BUCKET = args.source.rstrip('/')
    starts_ = runs(args.start, args.end, sorted(args.cycles), args.every_day)
    print(f'{len(starts_)} CFSv2 runs to {args.hours} h from {BUCKET} into {args.archive}', flush=True)
    run(starts_, args.archive, args.hours, catchments, output, args.delete)
    return 0


if __name__ == '__main__':
    sys.exit(main())
