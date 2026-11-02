"""Daily catchment means of AI ensemble forecasts, one table per start, one row per member.

    python -m catchment_forecast.aggregate --system aifs --archive ROOT --geojson CATCHMENTS \\
        --output DIR [--start 2024-01-01 --end 2024-12-31]

ROOT holds one folder per start (YYYYMMDDHH) with one GRIB file per member.
Each start becomes DIR/SYSTEM/YYYYMMDDHH.parquet with init_time, member,
lead_day, valid_date, project_id and the history table's column names. A start
already written is skipped, so a rerun picks up where an earlier one stopped.

Nothing depends on the number of members: every file found is one member,
named from its file name, and each member gets its own complete rows.

Lead day d covers hours 24(d-1) to 24d after the start, which for a 00 UTC
start is a UTC day as in the history tables. Instantaneous fields are sampled
at hours 0, 6, 12 and 18 of the day (WeatherNext 2 has no step 0, so its first
day has three samples); tmean_c is their mean and tmax_c and tmin_c their
extremes, so the daily range is narrower than from hourly data until the
values are mapped onto the history. Wind speed, relative humidity and vapour
pressure deficit are computed at each grid point before the area mean, as in
the history tables. Amounts are summed over the day at each grid point:
AIFS accumulates from the start, so the totals at the ends of the day are
differenced; WeatherNext 2 gives 6 h amounts, which are added. Negative amounts
from GRIB packing become 0, and units follow each field's GRIB units. PET is
Hargreaves from the daily temperatures at the catchment centroid latitude.
Rainfall, snowfall and every other history column come from the adjustment,
which needs the history tables.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import time

import numpy as np

from catchment_history.catchments import read_catchments
from catchment_history.meteorology import hargreaves_pet, saturation_vapor_pressure_kpa

from .grids import area_mean, reduced_gaussian_cells, regular_cells, weights


def aifs_member(name):
    """aifs_f000-f528_p07.grib1 (hindcast) or aifs_p07_N320.grib (operational) -> p07."""
    return 'p' + re.search(r'_p(\d+)', name)[1]


def wn2_member(name):
    """predictions_hres_ckpt2.grib (seed 0) -> ckpt2_s000; predictions_hres_s003_ckpt2.grib -> ckpt2_s003."""
    match = re.search(r'(?:_s(\d+))?_ckpt(\d+)', name)
    return f'ckpt{match[2]}_s{int(match[1] or 0):03d}'


# amounts: {shortName: (column, accumulated from the start or not)}.
SYSTEMS = {
    'aifs': dict(pattern='aifs_*p[0-9][0-9]*.grib*', member=aifs_member, first_step=0,
                 amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_wm2', True)}),
    'wn2': dict(pattern='predictions_hres_*.grib', member=wn2_member, first_step=6,
                amounts={'tp': ('precipitation_mm', False)}),
}
INSTANT = ('2t', '2d', '10u', '10v')
SAMPLE_HOURS = (0, 6, 12, 18)
# Factor from a day's amount in the field's GRIB units to the column's units.
DAILY = {'m': 1000., 'kg m**-2': 1., 'J m**-2': 1 / 86400}
COLUMNS = ['precipitation_mm', 'tmean_c', 'tmax_c', 'tmin_c', 'relative_humidity_pct',
           'vapor_pressure_deficit_kpa', 'shortwave_down_mean_wm2', 'u_wind_ms', 'v_wind_ms', 'wind_speed_ms',
           'pet_hargreaves_mm']


class Catchments:
    """Catchment ids, centroid latitudes and their weights on every grid met so far."""

    def __init__(self, geojson):
        features = read_catchments(geojson)
        self.ids = [feature['properties']['id'] for feature in features]
        self.geometries = [feature['geometry'] for feature in features]
        self.latitudes = [geometry.centroid.y for geometry in self.geometries]
        self.grids = {}

    def on(self, key, cells):
        """(points used, catchment-by-point area matrix) of the grid KEY; cells() describes it."""
        if key not in self.grids:
            self.grids[key] = weights(self.geometries, cells())
        return self.grids[key]


def grid_weights(eccodes, handle, catchments):
    get = lambda key: eccodes.codes_get(handle, key)
    kind = get('gridType')
    if kind == 'reduced_gg':
        rows = np.sort(eccodes.codes_get_array(handle, 'distinctLatitudes'))[::-1]
        counts = eccodes.codes_get_array(handle, 'pl')
        return catchments.on((kind, len(rows), int(counts.sum())), lambda: reduced_gaussian_cells(rows, counts))
    if kind == 'regular_ll':
        if get('iScansNegatively') or get('jPointsAreConsecutive'):
            raise ValueError('Regular grids must run west to east along rows')
        axis = lambda first, last, count: np.linspace(get(first + 'GridPointInDegrees'),
                                                      get(last + 'GridPointInDegrees'), get(count))
        latitudes = axis('latitudeOfFirst', 'latitudeOfLast', 'Nj')
        longitudes = axis('longitudeOfFirst', 'longitudeOfLast', 'Ni')
        key = (kind, latitudes[0], latitudes[-1], len(latitudes), longitudes[0], longitudes[-1], len(longitudes))
        return catchments.on(key, lambda: regular_cells(latitudes, longitudes))
    raise ValueError(f'Unsupported grid {kind}')


def read_member(path, names, catchments):
    """({(shortName, end step in hours): values at the used points}, {shortName: units}, area matrix)."""
    import eccodes
    fields, units, grid = {}, {}, None
    with open(path, 'rb') as stream:
        while (handle := eccodes.codes_grib_new_from_file(stream)) is not None:
            try:
                name = eccodes.codes_get(handle, 'shortName')
                if name not in names:
                    continue
                grid = grid or grid_weights(eccodes, handle, catchments)
                eccodes.codes_set(handle, 'stepUnits', 'h')
                values = eccodes.codes_get_values(handle)
                if eccodes.codes_get(handle, 'bitmapPresent'):
                    values[values == eccodes.codes_get(handle, 'missingValue')] = np.nan
                fields[name, int(eccodes.codes_get(handle, 'endStep'))] = values[grid[0]]
                units[name] = eccodes.codes_get(handle, 'units')
            finally:
                eccodes.codes_release(handle)
    if grid is None:
        raise ValueError(f'{path.name}: none of {", ".join(sorted(names))}')
    if units.get('2t', 'K') != 'K':
        raise ValueError(f'{path.name}: 2t in {units["2t"]}, not K')
    return fields, units, grid[1]


def daily(fields, units, system, matrix, start, latitudes):
    """{column: lead days x catchments} from one member's point fields {(shortName, step): values}."""
    spec = SYSTEMS[system]
    mean = lambda values: area_mean(matrix, values)
    days = max(step for _, step in fields) // 24
    blank = np.full(matrix.shape[0], np.nan)
    # Catchment means at each step, derived quantities first computed at each grid point.
    at = {}
    for (name, step), kelvin in fields.items():
        if name != '2t':
            continue
        t = kelvin - 273.15
        at['tmean_c', step] = mean(t)
        if ('10u', step) in fields and ('10v', step) in fields:
            u, v = fields['10u', step], fields['10v', step]
            at['u_wind_ms', step], at['v_wind_ms', step] = mean(u), mean(v)
            at['wind_speed_ms', step] = mean(np.hypot(u, v))
        if ('2d', step) in fields:
            saturation = saturation_vapor_pressure_kpa(t)
            vapour = saturation_vapor_pressure_kpa(fields['2d', step] - 273.15)
            at['relative_humidity_pct', step] = mean(100 * vapour / saturation)
            at['vapor_pressure_deficit_kpa', step] = mean(np.maximum(saturation - vapour, 0.))

    def samples(column, day):
        steps = [24 * (day - 1) + hour for hour in SAMPLE_HOURS if 24 * (day - 1) + hour >= spec['first_step']]
        values = [at.get((column, step)) for step in steps]
        if any(value is None for value in values):
            return np.full((len(steps), len(blank)), np.nan)
        return np.array(values)

    out = {}
    for column in {column for column, _ in at}:
        by_day = [samples(column, day) for day in range(1, days + 1)]
        out[column] = np.array([values.mean(axis=0) for values in by_day])
        if column == 'tmean_c':
            out['tmax_c'] = np.array([values.max(axis=0) for values in by_day])
            out['tmin_c'] = np.array([values.min(axis=0) for values in by_day])

    def amount(name, accumulated, day):
        """The day's amount at each grid point, or None when a step is missing."""
        if accumulated:
            end, begin = fields.get((name, 24 * day)), 0. if day == 1 else fields.get((name, 24 * (day - 1)))
            return None if end is None or begin is None else end - begin
        parts = [fields.get((name, step)) for step in range(24 * day - 18, 24 * day + 1, 6)]
        return None if any(part is None for part in parts) else sum(np.maximum(part, 0.) for part in parts)

    for name, (column, accumulated) in spec['amounts'].items():
        if name in units:
            if units[name] not in DAILY:
                raise ValueError(f'{name} in {units[name]}: no conversion to {column}')
            totals = [amount(name, accumulated, day) for day in range(1, days + 1)]
            out[column] = np.array([blank if total is None else mean(np.maximum(total, 0.)) * DAILY[units[name]]
                                    for total in totals])
    pet = np.full((days, matrix.shape[0]), np.nan)
    for day in range(days):
        for c, latitude in enumerate(latitudes):
            stats = out['tmean_c'][day, c], out['tmin_c'][day, c], out['tmax_c'][day, c]
            if all(np.isfinite(stats)):
                pet[day, c] = hargreaves_pet(*stats, latitude, start + timedelta(days=day))
    out['pet_hargreaves_mm'] = pet
    return {column: out[column] for column in COLUMNS if column in out}


def run_start(system, folder, catchments, output):
    import pyarrow as pa
    import pyarrow.parquet as pq
    spec = SYSTEMS[system]
    files = sorted(folder.glob(spec['pattern']))
    if not files:
        print(f'{folder.name}: no member files', flush=True)
        return
    init = datetime.strptime(folder.name, '%Y%m%d%H').replace(tzinfo=timezone.utc)
    names = set(INSTANT) | set(spec['amounts'])
    count, members = len(catchments.ids), []
    for path in files:
        begin = time.time()
        fields, units, matrix = read_member(path, names, catchments)
        values = daily(fields, units, system, matrix, init.date(), catchments.latitudes)
        members.append((spec['member'](path.name), values))
        print(f'{folder.name} {members[-1][0]}: {len(values["tmean_c"])} days in {time.time() - begin:.0f} s',
              flush=True)
    columns = [column for column in COLUMNS if any(column in values for _, values in members)]
    tables = []
    for member, values in members:
        days = len(values['tmean_c'])
        tables.append(pa.table({
            'init_time': pa.array([init] * days * count, pa.timestamp('s', tz='UTC')),
            'member': [member] * days * count,
            'lead_day': np.repeat(np.arange(1, days + 1, dtype='int16'), count),
            'valid_date': pa.array(np.repeat([init.date() + timedelta(days=d) for d in range(days)], count),
                                   pa.date32()),
            'project_id': catchments.ids * days,
            **{column: values.get(column, np.full((days, count), np.nan)).ravel() for column in columns}}))
    partial = output / f'{folder.name}.part'
    pq.write_table(pa.concat_tables(tables), partial, compression='zstd')
    partial.replace(output / f'{folder.name}.parquet')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--system', required=True, choices=sorted(SYSTEMS))
    parser.add_argument('--archive', required=True, type=Path, help='Folder of start folders YYYYMMDDHH')
    parser.add_argument('--geojson', required=True, type=Path, help='Catchment polygons with properties.id')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--start', type=date.fromisoformat, help='First start date (default: all)')
    parser.add_argument('--end', type=date.fromisoformat, help='Last start date (default: all)')
    args = parser.parse_args(argv)
    catchments = Catchments(args.geojson)
    output = args.output / args.system
    output.mkdir(parents=True, exist_ok=True)
    first, last = (f'{day:%Y%m%d}' if day else None for day in (args.start, args.end))
    starts = sorted(folder for folder in args.archive.iterdir()
                    if folder.is_dir() and re.fullmatch(r'\d{10}', folder.name)
                    and (first is None or folder.name[:8] >= first) and (last is None or folder.name[:8] <= last))
    print(f'{len(starts)} starts of {args.system}, {len(catchments.ids)} catchments, into {output}', flush=True)
    for folder in starts:
        if not (output / f'{folder.name}.parquet').exists():
            run_start(args.system, folder, catchments, output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
