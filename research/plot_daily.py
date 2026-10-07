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
"""

import argparse
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


def load(path, project, variables, start=None, end=None):
    """Valid daily values as {(source, variable): (dates, values)} for one catchment."""
    path, wanted, found = Path(path), set(variables), {}

    def keep(source, variable, day, value):
        if variable in wanted and value not in ('', None) and (start is None or day >= start) and (
                end is None or day <= end):
            found.setdefault((source, variable), {})[day] = float(value)

    if path.is_dir():
        files = sorted(path.glob('*/daily_*.csv*')) + sorted(path.glob('daily_*.csv*'))
        if not files:
            raise ValueError(f'No daily source files under {path}')
        for file in files:
            opener = gzip.open if file.suffix == '.gz' else open
            with opener(file, 'rt', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row['project_id'] == project and row['qc'] == 'valid':
                        keep(row['source'], row['variable'], date.fromisoformat(row['date']), row['value'])
    else:
        from check_daily import table_rows
        manifest = json.loads(Path(str(path) + '.manifest.json').read_text())
        columns = {column: (info['source'], info['variable']) for column, info in manifest['columns'].items()
                   if info.get('variable') in wanted}
        for row in table_rows(path, manifest['columns']):
            if row['project_id'] == project:
                day = date.fromisoformat(str(row['date']))
                for column, (source, variable) in columns.items():
                    keep(source, variable, day, row[column])
    return {key: (np.array(sorted(values)), np.array([values[d] for d in sorted(values)]))
            for key, values in found.items()}


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', type=Path, help='Working directory with source folders, or OUTPUT_full table')
    parser.add_argument('--project', required=True, help='Catchment project_id, for example MICA')
    parser.add_argument('--kind', choices=['overview', 'compare', 'wateryear'], default='overview')
    parser.add_argument('--variable', default='precipitation_mm',
                        help='Variable name without source prefix (compare, wateryear)')
    parser.add_argument('--source', default=None, choices=list(SOURCE_NAMES),
                        help='Source for wateryear (default: AORC if available, else ERA5-Land, else ERA5)')
    parser.add_argument('--start', type=date.fromisoformat)
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--resample', choices=['auto', 'D', 'W', 'M'], default='auto',
                        help='Daily, weekly or monthly points (overview, compare); auto picks by span')
    parser.add_argument('--output', type=Path, help='PNG, PDF or SVG; default: figures/PROJECT_KIND[_VARIABLE].png')
    parser.add_argument('--dpi', type=int, default=170)
    args = parser.parse_args()
    if args.kind == 'overview':
        variables = {variable for _, keys in OVERVIEW for _, variable in keys}
    else:
        variables = {args.variable}
    series = load(args.input, args.project, variables, args.start, args.end)
    if not series:
        parser.error(f'No valid values for {args.project} in {args.input}')
    days = [d for d, _ in series.values()]
    span = f'{min(d[0] for d in days)} to {max(d[-1] for d in days)}'
    if args.kind == 'overview':
        rule = resolution(series, args.resample)
        fig = plot_overview(series, args.project, rule, f'{args.project} · {span}')
    elif args.kind == 'compare':
        rule = resolution(series, args.resample)
        fig = plot_compare(series, args.variable, rule, f'{args.project} · sources compared · {span}')
    else:
        source = args.source or next(s for s in SOURCE_NAMES if (s, args.variable) in series)
        fig = plot_water_years(series, args.variable, source, f'{args.project} · water years · {span}')
    output = args.output or Path('figures') / '_'.join(
        [args.project, args.kind] + ([] if args.kind == 'overview' else [args.variable])).__add__('.png')
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=args.dpi)
    print(f'Wrote {output}')


if __name__ == '__main__':
    main()
