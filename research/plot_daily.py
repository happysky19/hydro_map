"""Plot daily catchment series from a delivery or from source folders still downloading.

INPUT is either a working directory holding aorc/, era5-land/ and era5/ source
folders (completed years or months are plotted while the rest downloads), or a
full delivery table with its manifest (OUTPUT_full.csv).

Kinds:
  overview   precipitation as rain and snow, air temperature, snow water
             equivalent, soil moisture, evapotranspiration and the 0 degC level
  compare    one variable from every source that provides it, and the
             ERA5-Land minus AORC difference
  wateryear  one variable by water year (1 Oct to 30 Sep): running totals for
             daily amounts, daily values for states; earlier years, their median
             and the latest year

Kinds that draw every catchment at once (--geojson gives the polygons, names
and outlets; only years complete for every catchment are used):
  map        annual precipitation, snowfall share and mean air temperature on
             the catchment polygons
  seasons    monthly precipitation, snowfall share and air temperature, one
             row per catchment, rows grouped by river system
  anomaly    water-year precipitation of each catchment as a percentage of its
             own mean, one row per catchment and one column per water year;
             a dot marks a water year with missing days
  annual     annual precipitation as rain and snow with potential (AORC) or
             actual (ERA5-Land) evapotranspiration, catchments sorted wettest
             first
  event      one variable (--variable) day by day between --start and --end,
             one row per catchment, for example a flood or a cold wave
"""

import argparse
import calendar
import csv
from datetime import date, timedelta
import gzip
import json
from pathlib import Path

import numpy as np


AORC, LAND, ERA5 = 'aorc_v1.1', 'era5_land_cds', 'era5_cds'
SOURCE_NAMES = {AORC: 'AORC', LAND: 'ERA5-Land', ERA5: 'ERA5'}
# Categorical slots in fixed order: one colour per source wherever sources are compared.
SERIES = ['#2a78d6', '#eb6834', '#1baf7a']
SOURCE_COLORS = dict(zip(SOURCE_NAMES, SERIES))
INK, INK_SECONDARY, MUTED, GRID, AXIS, SURFACE = '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#c3c2b7', '#fcfcfb'
AMOUNTS = {'precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'snowmelt_mm',
           'actual_evapotranspiration_mm', 'pet_hargreaves_mm'}
LABELS = {
    'precipitation_mm': ('Precipitation', 'mm'), 'rainfall_mm': ('Rainfall', 'mm'),
    'snowfall_mm': ('Snowfall', 'mm'), 'snowmelt_mm': ('Snowmelt', 'mm'),
    'tmean_c': ('2 m air temperature', '°C'), 'tmin_c': ('Daily minimum temperature', '°C'),
    'tmax_c': ('Daily maximum temperature', '°C'), 'wet_bulb_temperature_c': ('Wet-bulb temperature', '°C'),
    'relative_humidity_pct': ('Relative humidity', '%'), 'specific_humidity_kgkg': ('Specific humidity', 'kg/kg'),
    'vapor_pressure_deficit_kpa': ('Vapor pressure deficit', 'kPa'),
    'surface_pressure_pa': ('Surface pressure', 'Pa'), 'wind_speed_ms': ('10 m wind speed', 'm/s'),
    'u_wind_ms': ('10 m eastward wind', 'm/s'), 'v_wind_ms': ('10 m northward wind', 'm/s'),
    'shortwave_down_mean_wm2': ('Downward shortwave radiation', 'W/m²'),
    'longwave_down_mean_wm2': ('Downward longwave radiation', 'W/m²'),
    'net_radiation_mean_wm2': ('Net radiation', 'W/m²'),
    'snow_water_equivalent_mm': ('Snow water equivalent', 'mm'), 'snow_depth_m': ('Snow depth', 'm'),
    'snow_cover_pct': ('Snow cover', '%'), 'snow_density_kgm3': ('Snow density', 'kg/m³'),
    'soil_moisture_layer1_m3m3': ('Surface soil moisture, 0–7 cm', 'm³/m³'),
    'root_zone_soil_moisture_0_100cm_m3m3': ('Root-zone soil moisture, 0–100 cm', 'm³/m³'),
    'soil_moisture_layer4_m3m3': ('Deep soil moisture, 100–289 cm', 'm³/m³'),
    'actual_evapotranspiration_mm': ('Actual evapotranspiration', 'mm'),
    'pet_hargreaves_mm': ('Potential evapotranspiration', 'mm'),
    'cloud_cover_fraction': ('Total cloud cover', 'fraction'),
    'freezing_level_above_sea_level_m': ('0 °C level above sea level', 'm'),
    'freezing_level_above_ground_m': ('0 °C level above ground', 'm'),
}
OVERVIEW = [
    ('precipitation', [(AORC, 'rainfall_mm'), (AORC, 'snowfall_mm'), (AORC, 'precipitation_mm')]),
    ('temperature', [(AORC, 'tmean_c'), (AORC, 'tmin_c'), (AORC, 'tmax_c')]),
    ('swe', [(LAND, 'snow_water_equivalent_mm')]),
    ('soil', [(LAND, 'soil_moisture_layer1_m3m3'), (LAND, 'root_zone_soil_moisture_0_100cm_m3m3')]),
    ('et', [(LAND, 'actual_evapotranspiration_mm'), (AORC, 'pet_hargreaves_mm')]),
    ('freezing', [(ERA5, 'freezing_level_above_sea_level_m')]),
]


def lowercase_first(text):
    return text[:1].lower() + text[1:]


def label(variable):
    return LABELS.get(variable, (variable.replace('_', ' '), ''))


def load_many(path, variables, start=None, end=None, projects=None):
    """Valid daily values as {project: {(source, variable): (dates, values)}}, for PROJECTS or every catchment."""
    path, wanted, found = Path(path), set(variables), {}

    def keep(project, source, variable, day, value):
        if variable not in wanted or value in ('', None) or (projects is not None and project not in projects):
            return
        day = day if isinstance(day, date) else date.fromisoformat(day)
        if (start is None or day >= start) and (end is None or day <= end):
            found.setdefault(project, {}).setdefault((source, variable), {})[day] = float(value)

    if path.is_dir():
        files = sorted(path.glob('*/daily_*.csv*')) + sorted(path.glob('daily_*.csv*'))
        if not files:
            raise ValueError(f'No daily source files under {path}')
        for file in files:
            opener = gzip.open if file.suffix == '.gz' else open
            with opener(file, 'rt', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row['qc'] == 'valid':
                        keep(row['project_id'], row['source'], row['variable'], row['date'], row['value'])
    else:
        from check_daily import table_rows
        manifest = json.loads(Path(str(path) + '.manifest.json').read_text())
        columns = {column: (info['source'], info['variable']) for column, info in manifest['columns'].items()
                   if info.get('variable') in wanted}
        for row in table_rows(path, manifest['columns']):
            day = date.fromisoformat(str(row['date']))
            for column, (source, variable) in columns.items():
                keep(row['project_id'], source, variable, day, row[column])
    return {project: {key: (np.array(sorted(values)), np.array([values[d] for d in sorted(values)]))
                      for key, values in series.items()} for project, series in found.items()}


def load(path, project, variables, start=None, end=None):
    """Valid daily values as {(source, variable): (dates, values)} for one catchment."""
    return load_many(path, variables, start, end, {project}).get(project, {})


def resolution(series, choice):
    if choice != 'auto':
        return choice
    days = [d for d, _ in series.values()]
    span = (max(d[-1] for d in days) - min(d[0] for d in days)).days if days else 0
    return 'D' if span <= 400 else 'W' if span <= 6*366 else 'M'


def period_start(day, rule):
    if rule == 'W':
        return day - timedelta(days=day.weekday())
    if rule == 'M':
        return day.replace(day=1)
    return day


def next_period(day, rule):
    if rule == 'D':
        return day + timedelta(days=1)
    if rule == 'W':
        return day + timedelta(days=7)
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def resample(days, values, rule, amount):
    """Regular daily, weekly or monthly points; missing periods are NaN so lines break.

    Weekly and monthly points are totals for amounts (complete periods only) and
    means for states (at least half of the days present).
    """
    if not len(days):
        return days, values
    groups = {}
    for day, value in zip(days, values):
        groups.setdefault(period_start(day, rule), []).append(value)
    starts, first = [], period_start(days[0], rule)
    while first <= days[-1]:
        starts.append(first)
        first = next_period(first, rule)
    output = []
    for first in starts:
        if first not in groups:
            output.append(np.nan)
            continue
        if rule == 'D':
            output.append(groups[first][0])
            continue
        length = (next_period(first, rule) - first).days
        group = groups[first]
        if amount:
            output.append(sum(group) if len(group) == length else np.nan)
        else:
            output.append(np.mean(group) if len(group) >= length/2 else np.nan)
    return np.array(starts), np.array(output)


def units_per_period(units, amount, rule):
    return f'{units}/' + {'D': 'day', 'W': 'week', 'M': 'month'}[rule] if amount else units


def setup():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        'font.family': 'DejaVu Sans', 'font.size': 9, 'axes.titlesize': 10, 'axes.titleweight': 'normal',
        'axes.edgecolor': AXIS, 'axes.labelcolor': INK_SECONDARY, 'axes.facecolor': SURFACE,
        'figure.facecolor': SURFACE, 'savefig.facecolor': SURFACE, 'text.color': INK,
        'xtick.color': MUTED, 'ytick.color': MUTED, 'xtick.labelsize': 8.5, 'ytick.labelsize': 8.5,
        'axes.spines.top': False, 'axes.spines.right': False, 'legend.frameon': False,
        'legend.fontsize': 8.5, 'lines.linewidth': 1.6, 'lines.solid_capstyle': 'round'})
    return plt


def style(ax, title, units):
    ax.set_title(f'{title} ({units})' if units else title, loc='left', color=INK, pad=6)
    ax.grid(axis='y', color=GRID, linewidth=.6)
    ax.set_axisbelow(True)
    ax.tick_params(length=3)


def date_axis(ax):
    import matplotlib.dates as mdates
    locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))


def bar_width(days, rule):
    return {'D': .9, 'W': 6, 'M': 27}[rule]


def plot_overview(series, project, rule, title):
    plt = setup()
    available = [(name, [key for key in keys if key in series]) for name, keys in OVERVIEW]
    available = [(name, keys) for name, keys in available if keys]
    if not available:
        raise ValueError('None of the overview variables has valid data for this catchment')
    fig, axes = plt.subplots(len(available), 1, figsize=(11, 1.95*len(available) + .9), sharex=True,
                             layout='constrained', squeeze=False)
    get = lambda source, variable: resample(*series[source, variable], rule, variable in AMOUNTS)
    for ax, (name, keys) in zip(axes[:, 0], available):
        if name == 'precipitation':
            phase = [(AORC, 'rainfall_mm'), (AORC, 'snowfall_mm')]
            if all(key in series for key in phase):
                (days, rain), (_, snow) = get(*phase[0]), get(*phase[1])
                width = bar_width(days, rule)
                ax.bar(days, rain, width=width, color=SERIES[0], label='Rain', linewidth=0)
                ax.bar(days, snow, width=width, bottom=np.nan_to_num(rain), color=SERIES[1], label='Snow',
                       linewidth=0)
                ax.legend(loc='upper left', ncols=2)
            else:
                days, total = get(AORC, 'precipitation_mm')
                ax.bar(days, total, width=bar_width(days, rule), color=SERIES[0], linewidth=0)
            ax.set_ylim(bottom=0)
            style(ax, 'Precipitation, AORC', units_per_period('mm', True, rule))
        elif name == 'temperature':
            if (AORC, 'tmean_c') in series:
                days, mean = get(AORC, 'tmean_c')
                if (AORC, 'tmin_c') in series and (AORC, 'tmax_c') in series:
                    low_days, low = get(AORC, 'tmin_c')
                    high_days, high = get(AORC, 'tmax_c')
                    if np.array_equal(low_days, days) and np.array_equal(high_days, days):
                        ax.fill_between(days, low, high, color=SERIES[0], alpha=.18, linewidth=0,
                                        label='Daily minimum to maximum')
                ax.plot(days, mean, color=SERIES[0], label='Daily mean')
                ax.axhline(0, color=MUTED, linewidth=.8, linestyle=(0, (4, 3)))
                ax.annotate('0 °C', xy=(1, 0), xycoords=('axes fraction', 'data'), xytext=(4, 0),
                            textcoords='offset points', va='center', color=MUTED, fontsize=8)
                ax.legend(loc='upper left', ncols=2)
            style(ax, '2 m air temperature, AORC', '°C')
        elif name == 'swe':
            days, swe = get(LAND, 'snow_water_equivalent_mm')
            ax.fill_between(days, 0, swe, color=SERIES[0], alpha=.18, linewidth=0)
            ax.plot(days, swe, color=SERIES[0])
            ax.set_ylim(bottom=0)
            style(ax, 'Snow water equivalent, ERA5-Land seasonal snow', 'mm')
        elif name == 'soil':
            for slot, key in enumerate(keys):
                days, values = get(*key)
                ax.plot(days, values, color=SERIES[slot],
                        label={'soil_moisture_layer1_m3m3': 'Surface, 0–7 cm',
                               'root_zone_soil_moisture_0_100cm_m3m3': 'Root zone, 0–100 cm'}[key[1]])
            if len(keys) > 1:
                ax.legend(loc='upper left', ncols=2)
            single = '' if len(keys) > 1 else ', ' + lowercase_first(ax.get_lines()[0].get_label())
            style(ax, f'Soil moisture{single}, ERA5-Land', 'm³/m³')
        elif name == 'et':
            for slot, key in enumerate(keys):
                days, values = get(*key)
                ax.plot(days, values, color=SERIES[slot],
                        label={'actual_evapotranspiration_mm': 'Actual, ERA5-Land',
                               'pet_hargreaves_mm': 'Potential, Hargreaves from AORC'}[key[1]])
            if len(keys) > 1:
                ax.legend(loc='upper left', ncols=2)
            single = '' if len(keys) > 1 else ', ' + lowercase_first(ax.get_lines()[0].get_label())
            style(ax, f'Evapotranspiration{single}', units_per_period('mm', True, rule))
        elif name == 'freezing':
            days, values = get(ERA5, 'freezing_level_above_sea_level_m')
            ax.plot(days, values, color=SERIES[0])
            style(ax, '0 °C level above sea level, ERA5', 'm')
    date_axis(axes[-1, 0])
    fig.suptitle(title, x=.01, ha='left', fontsize=12, color=INK)
    return fig


def plot_compare(series, variable, rule, title):
    plt = setup()
    sources = [source for source in SOURCE_NAMES if (source, variable) in series]
    if not sources:
        raise ValueError(f'No valid {variable} values for this catchment')
    name, units = label(variable)
    amount = variable in AMOUNTS
    pair = AORC in sources and LAND in sources
    fig, axes = plt.subplots(2 if pair else 1, 1, figsize=(11, 5.6 if pair else 3.4), sharex=True,
                             layout='constrained', squeeze=False, height_ratios=[2, 1] if pair else None)
    resampled = {source: resample(*series[source, variable], rule, amount) for source in sources}
    ax = axes[0, 0]
    for source in sources:
        ax.plot(*resampled[source], color=SOURCE_COLORS[source], label=SOURCE_NAMES[source])
    if len(sources) > 1:
        ax.legend(loc='upper left', ncols=len(sources))
    style(ax, name, units_per_period(units, amount, rule))
    if pair:
        (aorc_days, aorc), (land_days, land) = resampled[AORC], resampled[LAND]
        common, a_index, l_index = np.intersect1d(aorc_days, land_days, return_indices=True)
        difference = land[l_index] - aorc[a_index]
        valid = np.isfinite(difference)
        ax = axes[1, 0]
        ax.axhline(0, color=AXIS, linewidth=.9)
        ax.plot(common, difference, color=INK_SECONDARY, linewidth=1.3)
        statistics = ''
        if valid.sum() > 2:
            r = np.corrcoef(aorc[a_index][valid], land[l_index][valid])[0, 1]
            statistics = f' · mean {np.mean(difference[valid]):+.3g}, correlation {r:.2f}'
        style(ax, f'ERA5-Land minus AORC{statistics}', units_per_period(units, amount, rule))
    date_axis(axes[-1, 0])
    fig.suptitle(title, x=.01, ha='left', fontsize=12, color=INK)
    return fig


def water_year(day):
    return day.year + 1 if day.month >= 10 else day.year


def plot_water_years(series, variable, source, title):
    plt = setup()
    if (source, variable) not in series:
        raise ValueError(f'No valid {SOURCE_NAMES.get(source, source)} {variable} values for this catchment')
    days, values = series[source, variable]
    name, units = label(variable)
    amount = variable in AMOUNTS
    years = {}
    for day, value in zip(days, values):
        start = date(water_year(day) - 1, 10, 1)
        years.setdefault(water_year(day), np.full(366, np.nan))[(day - start).days] = value
    curves = {}
    for year, daily in years.items():
        # Running totals treat a missing day as no contribution; states keep their gaps.
        curves[year] = np.cumsum(np.nan_to_num(daily)) if amount else daily
        if amount:
            curves[year][np.flatnonzero(np.isfinite(daily))[-1] + 1:] = np.nan
    latest = max(curves)
    fig, ax = plt.subplots(figsize=(10, 4.8), layout='constrained')
    x = np.arange(366)
    earlier = [year for year in sorted(curves) if year != latest]
    for index, year in enumerate(earlier):
        ax.plot(x, curves[year], color=AXIS, linewidth=1, label='Earlier water years' if index == 0 else None)
    complete = [curves[year] for year in earlier if np.isfinite(curves[year][:365]).mean() > .9]
    if len(complete) >= 3:
        ax.plot(x, np.nanmedian(np.array(complete), axis=0), color=INK_SECONDARY, linewidth=1.6,
                linestyle=(0, (5, 3)), label=f'Median of {len(complete)} water years')
    ax.plot(x, curves[latest], color=SERIES[0], linewidth=2.2, label=f'Water year {latest}')
    last = np.flatnonzero(np.isfinite(curves[latest]))
    if len(last):
        ax.annotate(f'WY{latest}', xy=(x[last[-1]], curves[latest][last[-1]]), xytext=(5, 0),
                    textcoords='offset points', va='center', color=INK, fontsize=8.5)
    months = [date(2000, month, 1) for month in (10, 11, 12)] + [date(2001, month, 1) for month in range(1, 10)]
    ax.set_xticks([(month - date(2000, 10, 1)).days for month in months], [m.strftime('%b') for m in months])
    ax.set_xlim(0, 365)
    if amount or variable in ('snow_water_equivalent_mm', 'snow_depth_m'):
        ax.set_ylim(bottom=0)
    ax.legend(loc='upper left')
    style(ax, f"{'Running total of ' + lowercase_first(name) if amount else name}, {SOURCE_NAMES.get(source, source)}",
          units)
    fig.suptitle(title, x=.01, ha='left', fontsize=12, color=INK)
    return fig


# ---------------------------------------------------------------- every catchment at once

CATCHMENT_KINDS = ('map', 'seasons', 'anomaly', 'annual', 'event')
# Rows of the catchment figures, upstream to downstream within each river system. Catchments
# not listed here follow, north to south.
GROUPS = [
    ('Columbia, Canada', ['MICA', 'REVELSTOKE', 'HUGH_KEENLEYSIDE', 'DUNCAN', 'KOOTENAY_CANAL', 'CORRA_LINN',
                          'BRILLIANT', 'SEVEN_MILE', 'WANETA']),
    ('Kootenai, Flathead,\nPend Oreille', ['LIBBY', 'HUNGRY_HORSE', 'KERR', 'NOXON_RAPIDS', 'CABINET_GORGE',
                                           'ALBENI_FALLS', 'BOUNDARY']),
    ('Columbia, US', ['GRAND_COULEE', 'CHIEF_JOSEPH', 'WELLS', 'ROCKY_REACH', 'ROCK_ISLAND', 'WANAPUM',
                      'PRIEST_RAPIDS', 'MCNARY', 'JOHN_DAY', 'THE_DALLES', 'BONNEVILLE']),
    ('Snake', ['PALISADES', 'AMERICAN_FALLS', 'BROWNLEE', 'OXBOW', 'HELLS_CANYON', 'DWORSHAK', 'LOWER_GRANITE',
               'LITTLE_GOOSE', 'LOWER_MONUMENTAL', 'ICE_HARBOR']),
    ('Cascades', ['ROSS', 'MOSSYROCK', 'SWIFT', 'DETROIT', 'LOOKOUT_POINT', 'ROUND_BUTTE']),
]
# One hue light to dark for amounts, a second hue for the snowfall share, blue-grey-red around 0 °C
# (and around the mean for anomalies, blue for wet).
BLUE_RAMP = [SURFACE, '#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#1c5cab', '#0d366b']
ORANGE_RAMP = [SURFACE, '#fde3d6', '#f9bda2', '#f4966e', '#eb6834', '#c94f22', '#9c3a15']
COLD_WARM = ['#0d366b', '#2a78d6', '#9ec5f4', '#f0efec', '#f4a3a2', '#e34948', '#8a1a1a']
MONTH_LETTERS = list('JFMAMJJASOND')


def catchments(geojson):
    """{project: dict(name, lat, lon, area, rings)} from a dam_catchments GeoJSON."""
    found = {}
    for feature in json.loads(Path(geojson).read_text())['features']:
        properties, geometry = feature['properties'], feature['geometry']
        parts = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
        name = properties.get('name') or properties['id']
        if '(' in name and ')' in name:   # a short name in parentheses, for example Seli's Ksanka Qlispe (Kerr)
            name = name[name.index('(') + 1:name.index(')')]
        found[properties['id']] = dict(name=name, lat=float(properties['lat']), lon=float(properties['lon']),
                                       area=float(properties.get('area_local') or properties.get('area') or 0),
                                       rings=[np.array(part[0], dtype=float) for part in parts])
    return found


def arrange(projects, meta):
    """Row order and (group title, row count) pairs: listed GROUPS first, then the rest north to south."""
    rows, groups = [], []
    for title, members in GROUPS:
        present = [project for project in members if project in projects]
        if present:
            rows.extend(present)
            groups.append((title, len(present)))
    rest = sorted((project for project in projects if project not in rows), key=lambda project: -meta[project]['lat'])
    if rest:
        rows.extend(rest)
        groups.append(('Other' if groups else '', len(rest)))
    return rows, groups


def span(years):
    """'1997–2006 and 2025' for years with gaps, '1997–2025' when continuous."""
    runs, start = [], years[0]
    for year, following in zip(years, list(years[1:]) + [None]):
        if following != year + 1:
            runs.append(f'{start}–{year}' if year != start else str(year))
            start = following
    return ' and '.join(filter(None, [', '.join(runs[:-1]), runs[-1]]))


COMPLETE_DAYS = 350   # a year counts for every-catchment figures when each catchment has this many days


def year_counts(series_by_project, key, water=False):
    """{year: {project: days with a value}} by calendar year, or by water year."""
    counts = {}
    for project, series in series_by_project.items():
        for day in (series[key][0] if key in series else []):
            year = water_year(day) if water else day.year
            counts.setdefault(year, {}).setdefault(project, 0)
            counts[year][project] += 1
    return counts


def complete_years(counts, projects):
    """Years in which every catchment has at least COMPLETE_DAYS days."""
    return sorted(year for year, by_project in counts.items()
                  if all(by_project.get(project, 0) >= COMPLETE_DAYS for project in projects))


def monthly(days, values, years, amount):
    """Twelve monthly totals (amounts) or means (states), averaged over the calendar YEARS."""
    keep = np.isin([day.year for day in days], years)
    months, kept = np.array([day.month for day in days])[keep], values[keep]
    if amount:
        return np.array([kept[months == month].sum() for month in range(1, 13)]) / len(years)
    return np.array([kept[months == month].mean() if (months == month).any() else np.nan for month in range(1, 13)])


def catchment_figure(n_rows, width, years=None):
    plt = setup()
    plt.rcParams.update({'axes.titlesize': 11, 'axes.titleweight': 'bold', 'axes.titlelocation': 'left'})
    height = max(4.5, .25 * n_rows + 2.6)
    return plt, (width if years is None else max(width, 4.2 + .3 * len(years)), height)


def ramp(colors, name='ramp'):
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(name, colors)


def around_zero(values, low=-1, high=1):
    from matplotlib.colors import TwoSlopeNorm
    return TwoSlopeNorm(0, min(low, np.floor(np.nanmin(values))), max(high, np.ceil(np.nanmax(values))))


def row_axis(ax, names, groups):
    """Catchment names as y ticks, a surface-coloured gap between river systems."""
    ax.set_yticks(range(len(names)), names, fontsize=8.5)
    ax.set_ylim(len(names) - .5, -.5)
    ax.tick_params(length=0)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    edge = 0
    for _, count in groups[:-1]:
        edge += count
        ax.axhline(edge - .5, color=SURFACE, linewidth=2.5)


def group_labels(ax, groups):
    """River-system titles beside the rows, on a label-only axis with the same row scale."""
    ax.axis('off')
    ax.set_ylim(sum(count for _, count in groups) - .5, -.5)
    edge = 0
    for title, count in groups:
        ax.text(1, edge + count / 2 - .5, title, transform=ax.get_yaxis_transform(), ha='right', va='center',
                fontsize=9, color=INK_SECONDARY, fontweight='bold', linespacing=1.1)
        edge += count


def heat_bar(fig, image, ax, label):
    bar = fig.colorbar(image, ax=ax, orientation='horizontal', fraction=.04, pad=.03, aspect=35)
    bar.set_label(label, color=INK_SECONDARY, fontsize=9)
    bar.outline.set_visible(False)
    bar.ax.tick_params(labelsize=8)


def climate(series_by_project, source, rows, years, variables):
    """Annual totals or means and twelve monthly values of each VARIABLE for each catchment over YEARS."""
    found = {}
    for project in rows:
        series, annual, months = series_by_project[project], {}, {}
        for variable in variables:
            if (source, variable) not in series:
                continue
            days, values = series[source, variable]
            kept = values[np.isin([day.year for day in days], years)]
            amount = variable in AMOUNTS
            annual[variable] = kept.sum() / len(years) if amount else kept.mean()
            months[variable] = monthly(days, values, years, amount)
        found[project] = dict(annual=annual, months=months)
    return found


def place_labels(ax, fig, points, labels):
    """Each label beside its point, in the first of six positions that covers no other point or label."""
    import matplotlib.patheffects as effects
    fig.canvas.draw()
    display = ax.transData.transform(points)
    renderer, taken = fig.canvas.get_renderer(), []
    for index, text in labels:
        others = np.delete(display, index, axis=0)
        best = None
        for dx, dy in ((6, 2), (-6, 2), (0, 8), (0, -12), (6, -10), (-6, -10)):
            artist = ax.annotate(text, points[index], xytext=(dx, dy), textcoords='offset points',
                                 ha='left' if dx > 0 else 'right' if dx < 0 else 'center', fontsize=8.5, color=INK,
                                 path_effects=[effects.withStroke(linewidth=2.5, foreground=SURFACE)])
            box = artist.get_window_extent(renderer).expanded(1.15, 1.4)
            hits = sum(box.contains(x, y) for x, y in others) + sum(box.overlaps(other) for other in taken)
            if best is None or hits < best[0]:
                if best is not None:
                    best[1].remove()
                best = (hits, artist, box)
            else:
                artist.remove()
            if hits == 0:
                break
        taken.append(best[2])


def plot_map(values, meta, rows, title):
    from matplotlib.collections import PolyCollection
    plt, size = catchment_figure(0, 17)
    fig, axes = plt.subplots(1, 3, figsize=(size[0], 7.4), layout='constrained')
    fig.get_layout_engine().set(h_pad=.15)
    precipitation = np.array([values[p]['annual']['precipitation_mm'] for p in rows])
    with np.errstate(invalid='ignore', divide='ignore'):
        share = np.where(precipitation > 0, 100 * np.array([values[p]['annual']['snowfall_mm'] for p in rows])
                         / precipitation, 0)
    temperature = np.array([values[p]['annual']['tmean_c'] for p in rows])
    panels = [('Precipitation', precipitation, ramp(BLUE_RAMP), 'mm per year', plt.Normalize(0, precipitation.max())),
              ('Snowfall share of precipitation', share, ramp(ORANGE_RAMP), '%',
               plt.Normalize(0, max(10, 10 * np.ceil(share.max() / 10)))),
              ('Mean air temperature', temperature, ramp(COLD_WARM), '°C', around_zero(temperature))]
    lons = np.concatenate([ring[:, 0] for p in rows for ring in meta[p]['rings']])
    lats = np.concatenate([ring[:, 1] for p in rows for ring in meta[p]['rings']])
    for ax, (name, colours, cmap, unit, norm) in zip(axes, panels):
        polygons = [ring for p in rows for ring in meta[p]['rings']]
        shades = np.array([value for p, value in zip(rows, colours) for _ in meta[p]['rings']])
        patches = PolyCollection(polygons, array=shades, cmap=cmap, norm=norm, edgecolor=SURFACE, linewidth=.5)
        ax.add_collection(patches)
        ax.scatter([meta[p]['lon'] for p in rows], [meta[p]['lat'] for p in rows], s=9, color=INK, zorder=3,
                   linewidths=0)
        ax.set_xlim(lons.min() - .6, lons.max() + .6)
        ax.set_ylim(lats.min() - .3, lats.max() + .3)
        ax.set_aspect(1 / np.cos(np.radians(lats.mean())))
        ax.set_title(name, pad=12)
        ax.grid(False)
        ax.set_xticks([x for x in range(-180, 181, 4) if lons.min() <= x <= lons.max()])
        ax.set_yticks([y for y in range(-90, 91, 4) if lats.min() <= y <= lats.max()])
        ax.xaxis.set_major_formatter(lambda x, _: f'{abs(x):g}°{"W" if x < 0 else "E"}')
        ax.yaxis.set_major_formatter(lambda y, _: f'{abs(y):g}°{"S" if y < 0 else "N"}')
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        heat_bar(fig, patches, ax, unit)
    fig.suptitle(title, x=.01, ha='left', fontsize=13, color=INK)
    # Name the largest catchments and the wettest and driest one.
    named = set(sorted(rows, key=lambda p: -meta[p]['area'])[:4]) | {rows[int(precipitation.argmax())],
                                                                      rows[int(precipitation.argmin())]}
    place_labels(axes[0], fig, np.array([(meta[p]['lon'], meta[p]['lat']) for p in rows]),
                 [(rows.index(p), meta[p]['name']) for p in sorted(named, key=rows.index)])
    return fig


def plot_seasons(values, meta, rows, groups, title):
    plt, size = catchment_figure(len(rows), 16)
    fig = plt.figure(figsize=size, layout='constrained')
    fig.get_layout_engine().set(h_pad=.15)
    label_axis, *axes = fig.subplots(1, 4, width_ratios=[.35, 1, 1, 1])
    precipitation = np.array([values[p]['months']['precipitation_mm'] for p in rows])
    with np.errstate(invalid='ignore', divide='ignore'):
        share = np.where(precipitation > 0, 100 * np.array([values[p]['months']['snowfall_mm'] for p in rows])
                         / precipitation, 0)
    temperature = np.array([values[p]['months']['tmean_c'] for p in rows])
    panels = [('Precipitation', precipitation, ramp(BLUE_RAMP), 'mm per month',
               plt.Normalize(0, np.nanpercentile(precipitation, 99))),
              ('Snowfall share', share, ramp(ORANGE_RAMP), '% of precipitation', plt.Normalize(0, 100)),
              ('Air temperature', temperature, ramp(COLD_WARM), '°C', around_zero(temperature))]
    names = [meta[p]['name'] for p in rows]
    for index, (ax, (name, grid, cmap, unit, norm)) in enumerate(zip(axes, panels)):
        image = ax.imshow(grid, aspect='auto', cmap=cmap, norm=norm, interpolation='nearest')
        ax.set_xticks(range(12), MONTH_LETTERS, fontsize=8.5)
        ax.set_title(name, pad=8)
        row_axis(ax, names if index == 0 else [''] * len(names), groups)
        heat_bar(fig, image, ax, unit)
    group_labels(label_axis, groups)
    fig.suptitle(title, x=.01, ha='left', fontsize=13, color=INK)
    return fig


def plot_anomaly(series_by_project, meta, rows, groups, key, title):
    counts = year_counts(series_by_project, key, water=True)
    years = complete_years(counts, rows)
    if len(years) < 2:
        raise ValueError('The anomaly figure needs at least two water years complete for every catchment')
    totals = np.zeros((len(rows), len(years)))
    for i, project in enumerate(rows):
        days, values = series_by_project[project][key]
        year_of = np.array([water_year(day) for day in days])
        totals[i] = [values[year_of == year].sum() for year in years]
    anomaly = 100 * totals / totals.mean(axis=1, keepdims=True) - 100
    plt, size = catchment_figure(len(rows), 9, years)
    fig = plt.figure(figsize=size, layout='constrained')
    label_axis, ax = fig.subplots(1, 2, width_ratios=[.4, 1])
    limit = max(20, 10 * np.ceil(np.abs(anomaly).max() / 10))
    from matplotlib.colors import TwoSlopeNorm
    image = ax.imshow(anomaly, aspect='auto', cmap=ramp(COLD_WARM[::-1]), norm=TwoSlopeNorm(0, -limit, limit),
                      interpolation='nearest')
    ax.set_xticks(range(len(years)), [str(year) for year in years], fontsize=8.5, rotation=90)
    ax.set_xlabel('Water year (October to September)')
    row_axis(ax, [meta[p]['name'] for p in rows], groups)
    for j, year in enumerate(years):
        expected = 366 if calendar.isleap(year) else 365   # the water year holds February of YEAR
        for i, project in enumerate(rows):
            if counts[year][project] < expected:
                ax.plot(j, i, 'o', markersize=3, color=INK, markeredgecolor=SURFACE, markeredgewidth=.6)
    heat_bar(fig, image, ax, f'% of the {span(years)} mean')
    group_labels(label_axis, groups)
    fig.suptitle(title, x=.01, ha='left', fontsize=13, color=INK)
    return fig


def plot_annual(values, meta, rows, et_key, title):
    order = sorted(rows, key=lambda p: -values[p]['annual']['precipitation_mm'])
    rain = np.array([values[p]['annual']['rainfall_mm'] for p in order])
    snow = np.array([values[p]['annual']['snowfall_mm'] for p in order])
    plt, size = catchment_figure(len(rows), 11)
    fig, ax = plt.subplots(figsize=size, layout='constrained')
    y, gap = np.arange(len(order)), .004 * (rain + snow).max()   # a sliver of surface between the two fills
    ax.barh(y, rain, height=.72, color=SERIES[0], label='Rain')
    ax.barh(y, snow, height=.72, left=rain + gap, color=SERIES[1], label='Snow, water equivalent')
    if et_key:
        et = [values[p]['annual'].get(et_key[1], np.nan) for p in order]
        ax.scatter(et, y, s=34, facecolor=SURFACE, edgecolor=INK, linewidths=1.3, zorder=4,
                   label=lowercase_first(label(et_key[1])[0]).capitalize())
    ax.set_yticks(y, [meta[p]['name'] for p in order], fontsize=8.5)
    ax.set_ylim(len(order) - .4, -.6)
    ax.tick_params(length=0)
    ax.grid(axis='x', color=GRID, linewidth=.6)
    ax.set_axisbelow(True)
    for side in ('left', 'top', 'right'):
        ax.spines[side].set_visible(False)
    ax.set_xlabel('mm per year')
    ax.xaxis.set_major_formatter(lambda value, _: f'{value:,.0f}')
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[-2:] + handles[:-2], labels[-2:] + labels[:-2], loc='lower left', bbox_to_anchor=(0, 1),
              ncols=3, handlelength=1.4, columnspacing=1.5)
    fig.suptitle(title, x=.01, ha='left', fontsize=13, color=INK)
    return fig


def plot_event(series_by_project, meta, rows, groups, key, start, end, title):
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    grid = np.full((len(rows), len(days)), np.nan)
    for i, project in enumerate(rows):
        have, values = series_by_project[project][key]
        slots = {day: j for j, day in enumerate(days)}
        for day, value in zip(have, values):
            if day in slots:
                grid[i, slots[day]] = value
    name, units = label(key[1])
    if units == '°C':
        cmap, norm = ramp(COLD_WARM), around_zero(grid)
    else:
        low = 0 if key[1] in AMOUNTS else np.nanmin(grid)
        cmap, norm = ramp(BLUE_RAMP), __import__('matplotlib').colors.Normalize(low, np.nanpercentile(grid, 99.5))
    plt, size = catchment_figure(len(rows), max(8, 3 + .35 * len(days)))
    fig = plt.figure(figsize=size, layout='constrained')
    label_axis, ax = fig.subplots(1, 2, width_ratios=[.4, 1])
    image = ax.imshow(grid, aspect='auto', cmap=cmap, norm=norm, interpolation='nearest')
    step = max(1, int(np.ceil(len(days) / 8)))
    ax.set_xticks(range(0, len(days), step), [day.strftime('%-d %b') for day in days[::step]], fontsize=8.5,
                  rotation=0)
    ax.set_xlabel(str(start.year) if start.year == end.year else f'{start.year}–{end.year}')
    row_axis(ax, [meta[p]['name'] for p in rows], groups)
    heat_bar(fig, image, ax, units + (' per day' if key[1] in AMOUNTS else ''))
    group_labels(label_axis, groups)
    fig.suptitle(title, x=.01, ha='left', fontsize=13, color=INK)
    return fig


KIND_VARIABLES = {
    'map': ['precipitation_mm', 'snowfall_mm', 'tmean_c'], 'seasons': ['precipitation_mm', 'snowfall_mm', 'tmean_c'],
    'anomaly': ['precipitation_mm'],
    'annual': ['precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'pet_hargreaves_mm', 'actual_evapotranspiration_mm'],
}


def plot_catchments(kind, path, geojson, source, variable, start, end):
    """Load every catchment and draw one of CATCHMENT_KINDS."""
    meta = catchments(geojson)
    variables = [variable] if kind == 'event' else KIND_VARIABLES[kind]
    series_by_project = load_many(path, variables, start, end, set(meta))
    required = variables[:1] if kind == 'event' else [v for v in variables if v not in
                                                      ('pet_hargreaves_mm', 'actual_evapotranspiration_mm')]
    if source is None:
        source = next((s for s in SOURCE_NAMES if any((s, required[0]) in v for v in series_by_project.values())), None)
    keys = [(source, v) for v in required]
    projects = [p for p, series in series_by_project.items() if all(key in series for key in keys)]
    if not projects:
        raise ValueError(f'No catchment in {path} has valid {SOURCE_NAMES.get(source, source)} values for {required}')
    rows, groups = arrange(projects, meta)
    heading = f'{len(rows)} catchments · {SOURCE_NAMES[source]}'
    if kind == 'event':
        if start is None or end is None:
            raise ValueError('--kind event needs --start and --end')
        name = label(variable)[0]
        when = f'{start:%-d %b} – {end:%-d %b %Y}' if start.year == end.year else f'{start:%-d %b %Y} – {end:%-d %b %Y}'
        return plot_event(series_by_project, meta, rows, groups, keys[0], start, end, f'{name}, {when} · {heading}')
    if kind == 'anomaly':
        return plot_anomaly(series_by_project, meta, rows, groups, keys[0], f'Water-year precipitation anomaly · {heading}')
    years = complete_years(year_counts(series_by_project, keys[0]), rows)
    if not years:
        raise ValueError('No calendar year is complete for every catchment')
    values = climate(series_by_project, source, rows, years, variables)
    heading = f'{heading} · {span(years)}'
    if kind == 'map':
        return plot_map(values, meta, rows, heading)
    if kind == 'seasons':
        return plot_seasons(values, meta, rows, groups, f'Seasonal cycle · {heading}')
    et_key = next(((source, v) for v in ('pet_hargreaves_mm', 'actual_evapotranspiration_mm')
                   if all((source, v) in series_by_project[p] for p in rows)), None)
    return plot_annual(values, meta, rows, et_key, f'Annual precipitation and evapotranspiration · {heading}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', type=Path, help='Working directory with source folders, or OUTPUT_full table')
    parser.add_argument('--project', help='Catchment project_id, for example MICA (overview, compare, wateryear)')
    parser.add_argument('--geojson', type=Path, help='Catchment polygons with id, name, lat and lon (map, seasons, anomaly)')
    parser.add_argument('--kind', choices=['overview', 'compare', 'wateryear', *CATCHMENT_KINDS], default='overview')
    parser.add_argument('--variable', default='precipitation_mm',
                        help='Variable name without source prefix (compare, wateryear, event)')
    parser.add_argument('--source', default=None, choices=list(SOURCE_NAMES),
                        help='Source for wateryear, map, seasons and anomaly (default: AORC if available, '
                             'else ERA5-Land, else ERA5)')
    parser.add_argument('--start', type=date.fromisoformat)
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--resample', choices=['auto', 'D', 'W', 'M'], default='auto',
                        help='Daily, weekly or monthly points (overview, compare); auto picks by span')
    parser.add_argument('--output', type=Path, help='PNG, PDF or SVG; default: figures/PROJECT_KIND[_VARIABLE].png')
    parser.add_argument('--dpi', type=int, default=170)
    args = parser.parse_args()
    if args.kind in CATCHMENT_KINDS:
        if not args.geojson:
            parser.error(f'--kind {args.kind} needs --geojson')
        if args.kind == 'event' and not (args.start and args.end):
            parser.error('--kind event needs --start and --end')
        fig = plot_catchments(args.kind, args.input, args.geojson, args.source, args.variable, args.start, args.end)
        output = args.output or Path('figures') / '_'.join(
            ['catchments', args.kind] + ([args.variable, str(args.start), str(args.end)] if args.kind == 'event' else [])
        ).__add__('.png')
    else:
        if not args.project:
            parser.error(f'--kind {args.kind} needs --project')
        if args.kind == 'overview':
            variables = {variable for _, keys in OVERVIEW for _, variable in keys}
        else:
            variables = {args.variable}
        series = load(args.input, args.project, variables, args.start, args.end)
        if not series:
            parser.error(f'No valid values for {args.project} in {args.input}')
        days = [d for d, _ in series.values()]
        period = f'{min(d[0] for d in days)} to {max(d[-1] for d in days)}'
        if args.kind == 'overview':
            rule = resolution(series, args.resample)
            fig = plot_overview(series, args.project, rule, f'{args.project} · {period}')
        elif args.kind == 'compare':
            rule = resolution(series, args.resample)
            fig = plot_compare(series, args.variable, rule, f'{args.project} · sources compared · {period}')
        else:
            source = args.source or next(s for s in SOURCE_NAMES if (s, args.variable) in series)
            fig = plot_water_years(series, args.variable, source, f'{args.project} · water years · {period}')
        output = args.output or Path('figures') / '_'.join(
            [args.project, args.kind] + ([] if args.kind == 'overview' else [args.variable])).__add__('.png')
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=args.dpi)
    print(f'Wrote {output}')


if __name__ == '__main__':
    main()
