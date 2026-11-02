"""Download ECMWF IFS ENS forecasts from the TIGGE archive on the ECMWF Data Store.

    python -m catchment_forecast.tigge --year 2024 --output DIR

The start dates are those of the AIFS-ENS and WeatherNext 2 hindcasts: 00 UTC
on days 1, 5, ..., 29 of each month, never 29 February (95 a year). Every
start is cropped to the catchment area on the 0.25 degree grid of ERA5, with
steps 0 to 360 h every 6 h. The control forecasts come in one request per
quarter, the 50 perturbed members in one request per start.

The Data Store queues one request per user and dataset at a time, so requests
go one after another. Finished files are kept and skipped on a rerun; a
request the queue refuses is retried. The key is read from ~/.cdsapirc and
never printed. The first control file decides the parameters: those the
archive does not hold are left out of every later request.
"""

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import time


URL = 'https://ecds.ecmwf.int/api'
DATASET = 'tigge-forecasts'
AREA = '59/-127/41/-108'   # N/W/S/E, the catchments with a margin
GRID = '0.25/0.25'
STEPS = '0/to/360/by/6'
MEMBERS = '1/to/50'
# TIGGE parameter codes, by the column of the history table they feed.
PARAMS = {
    '2t': 167,          # tmean_c, relative humidity, vapour pressure deficit
    '2d': 168,          # relative humidity, vapour pressure deficit, rain-snow split
    'mx2t6': 121,       # tmax_c
    'mn2t6': 122,       # tmin_c
    'tp': 228228,       # precipitation_mm, accumulated from the start
    'sf': 228144,       # snowfall_mm, accumulated from the start
    'sd': 228141,       # snow_water_equivalent_mm
    'sm': 228039,       # soil moisture, top 20 cm
    'st': 228139,       # soil temperature, top 20 cm
    'sp': 134,          # humidity from 2t and 2d
    '10u': 165,         # u_wind_ms, wind_speed_ms
    '10v': 166,         # v_wind_ms, wind_speed_ms
    'ssrd': 169,        # shortwave_down_mean_wm2, if the archive holds it
    'ssr': 176,         # net_radiation_mean_wm2
    'str': 177,         # net_radiation_mean_wm2
    'slhf': 147,        # actual_evapotranspiration_mm
    'tcc': 228164,      # cloud_cover_fraction
}
QUEUE_FULL = 'Maximum number queued requests'


def start_dates(year):
    """00 UTC starts on days 1, 5, ..., 29, without 29 February."""
    return [date(year, month, day) for month in range(1, 13) for day in range(1, 30, 4)
            if not (month == 2 and day == 29)]


def request(kind, dates, params):
    """A MARS-style tigge-forecasts request for the control ('cf') or perturbed ('pf') forecasts."""
    body = dict(origin='ecmf', levtype='sfc', expver='prod', type=kind, time='00:00:00', step=STEPS,
                grid=GRID, area=AREA, date=[day.isoformat() for day in dates],
                param='/'.join(str(PARAMS[name]) for name in params), data_format='grib')
    if kind == 'pf':
        body['number'] = MEMBERS
    return body


def plan(year):
    """(file name, kind, dates): the control forecasts by quarter, then each start's perturbed members."""
    days = start_dates(year)
    control = [(f'cf_{year}_q{quarter + 1}.grib', 'cf', [d for d in days if (d.month - 1) // 3 == quarter])
               for quarter in range(4)]
    return control + [(f'pf_{day:%Y%m%d}.grib', 'pf', [day]) for day in days]


def fields(path):
    """{shortName: message count} of a GRIB file, or None without eccodes."""
    try:
        import eccodes
    except ImportError:
        return None
    counts = {}
    with open(path, 'rb') as stream:
        while (handle := eccodes.codes_grib_new_from_file(stream)) is not None:
            name = eccodes.codes_get(handle, 'shortName')
            counts[name] = counts.get(name, 0) + 1
            eccodes.codes_release(handle)
    return counts


def say(text):
    print(f'{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {text}', flush=True)


def client():
    import cdsapi
    rc = Path.home() / '.cdsapirc'
    key = next(line.split(':', 1)[1].strip() for line in rc.read_text().splitlines() if line.startswith('key'))
    return cdsapi.Client(url=URL, key=key, quiet=True, progress=False, retry_max=5, sleep_max=120, timeout=1800)


def retrieve(api, body, target, attempts=4, wait=600):
    """Write one request to TARGET through a temporary file; retry a full queue and other failures."""
    partial = target.with_suffix('.part')
    for attempt in range(1, attempts + 1):
        begin = time.time()
        try:
            api.retrieve(DATASET, body, str(partial))
            partial.replace(target)
            return True
        except Exception as error:   # the Data Store reports every failure as an exception
            text = ' '.join(str(error).split())[:300] or type(error).__name__
            say(f'{target.name}: attempt {attempt} failed after {time.time() - begin:.0f} s: {text}')
            if QUEUE_FULL in str(error) or attempt < attempts:
                time.sleep(wait)
    partial.unlink(missing_ok=True)
    return False


def run(year, output, only=None):
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'params.json'
    params = json.loads(state_path.read_text()) if state_path.exists() else list(PARAMS)
    api, failed = client(), []
    jobs = [job for job in plan(year) if only is None or job[1] == only]
    say(f'{len(jobs)} requests for {year}; parameters {", ".join(params)}; files in {output}')
    for name, kind, dates in jobs:
        target = output / name
        if target.exists():
            continue
        begin = time.time()
        if not retrieve(api, request(kind, dates, params), target):
            failed.append(name)
            continue
        counts = fields(target)
        say(f'{name}: {target.stat().st_size / 1e6:.0f} MB in {(time.time() - begin) / 60:.0f} min'
            + (f'; {sum(counts.values())} fields' if counts else ''))
        if counts is not None and not state_path.exists():
            # The archive leaves out what it does not hold; ask only for what the first file has.
            held = [name for name in params if name in counts]
            missing = sorted(set(params) - set(held))
            say('parameters held: ' + ', '.join(held) + ('; not held: ' + ', '.join(missing) if missing else ''))
            params = held
            state_path.write_text(json.dumps(params))
    say('done' + (f'; failed: {", ".join(failed)}' if failed else ''))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Folder for the GRIB files')
    parser.add_argument('--only', choices=['cf', 'pf'], help='Control or perturbed forecasts only')
    args = parser.parse_args(argv)
    return run(args.year, args.output, args.only)


if __name__ == '__main__':
    sys.exit(main())
