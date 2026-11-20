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
at hours 0, 6, 12 and 18 of the day (WeatherNext 2 ignores step 0, so its first
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
    """Historical checkpoint/seed IDs, or the original pNN ID of wn2_pNN_latlon.grib."""
    operational = re.fullmatch(r'wn2_p(\d+)_latlon\.grib', name)
    if operational:
        return f'p{int(operational[1]):02d}'
    match = re.search(r'(?:_s(\d+))?_ckpt(\d+)', name)
    if match is None:
        raise ValueError(f'Unrecognized WN2 member file: {name}')
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


def coded(codes):
    """The naming of messages told apart by GRIB2 codes, keyed as GEFS_CODES: soil fields get their layer, as in
    soilw0.1-0.4, and a field not in codes gets None."""
    def name(get):
        code = tuple(get(key, int) for key in ('discipline', 'parameterCategory', 'parameterNumber',
                                                 'typeOfFirstFixedSurface'))
        depth = lambda which: (get(f'scaledValueOf{which}FixedSurface', int)
                               / 10 ** get(f'scaleFactorOf{which}FixedSurface', int))
        if code[3] == 103:
            return codes.get(code + (round(depth('First')),))
        found = codes.get(code)
        if found is not None and code[3] == 106:
            return f'{found}{depth("First"):g}-{depth("Second"):g}'
        return found
    return name


gefs_name = coded(GEFS_CODES)

# CFSv2 time series: every message is coded as instantaneous at its step, but the precipitation rate and the
# radiation fluxes are means, and tmax and tmin extremes, over the 6 h ending at the step.
CFS_CODES = {
    (0, 0, 0, 103, 2): '2t', (0, 0, 4, 103, 2): 'tmax', (0, 0, 5, 103, 2): 'tmin', (0, 1, 0, 103, 2): 'q',
    (0, 3, 0, 1): 'sp', (0, 2, 2, 103, 10): '10u', (0, 2, 3, 103, 10): '10v', (0, 1, 7, 1): 'prate',
    (0, 4, 192, 1): 'dswrf', (0, 4, 193, 1): 'uswrf', (0, 5, 192, 1): 'dlwrf', (0, 5, 193, 1): 'ulwrf',
    (0, 0, 0, 106): 'tsoil',
}


def vapour_kpa(q, pressure):
    """Vapour pressure in kPa from specific humidity (kg/kg) and pressure (Pa)."""
    return q * pressure / (0.622 + 0.378 * q) / 1000


def humidity(f):
    """(saturation, actual) vapour pressure in kPa from 2 m temperature, specific humidity and surface pressure."""
    return saturation_vapor_pressure_kpa(f('2t') - 273.15), vapour_kpa(f('q'), f('sp'))


CFS_STATES = {
    'relative_humidity_pct': (lambda f: (lambda s, e: 100 * e / s)(*humidity(f)), False),
    'vapor_pressure_deficit_kPa': (lambda f: (lambda s, e: np.maximum(s - e, 0.))(*humidity(f)), False),
    'soil_temperature_layer1_degC': (lambda f: f('tsoil0-0.1') - 273.15, True),
}
CFS_FLUXES = {
    'precipitation_mm': (lambda f: np.maximum(f('prate'), 0.) * 86400, False),
    'shortwave_down_mean_W_m2': (lambda f: f('dswrf'), False),
    'net_radiation_mean_W_m2': (lambda f: f('dswrf') - f('uswrf') + f('dlwrf') - f('ulwrf'), False),
}


def cfs_member(name):
    """cfs_01.grib2 -> 01."""
    return re.search(r'cfs_(\d+)', name)[1]


def ifs_member(name):
    """ifs_c00.grib2 (control) -> c00; ifs_p07.grib2 -> p07."""
    return re.search(r'ifs_([cp]\d+)', name)[1]


def ec46_member(name):
    """ec46_c00.grib (control) -> c00; ec46_p007.grib -> p007; seas5_p007.grib -> p007."""
    return re.search(r'(?:ec46|seas5)_([cp]\d+)', name)[1]


def ec46_name(get):
    """Daily means may carry ecCodes' avg_ prefix instead of the S2S parameter name."""
    name = get('shortName')
    return name[4:] if name in ('avg_2t', 'avg_2d', 'avg_st20') else name


# Net radiation from the accumulated net shortwave and longwave, both signed.
NET_RADIATION = {'ssr': ('net_radiation_mean_W_m2', True, True), 'str': ('net_radiation_mean_W_m2', True, True)}


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

# amounts: {shortName: (column, accumulated from the start or not[, signed])}; amounts of one column add up, and
# the column is left out unless all of them are there. Unsigned amounts below 0 (GRIB packing) count as 0.
# sample_hours: the hours of the day whose instantaneous fields make tmean, wind and humidity (default
# SAMPLE_HOURS; 24 for fields that are already a mean of the day ending at their step).
# extremes: {column: (shortName, window in hours, 'max' or 'min')}, the extreme of the catchment means of the
# windows that end in the day, minimally widened to enclose the sampled daily mean for PET consistency.
SYSTEMS = {
    # Hindcast aifs_f000-f528_p07.grib1 and operational aifs_p07_N320.grib; not the per-step files beside them.
    'aifs': dict(patterns=('aifs_*p[0-9][0-9].grib*', 'aifs_p[0-9][0-9]_N[0-9]*.grib*'), member=aifs_member,
                 first_step=0,
                 amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_W_m2', True)}),
    'wn2': dict(patterns=('predictions_hres_*.grib', 'wn2_p[0-9]*_latlon.grib'), member=wn2_member, first_step=6,
                name_pattern=r'(?:predictions_hres_.*|wn2_p[0-9]+_latlon)\.grib',
                amounts={'tp': ('precipitation_mm', False)}),
    # A gefs file holds every field, or the weather fields only (gefs --weather): the weather fields are
    # required, and a state or flux whose fields come on no day is left out of the table.
    'gefs': dict(patterns=('gefs_*.grib2',), member=gefs_member, first_step=0, naming=gefs_name, window=6,
                 amounts={'tp': ('precipitation_mm', False)}, states=GEFS_STATES, fluxes=GEFS_FLUXES,
                 fields={name for name in GEFS_CODES.values() if name not in ('soilw', 'tsoil')}
                 | {f'{name}{layer}' for name in ('soilw', 'tsoil') for layer in LAYERS},
                 required=('2t', '2d', '10u', '10v', 'tp', 'dswrf')),
    # IFS ENS from ECMWF's open data (ifs): the weather fields, as for AIFS.
    'ifs': dict(patterns=('ifs_*.grib2',), member=ifs_member, first_step=0,
                amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_W_m2', True)},
                required=('2t', '2d', '10u', '10v', 'tp', 'ssrd')),
    # CFSv2 time series (cfs), from step 6.
    'cfs': dict(patterns=('cfs_*.grib2',), member=cfs_member, first_step=6, naming=coded(CFS_CODES),
                amounts={}, states=CFS_STATES, fluxes=CFS_FLUXES,
                extremes={'tmax_degC': ('tmax', 6, 'max'), 'tmin_degC': ('tmin', 6, 'min')},
                fields=set(CFS_CODES.values()) - {'tsoil'} | {'tsoil0-0.1'},
                required=('2t', 'tmax', 'tmin', 'q', 'sp', '10u', '10v', 'prate', 'dswrf', 'uswrf', 'dlwrf',
                          'ulwrf', 'tsoil0-0.1')),
    # ECMWF extended range from the S2S archive (ec46): 2 m temperature, dew point and soil temperature are
    # means of the day ending at their step, and 10 m wind is at 00 UTC, so each day takes its value at hour 24.
    'ec46': dict(patterns=('ec46_*.grib',), member=ec46_member, naming=ec46_name, first_step=0, sample_hours=(24,),
                 amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_W_m2', True),
                          **NET_RADIATION},
                 extremes={'tmax_degC': ('mx2t6', 6, 'max'), 'tmin_degC': ('mn2t6', 6, 'min')},
                 states={'soil_temperature_layer1_degC': (lambda f: f('st20') - 273.15, True)},
                 fields={'mx2t6', 'mn2t6', 'st20'},
                 required=('2t', '2d', '10u', '10v', 'tp', 'ssrd', 'ssr', 'str', 'mx2t6', 'mn2t6', 'st20')),
    # SEAS5 (seas5): instantaneous fields every 6 h from step 6, extremes of the 24 h ending at each day's end.
    'seas5': dict(patterns=('seas5_*.grib',), member=ec46_member, first_step=6,
                  amounts={'tp': ('precipitation_mm', True), 'ssrd': ('shortwave_down_mean_W_m2', True),
                           **NET_RADIATION},
                  extremes={'tmax_degC': ('mx2t24', 24, 'max'), 'tmin_degC': ('mn2t24', 24, 'min')},
                  states={'soil_temperature_layer1_degC': (lambda f: f('stl1') - 273.15, True)},
                  fields={'mx2t24', 'mn2t24', 'stl1'},
                  required=('2t', '2d', '10u', '10v', 'tp', 'ssrd', 'ssr', 'str', 'mx2t24', 'mn2t24', 'stl1')),
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
    if kind == 'regular_gg':
        if get('iScansNegatively') or get('jScansPositively') or get('jPointsAreConsecutive') \
                or get('longitudeOfFirstGridPointInDegrees') != 0:
            raise ValueError('Gaussian grids must run north to south, rows from longitude 0 eastwards')
        rows = np.sort(eccodes.codes_get_array(handle, 'distinctLatitudes'))[::-1]
        counts = np.full(len(rows), get('Ni'))
        return catchments.on((kind, len(rows), int(counts[0])), lambda: reduced_gaussian_cells(rows, counts))
    if kind == 'regular_ll':
        if get('iScansNegatively') or get('jPointsAreConsecutive'):
            raise ValueError('Regular grids must run west to east along rows')
        axis = lambda first, last, count: np.linspace(get(first + 'GridPointInDegrees'),
                                                      get(last + 'GridPointInDegrees'), get(count))
        latitudes = axis('latitudeOfFirst', 'latitudeOfLast', 'Nj')
        # Rows run eastwards and may wrap past 180 (ECMWF's open data run from 180 to 179.75).
        west, east_ = get('longitudeOfFirstGridPointInDegrees'), get('longitudeOfLastGridPointInDegrees')
        longitudes = np.linspace(west, east_ + 360 if east_ < west else east_, get('Ni'))
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


def daily(fields, units, system, matrix, start, latitudes, shift=0):
    """{column: lead days x catchments} from one member's point fields {(shortName, step): values}.

    Lead day d is the UTC day that begins shift + 24(d - 1) hours after the start: shift is 0 for a 00 UTC
    start and 18 for 06 UTC, so every lead day is a whole UTC day; start is the date of lead day 1."""
    spec = SYSTEMS[system]
    mean = lambda values: area_mean(matrix, values)
    days = (max(step for _, step in fields) - shift) // 24
    blank = np.full(matrix.shape[0], np.nan)
    begins = lambda day: shift + 24 * (day - 1)
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
    sample_hours = spec.get('sample_hours', SAMPLE_HOURS)

    def sample_steps(day, hours):
        return [begins(day) + hour for hour in hours if begins(day) + hour >= spec['first_step']]

    def samples(column, day):
        steps = sample_steps(day, sample_hours)
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

    def amount(name, accumulated, signed, day):
        """The day's amount at each grid point, or None when a step is missing."""
        end = begins(day) + 24
        if accumulated:
            last, first = fields.get((name, end)), 0. if begins(day) == 0 else fields.get((name, begins(day)))
            return None if last is None or first is None else last - first
        parts = [fields.get((name, step)) for step in range(end - 18, end + 1, 6)]
        if any(part is None for part in parts):
            return None
        return sum(part if signed else np.maximum(part, 0.) for part in parts)

    by_column = {}
    for name, (column, accumulated, *signed) in spec['amounts'].items():
        by_column.setdefault(column, []).append((name, accumulated, bool(signed and signed[0])))
    for column, parts in by_column.items():
        if any(name not in units for name, _, _ in parts):
            continue
        total = 0.
        for name, accumulated, signed in parts:
            if units[name] not in DAILY:
                raise ValueError(f'{name} in {units[name]}: no conversion to {column}')
            amounts = [amount(name, accumulated, signed, day) for day in range(1, days + 1)]
            total = total + np.array([blank if value is None else
                                      mean(value if signed else np.maximum(value, 0.)) * DAILY[units[name]]
                                      for value in amounts])
        out[column] = total

    for column, (name, window, kind) in spec.get('extremes', {}).items():
        if units.get(name) != 'K':
            raise ValueError(f'{name} in {units.get(name)}, not K')
        pick = np.max if kind == 'max' else np.min
        steps = lambda day: range(begins(day) + window, begins(day) + 25, window)
        extremes = np.array([pick([mean(fields[name, step]) for step in steps(day)], axis=0) - 273.15
                              if all((name, step) in fields for step in steps(day)) else blank
                              for day in range(1, days + 1)])
        daily_mean = out['tmean_degC']
        envelope = np.maximum if kind == 'max' else np.minimum
        out[column] = np.where(np.isfinite(daily_mean), envelope(extremes, daily_mean), extremes)

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

    for kind, hours in (('states', sample_hours), ('fluxes', WINDOW_ENDS)):
        for column, (formula, land) in spec.get(kind, {}).items():
            values = np.array([day_mean(formula, land, sample_steps(day, hours)) for day in range(1, days + 1)])
            if np.isfinite(values).any():   # the file has the fields on some day
                out[column] = values
    pet = np.full((days, matrix.shape[0]), np.nan)
    for day in range(days):
        for c, latitude in enumerate(latitudes):
            stats = out['tmean_degC'][day, c], out['tmin_degC'][day, c], out['tmax_degC'][day, c]
            if all(np.isfinite(stats)):
                pet[day, c] = hargreaves_pet(*stats, latitude, start + timedelta(days=day))
    out['pet_hargreaves_mm_day'] = pet
    return {column: out[column] for column in COLUMNS if column in out}


def member_files(folder, patterns, name_pattern=None):
    """The member files of a start folder, or one level down, where operational runs keep them (FCST)."""
    for prefix in ('', '*/'):
        files = sorted({path for pattern in patterns for path in folder.glob(prefix + pattern)
                        if name_pattern is None or re.fullmatch(name_pattern, path.name)})
        if files:
            return files
    return []


def run_start(system, folder, catchments, output):
    import pyarrow as pa
    import pyarrow.parquet as pq
    spec = SYSTEMS[system]
    files = member_files(folder, spec['patterns'], spec.get('name_pattern'))
    if not files:
        print(f'{folder.name}: no member files', flush=True)
        return
    member_ids = [spec['member'](path.name) for path in files]
    if len(set(member_ids)) != len(member_ids):
        raise ValueError(f'{folder.name}: Duplicate member IDs in the input files')
    init = datetime.strptime(folder.name, '%Y%m%d%H').replace(tzinfo=timezone.utc)
    # Lead day 1 is the first whole UTC day of the run.
    shift = (24 - init.hour) % 24
    first = (init + timedelta(hours=shift)).date()
    names = set(INSTANT) | set(spec['amounts']) | spec.get('fields', set())
    count, members = len(catchments.ids), []
    for path, member in zip(files, member_ids):
        begin = time.time()
        fields, units, matrix = read_member(path, names, catchments, spec.get('naming'), spec.get('window'),
                                            spec.get('required', ()))
        values = daily(fields, units, system, matrix, first, catchments.latitudes, shift)
        if not len(values['tmean_degC']):
            raise ValueError(f'{path}: no whole lead day; is it a member file?')
        members.append((member, values))
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
            'valid_date': pa.array(np.repeat([first + timedelta(days=d) for d in range(days)], count),
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
