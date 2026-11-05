"""Daily catchment means of ensemble forecasts, one table per start, one row per member.

    python -m catchment_forecast.aggregate --system aifs --archive ROOT --geojson CATCHMENTS \\
        --output DIR [--start 2024-01-01 --end 2024-12-31]

ROOT holds one folder per start (YYYYMMDDHH) with one GRIB file per member.
Each start becomes DIR/SYSTEM/YYYYMMDDHH.parquet with init_time, member,
lead_day, valid_date, project_id and the column names of the delivered
history table. A start already written is skipped, so a rerun picks up where
an earlier one stopped.

Nothing depends on the number of members: every file found is one member,
named from its file name, and each member gets its own complete rows.

Lead day d covers hours 24(d-1) to 24d after the start, which for a 00 UTC
start is a UTC day as in the history tables. Instantaneous fields are sampled
at hours 0, 6, 12 and 18 of the day (WeatherNext 2 has no step 0, so its first
day has three samples); tmean_degC is their mean and tmax_degC and tmin_degC their
extremes, so the daily range is narrower than from hourly data until the
values are mapped onto the history. Wind speed, relative humidity and vapour
pressure deficit are computed at each grid point before the area mean, as in
the history tables. Amounts are summed over the day at each grid point:
AIFS accumulates from the start, so the totals at the ends of the day are
differenced; WeatherNext 2 and GEFS give 6 h amounts, which are added. Negative
amounts from GRIB packing become 0, and units follow each field's GRIB units.
PET is Hargreaves from the daily temperatures at the catchment centroid
latitude. Rainfall and snowfall come from the adjustment, which needs the
history tables.

GEFS (see gefs) also gives the land-surface columns, as the history table
defines them: its states (snow, soil, the 0 degC level) are sampled like 2 m
temperature, and its 6 h mean fluxes (radiation, cloud, latent heat, snow
melting heat) are averaged over the four that end in the day. Snow and soil
fields are missing over water and are averaged over a catchment's land cells;
cells with 5 m or more of snow water are glaciers and are left out of the snow
means, as in the history table. Its fields are told apart by their GRIB2 codes,
which unlike eccodes short names do not change between eccodes versions, and
every 6 h mean or amount must cover the 6 h ending at its step.
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

from .grids import area_mean, land_mean, reduced_gaussian_cells, regular_cells, weights


def aifs_member(name):
    """aifs_f000-f528_p07.grib1 (hindcast) or aifs_p07_N320.grib (operational) -> p07."""
    return 'p' + re.search(r'_p(\d+)', name)[1]


def wn2_member(name):
    """predictions_hres_ckpt2.grib (seed 0) -> ckpt2_s000; predictions_hres_s003_ckpt2.grib -> ckpt2_s003."""
    match = re.search(r'(?:_s(\d+))?_ckpt(\d+)', name)
    return f'ckpt{match[2]}_s{int(match[1] or 0):03d}'


def gefs_member(name):
    """gefs_c00.grib2 (control) -> c00; gefs_p07.grib2 -> p07."""
    return re.search(r'gefs_([cp]\d+)', name)[1]


# GEFS fields by GRIB2 discipline, parameter category and number and type of first fixed surface, with the
# height for fields above ground.
GEFS_CODES = {
    (0, 0, 0, 103, 2): '2t', (0, 0, 6, 103, 2): '2d', (0, 2, 2, 103, 10): '10u', (0, 2, 3, 103, 10): '10v',
    (0, 1, 8, 1): 'tp', (0, 4, 192, 1): 'dswrf', (0, 4, 193, 1): 'uswrf', (0, 5, 192, 1): 'dlwrf',
    (0, 5, 193, 1): 'ulwrf', (0, 6, 1, 10): 'tcc', (0, 0, 10, 1): 'lhtfl', (0, 0, 16, 1): 'snohf',
    (0, 1, 13, 1): 'weasd', (0, 1, 11, 1): 'snod', (0, 3, 5, 4): 'hgt0c', (0, 3, 5, 1): 'orog',
    (2, 0, 192, 106): 'soilw', (2, 0, 2, 106): 'tsoil',
}
LAYERS = ('0-0.1', '0.1-0.4', '0.4-1', '1-2')   # GEFS soil layers in m
GLACIER_MM = 5000.
VAPORIZATION = 2.45e6   # J/kg, latent heat flux to evapotranspiration
FUSION = 3.34e5         # J/kg, snow phase-change heat flux to snowmelt


def gefs_name(get):
    """The name of a GEFS message (soil fields with their layer, soilw0.1-0.4), or None for a field not used."""
    code = tuple(get(key, int) for key in ('discipline', 'parameterCategory', 'parameterNumber',
                                             'typeOfFirstFixedSurface'))
    depth = lambda which: (get(f'scaledValueOf{which}FixedSurface', int)
                           / 10 ** get(f'scaleFactorOf{which}FixedSurface', int))
    if code[3] == 103:
        return GEFS_CODES.get(code + (round(depth('First')),))
    name = GEFS_CODES.get(code)
    if name is not None and code[3] == 106:
        return f'{name}{depth("First"):g}-{depth("Second"):g}'
    return name


def seasonal(f, values):
    """values without the glacier cells."""
    return np.where(f('weasd') >= GLACIER_MM, np.nan, values)


# GEFS columns beyond those of 2t, 2d, 10u, 10v and tp: {column: (point values from the fields f of one step,
# over land cells only)}. States are sampled at SAMPLE_HOURS; fluxes are the 6 h means ending at hours 6 to 24.
GEFS_STATES = {
    'snow_water_equivalent_mm': (lambda f: seasonal(f, f('weasd')), True),
    'snow_depth_m': (lambda f: seasonal(f, f('snod')), True),
    'freezing_level_above_ground_m': (lambda f: np.maximum(f('hgt0c') - f('orog'), 0.), False),
    'freezing_level_above_sea_level_m': (lambda f: np.maximum(f('hgt0c'), f('orog')), False),
    'soil_moisture_layer1_m3_m3': (lambda f: f('soilw0-0.1'), True),
    'root_zone_soil_moisture_0_100cm_m3_m3': (
        lambda f: .1 * f('soilw0-0.1') + .3 * f('soilw0.1-0.4') + .6 * f('soilw0.4-1'), True),
    'soil_moisture_layer4_m3_m3': (lambda f: f('soilw1-2'), True),
    **{f'soil_temperature_layer{i}_degC': (lambda f, layer=layer: f(f'tsoil{layer}') - 273.15, True)
       for i, layer in enumerate(LAYERS, 1)},
}
GEFS_FLUXES = {
    'shortwave_down_mean_W_m2': (lambda f: f('dswrf'), False),
    'net_radiation_mean_W_m2': (lambda f: f('dswrf') - f('uswrf') + f('dlwrf') - f('ulwrf'), False),
    'cloud_cover_fraction': (lambda f: f('tcc') / 100, False),
    'actual_evapotranspiration_mm': (lambda f: f('lhtfl') * 86400 / VAPORIZATION, False),
    'snowmelt_mm': (lambda f: np.maximum(f('snohf'), 0.) * 86400 / FUSION, True),
}
# Fields read once, at whatever step they come.
CONSTANT = ('orog',)

# amounts: {shortName: (column, accumulated from the start or not)}.
SYSTEMS = {
    # Hindcast aifs_f000-f528_p07.grib1 and operational aifs_p07_N320.grib; not the per-step files beside them.
    'aifs': dict(patterns=('aifs_*p[0-9][0-9].grib*', 'aifs_p[0-9][0-9]_N[0-9]*.grib*'), member=aifs_member,
                 first_step=0,
                 amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_W_m2', True)}),
    'wn2': dict(patterns=('predictions_hres_*.grib',), member=wn2_member, first_step=6,
                amounts={'tp': ('precipitation_mm', False)}),
    'gefs': dict(patterns=('gefs_*.grib2',), member=gefs_member, first_step=0, naming=gefs_name, window=6,
                 amounts={'tp': ('precipitation_mm', False)}, states=GEFS_STATES, fluxes=GEFS_FLUXES,
                 fields={name for name in GEFS_CODES.values() if name not in ('soilw', 'tsoil')}
                 | {f'{name}{layer}' for name in ('soilw', 'tsoil') for layer in LAYERS}),
}
INSTANT = ('2t', '2d', '10u', '10v')
SAMPLE_HOURS = (0, 6, 12, 18)
WINDOW_ENDS = (6, 12, 18, 24)
# Factor from a day's amount in the field's GRIB units to the column's units.
DAILY = {'m': 1000., 'kg m**-2': 1., 'J m**-2': 1 / 86400}
COLUMNS = ['precipitation_mm', 'tmean_degC', 'tmax_degC', 'tmin_degC', 'relative_humidity_pct',
           'vapor_pressure_deficit_kPa', 'shortwave_down_mean_W_m2', 'u_wind_m_s', 'v_wind_m_s', 'wind_speed_m_s',
           'pet_hargreaves_mm_day', 'net_radiation_mean_W_m2', 'cloud_cover_fraction', 'actual_evapotranspiration_mm',
           'snow_water_equivalent_mm', 'snow_depth_m', 'snowmelt_mm', 'freezing_level_above_ground_m',
           'freezing_level_above_sea_level_m', 'soil_moisture_layer1_m3_m3', 'root_zone_soil_moisture_0_100cm_m3_m3',
           'soil_moisture_layer4_m3_m3', *(f'soil_temperature_layer{i}_degC' for i in range(1, 5))]


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


def read_member(path, names, catchments, naming=None, window=None, required=()):
    """({(name, end step in hours): values at the used points}, {name: units}, area matrix).

    A field's name is its short name, or what naming makes of the message (None for a field not used). With
    a window (hours), a field with a period must cover the window hours ending at its step, and is skipped at
    step 0. Every required name must come at least once.
    """
    import eccodes
    fields, units, grid = {}, {}, None
    with open(path, 'rb') as stream:
        while (handle := eccodes.codes_grib_new_from_file(stream)) is not None:
            try:
                get = lambda key, kind=None: eccodes.codes_get(handle, key, kind)
                name = naming(get) if naming else get('shortName')
                if name not in names:
                    continue
                grid = grid or grid_weights(eccodes, handle, catchments)
                eccodes.codes_set(handle, 'stepUnits', 'h')
                step = int(get('endStep'))
                if window and get('stepType') != 'instant':
                    if step == 0:
                        continue
                    if step - int(get('startStep')) != window:
                        raise ValueError(f'{path.name}: {name} at step {step} covers {get("stepRange")} h, '
                                         f'not the {window} h before')
                values = eccodes.codes_get_values(handle)
                if get('bitmapPresent'):
                    values[values == get('missingValue')] = np.nan
                fields[name, step] = values[grid[0]]
                units[name] = get('units')
            finally:
                eccodes.codes_release(handle)
    if grid is None:
        raise ValueError(f'{path.name}: none of {", ".join(sorted(names))}')
    missing = set(required) - {name for name, _ in fields}
    if missing:
        raise ValueError(f'{path.name}: no {", ".join(sorted(missing))}')
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
        at['tmean_degC', step] = mean(t)
        if ('10u', step) in fields and ('10v', step) in fields:
            u, v = fields['10u', step], fields['10v', step]
            at['u_wind_m_s', step], at['v_wind_m_s', step] = mean(u), mean(v)
            at['wind_speed_m_s', step] = mean(np.hypot(u, v))
        if ('2d', step) in fields:
            saturation = saturation_vapor_pressure_kpa(t)
            vapour = saturation_vapor_pressure_kpa(fields['2d', step] - 273.15)
            at['relative_humidity_pct', step] = mean(100 * vapour / saturation)
            at['vapor_pressure_deficit_kPa', step] = mean(np.maximum(saturation - vapour, 0.))

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
        if column == 'tmean_degC':
            out['tmax_degC'] = np.array([values.max(axis=0) for values in by_day])
            out['tmin_degC'] = np.array([values.min(axis=0) for values in by_day])

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

    constant = {name: values for (name, _), values in fields.items() if name in CONSTANT}

    def field(step):
        def get(name):
            values = fields.get((name, step), constant.get(name))
            if values is None:
                raise KeyError(name)
            return values
        return get

    def day_mean(formula, land, steps):
        """Mean over the steps of the catchment means of formula; NaN when a step lacks a field."""
        try:
            points = [formula(field(step)) for step in steps]
        except KeyError:
            return blank
        return np.mean([(land_mean if land else area_mean)(matrix, values) for values in points], axis=0)

    for kind, hours in (('states', SAMPLE_HOURS), ('fluxes', WINDOW_ENDS)):
        for column, (formula, land) in spec.get(kind, {}).items():
            out[column] = np.array([day_mean(formula, land, [24 * (day - 1) + hour for hour in hours])
                                    for day in range(1, days + 1)])
    pet = np.full((days, matrix.shape[0]), np.nan)
    for day in range(days):
        for c, latitude in enumerate(latitudes):
            stats = out['tmean_degC'][day, c], out['tmin_degC'][day, c], out['tmax_degC'][day, c]
            if all(np.isfinite(stats)):
                pet[day, c] = hargreaves_pet(*stats, latitude, start + timedelta(days=day))
    out['pet_hargreaves_mm_day'] = pet
    return {column: out[column] for column in COLUMNS if column in out}


def member_files(folder, patterns):
    """The member files of a start folder, or one level down, where operational runs keep them (FCST)."""
    for prefix in ('', '*/'):
        files = sorted({path for pattern in patterns for path in folder.glob(prefix + pattern)})
        if files:
            return files
    return []


def run_start(system, folder, catchments, output):
    import pyarrow as pa
    import pyarrow.parquet as pq
    spec = SYSTEMS[system]
    files = member_files(folder, spec['patterns'])
    if not files:
        print(f'{folder.name}: no member files', flush=True)
        return
    init = datetime.strptime(folder.name, '%Y%m%d%H').replace(tzinfo=timezone.utc)
    names = set(INSTANT) | set(spec['amounts']) | spec.get('fields', set())
    count, members = len(catchments.ids), []
    for path in files:
        begin = time.time()
        fields, units, matrix = read_member(path, names, catchments, spec.get('naming'), spec.get('window'),
                                            spec.get('fields', ()))
        values = daily(fields, units, system, matrix, init.date(), catchments.latitudes)
        if not len(values['tmean_degC']):
            raise ValueError(f'{path}: no whole lead day; is it a member file?')
        members.append((spec['member'](path.name), values))
        print(f'{folder.name} {members[-1][0]}: {len(values["tmean_degC"])} days in {time.time() - begin:.0f} s',
              flush=True)
    columns = [column for column in COLUMNS if any(column in values for _, values in members)]
    tables = []
    for member, values in members:
        days = len(values['tmean_degC'])
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
