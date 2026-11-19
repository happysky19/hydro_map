"""Download ECMWF's extended-range ensemble (EC46) from the S2S archive on the ECMWF Data Store.

    python -m catchment_forecast.s2s forecast --start 2026-11-12 --end 2026-11-12 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]]
    python -m catchment_forecast.s2s reforecast --dates 2025-11-03 2025-11-10 --years 2006 2025 --archive ARCHIVE \\
        [--geojson CATCHMENTS --output OUTPUT [--delete]]

ECMWF runs its extended range every day at 00 UTC, a control and 100 members
to 46 days, and every other day reforecasts the same dates of the past 20
years with a control and 10 members. The S2S archive keeps both on a 1.5
degree grid and opens a real-time run 48 hours after its start; under the S2S
licence ECMWF's part is CC BY 4.0, free also for commercial use with credit to
ECMWF. The account's key is read from ~/.cdsapirc, the licence must have been
accepted on the Data Store, and the Data Store runs one request per user and
dataset at a time.

The archive gives daily means of 2 m temperature, dew point and the top 20 cm
soil temperature, 6 h extremes of 2 m temperature, precipitation and the
radiation accumulated from the start, and 10 m wind at 00 UTC each day. One
request takes every variable (VARIABLES) at every 6 h step and as the mean of
every day, cut to the catchment area, and the Data Store returns what each
variable has. A real-time start is one request for the control and the members
together; a reforecast is larger than the Data Store takes at once, so its
control and its members are two requests. They come into ARCHIVE/raw, and the
messages are then sorted into ARCHIVE/YYYYMMDD00/ec46_c00.grib (control) and
ec46_p001.grib ...: a reforecast's folder is the date it starts from, so each
of the 20 years is a start as in the hindcasts of the other systems. With
--geojson and --output each start is aggregated into OUTPUT/ec46, and --delete
then removes its GRIB files. Requests wait in a queue that all users of ECMWF's
archive share, an hour or more each.
A day done is marked in ARCHIVE/done and skipped on a rerun; a request that
fails is reported and its day skipped, and a rerun picks up the files there.
"""

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import sys
import time

from .tigge import client, retrieve, say


URL = 'https://ecds.ecmwf.int/api'
# N, W, S, E: the catchments (41.1-52.9 N, 122.8-109.8 W) with a margin. The Data Store keeps the points of
# its own grid inside the box, so the 1.5-degree points start at 42 N for a box from 41 N: 39 N leaves a cell.
AREA = [59, -127, 39, -108]
HOURS = 1104
# Every variable at every 6 h step and as the mean of every day: the Data Store returns the daily means of 2 m
# temperature, dew point and soil temperature, the 6 h extremes, and the other fields at their steps.
VARIABLES = ('2_m_temperature', '2_m_dewpoint_temperature', 'soil_temperature_top_20_cm',
             'maximum_2_m_temperature_in_the_last_6_hours', 'minimum_2_m_temperature_in_the_last_6_hours',
             'total_precipitation', 'surface_solar_radiation_downwards', 'surface_net_solar_radiation',
             'surface_net_thermal_radiation', '10_m_u_component_of_wind', '10_m_v_component_of_wind')
TYPES = {'cf': 'control_forecast', 'pf': 'perturbed_forecast'}


def leadtimes(hours=HOURS):
    return [str(hour) for hour in range(0, hours + 1, 6)] + [f'{hour}_{hour + 24}' for hour in range(0, hours, 24)]


def parts(years=None):
    """The forecast types of each request: all in one for a real-time start, one each for a reforecast."""
    return [list(TYPES)] if years is None else [[kind] for kind in TYPES]


def body(day, kinds, years=None):
    """A request for one start day (the model date of a reforecast, with years the past years it covers)."""
    request = dict(origin='ecmwf', year=f'{day:%Y}', month=f'{day:%m}', day=f'{day:%d}', time='00:00',
                   level_type='single_level', variable=list(VARIABLES), forecast_type=[TYPES[k] for k in kinds],
                   leadtime_hour=leadtimes(), area=AREA, data_format='grib')
    if years is not None:
        request.update(hyear=[str(year) for year in years], hmonth=f'{day:%m}', hday=f'{day:%d}')
    return request


def split(raw_files, archive, prefix='ec46'):
    """Sort the messages of raw_files into ARCHIVE/YYYYMMDDHH/PREFIX_MEMBER.grib by the date and time they start
    from and their member (number 0, the control, is c00; number n is p00n); returns the start folders."""
    import eccodes
    streams = {}
    try:
        for raw in raw_files:
            with open(raw, 'rb') as stream:
                while (handle := eccodes.codes_grib_new_from_file(stream)) is not None:
                    try:
                        number = eccodes.codes_get(handle, 'number') if eccodes.codes_is_defined(handle, 'number') \
                            else 0
                        start = f'{eccodes.codes_get(handle, "dataDate"):08d}' \
                                f'{eccodes.codes_get(handle, "dataTime") // 100:02d}'
                        member = 'c00' if number == 0 else f'p{number:03d}'
                        if (start, member) not in streams:
                            (archive / start).mkdir(parents=True, exist_ok=True)
                            streams[start, member] = (archive / start / f'{prefix}_{member}.part').open('wb')
                        streams[start, member].write(eccodes.codes_get_message(handle))
                    finally:
                        eccodes.codes_release(handle)
    finally:
        for stream in streams.values():
            stream.close()
    for start, member in streams:
        (archive / start / f'{prefix}_{member}.part').replace(archive / start / f'{prefix}_{member}.grib')
    return sorted({start for start, _ in streams})


def fetch_day(api, day, archive, dataset, years=None):
    """The raw files of one start day (or model date); None when a request failed."""
    raw = archive / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    files = []
    for kinds in parts(years):
        target = raw / f'{request_key(day, years)}_{"_".join(kinds)}.grib'
        if not target.exists():
            begin = time.time()
            if not retrieve(api, body(day, kinds, years), target, attempts=3, wait=300, dataset=dataset):
                return None
            say(f'{target.name}: {target.stat().st_size / 1e6:.1f} MB in {(time.time() - begin) / 60:.1f} min')
        files.append(target)
    return files


def request_key(day, years=None):
    """Keep different reforecast year selections out of each other's raw files and completion records."""
    return f'{day:%Y%m%d}' + (f'_{years[0]}-{years[-1]}' if years is not None else '')


def run(days, archive, dataset, years=None, catchments=None, output=None, delete=False):
    from .aggregate import run_start
    api = client(URL)
    done = archive / 'done'
    done.mkdir(parents=True, exist_ok=True)
    for day in days:
        marker = done / request_key(day, years)
        starts, files = [], []
        if marker.exists():
            content = marker.read_text()
            starts = json.loads(content) if content else []
            if output is None or (starts and all((output / f'{start}.parquet').exists() for start in starts)):
                continue
            if any(not (output / f'{start}.parquet').exists()
                   and not list((archive / start).glob('ec46_*.grib')) for start in starts):
                starts = []
        if not starts:
            files = fetch_day(api, day, archive, dataset, years)
            if files is None:
                say(f'{day}: skipped, a request failed')
                continue
            starts = split(files, archive)
            if not starts:
                raise ValueError(f'{day}: the response has no GRIB messages')
            say(f'{day}: {len(starts)} start{"s" if len(starts) > 1 else ""} from {starts[0]} to {starts[-1]}')
        if output is not None:
            for start in starts:
                if not (output / f'{start}.parquet').exists():
                    run_start('ec46', archive / start, catchments, output)
                if delete and (output / f'{start}.parquet').exists() and (archive / start).exists():
                    shutil.rmtree(archive / start)
        for path in files:
            path.unlink()
        partial = marker.with_suffix('.part')
        partial.write_text(json.dumps(starts))
        partial.replace(marker)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    forecast = commands.add_parser('forecast', help='Real-time runs, 48 h after their start')
    forecast.add_argument('--start', required=True, type=date.fromisoformat, help='First start date')
    forecast.add_argument('--end', required=True, type=date.fromisoformat, help='Last start date')
    reforecast = commands.add_parser('reforecast', help='Reforecasts of the given model dates')
    reforecast.add_argument('--dates', required=True, nargs='+', type=date.fromisoformat,
                            help='Model dates (reforecasts run on every other day)')
    reforecast.add_argument('--years', required=True, nargs=2, type=int, metavar=('FIRST', 'LAST'),
                            help='The past years to take (the archive holds the 20 before each model date)')
    for command in (forecast, reforecast):
        command.add_argument('--archive', required=True, type=Path, help='Folder of start folders YYYYMMDDHH')
        command.add_argument('--geojson', type=Path, help='Catchment polygons: aggregate each start')
        command.add_argument('--output', type=Path, help='Folder whose ec46 subfolder receives the tables')
        command.add_argument('--delete', action='store_true', help='Remove a start\'s GRIB files once aggregated')
    args = parser.parse_args(argv)
    if (args.geojson is None) != (args.output is None) or (args.delete and args.output is None):
        parser.error('--geojson and --output go together, and --delete needs them')
    catchments = output = None
    if args.output is not None:
        from .aggregate import Catchments
        catchments, output = Catchments(args.geojson), args.output / 'ec46'
        output.mkdir(parents=True, exist_ok=True)
    if args.command == 'forecast':
        days = [args.start + timedelta(days=n) for n in range((args.end - args.start).days + 1)]
        say(f'{len(days)} EC46 starts into {args.archive}')
        run(days, args.archive, 's2s-forecasts', None, catchments, output, args.delete)
    else:
        years = range(args.years[0], args.years[1] + 1)
        say(f'EC46 reforecasts of {len(args.dates)} model dates, {len(years)} years each, into {args.archive}')
        run(args.dates, args.archive, 's2s-reforecasts', years, catchments, output, args.delete)
    return 0


if __name__ == '__main__':
    sys.exit(main())
