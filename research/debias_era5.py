"""Correct ERA5-Land toward AORC by quantile mapping and fill the AORC columns where AORC has no data.

    python research/debias_era5.py --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/era5_2022_2026.csv --output outputs/catchment_daily_2022_2026.csv

--aorc is a full table with AORC columns (download_daily.py); --era5 is a
download_era5.py table or a full table. Each AORC variable gets its own mapping
per catchment and calendar month, fitted on the days both sources have in that
month and the months either side. AORC is kept wherever it exists, except in
the REPLACED catchments of delivery_variables.py: there every row takes the
mapped ERA5-Land, fitted on the --aorc table's AORC and ERA5-Land over the
REFERENCE dates. Corrected precipitation is split into rain and snow by a curve
of AORC's snow fraction against daily mean temperature, fitted for each
catchment and applied to the corrected temperature, and PET is recalculated
from the corrected temperatures.

OUTPUT has the requested-table columns for the --era5 period, with
aorc_gap_filled naming the filled or replaced columns of each row.
OUTPUT_check.csv tests the method: each year with AORC to fit on is corrected
with a fit on the other years and compared with AORC, beside the uncorrected
ERA5-Land.

--raw fills with ERA5-Land as it is, without the mapping, with its own rain and
snow; PET is recalculated from its temperatures.
"""

import argparse
from collections import Counter
import csv
from datetime import date
import json
import math
from pathlib import Path
import sys

import numpy as np

from check_daily import table_rows
from delivery_variables import AORC, LAND, REFERENCE, REPLACED, REQUESTED
from deliver_daily import ADDITIVE, FILL_COLUMN, RATIO, RATIO_LIMITS, companion
from export_daily import _write, digest
from meteorology import hargreaves_pet


MAPPED = sorted(ADDITIVE | RATIO)
VARIABLES = [item['variable'] for item in REQUESTED if item['source'] == AORC]
LEVELS = (np.arange(100) + .5) / 100
MIN_DAYS = 60
WET_MM = .1
CENTRES, WIDTHS = np.arange(-6, 6.01, .1), np.arange(.2, 4.01, .1)
CHECK_COLUMNS = ['variable', 'project_id', 'days', 'aorc_mean', 'era5_land_mean', 'corrected_mean',
                 'era5_land_mae', 'corrected_mae', 'aorc_p99', 'era5_land_p99', 'corrected_p99',
                 'aorc_wet_days', 'era5_land_wet_days', 'corrected_wet_days']


def load(path):
    manifest = json.loads(Path(str(path) + '.manifest.json').read_text())
    if digest(path) != manifest['output_sha256']:
        raise ValueError(f'{path.name}: hash differs from its manifest')
    columns = {(info.get('source'), info.get('variable')): column
               for column, info in manifest['columns'].items() if 'source' in info}
    return manifest, columns


def read(path, manifest, wanted):
    """Keys and float arrays of the wanted columns, in table order."""
    keys, values = [], {column: [] for column in wanted}
    for record in table_rows(path, manifest['columns']):
        keys.append((str(record['date']), record['project_id']))
        for column in wanted:
            value = record[column]
            values[column].append(math.nan if value is None or value == '' else float(value))
    return keys, {column: np.array(series) for column, series in values.items()}


def fit(x, y, projects, months, rows):
    """Quantile pairs per (project, month), from the paired rows of that month and the months either side."""
    maps = {}
    for project in np.unique(projects):
        mine = rows[projects[rows] == project]
        for month in range(1, 13):
            near = mine[np.isin(months[mine], [(month - 2) % 12 + 1, month, month % 12 + 1])]
            if len(near) < MIN_DAYS:
                raise ValueError(f'{project}, month {month}: {len(near)} days with both sources; need {MIN_DAYS}')
            xq, index = np.unique(np.quantile(x[near], LEVELS), return_inverse=True)
            maps[project, month] = xq, np.bincount(index, np.quantile(y[near], LEVELS)) / np.bincount(index)
    return maps


def mapped(v, xq, yq, ratio):
    """Interpolate between quantiles. Beyond them signed quantities shift by the outermost difference;
    nonnegative ones run linearly to zero below and scale by the top ratio above, and zero stays zero."""
    if not ratio:
        result = np.interp(v, xq, yq)
        result[v < xq[0]] = v[v < xq[0]] + yq[0] - xq[0]
        result[v > xq[-1]] = v[v > xq[-1]] + yq[-1] - xq[-1]
        return result
    if xq[0] > 0:
        xq, yq = np.r_[0., xq], np.r_[0., yq]
    result = np.interp(v, xq, yq)
    high = v > xq[-1]
    result[high] = v[high] * (np.clip(yq[-1] / xq[-1], *RATIO_LIMITS) if xq[-1] > 0 else 1.)
    result[v <= 0] = 0.
    return result


def snow_fraction(temperature, centre, width):
    return 1 / (1 + np.exp((temperature - centre) / width))


def fit_phase(aorc, projects, rows):
    """Per catchment, the logistic curve of AORC's snow fraction against daily mean temperature.

    Returns {project: (centre, width)} in degC: half the precipitation falls as snow at the
    centre. The fit weights each wet day by its precipitation, so large events count most.
    """
    p, s, t = (aorc[name][rows] for name in ('precipitation_mm', 'snowfall_mm', 'tmean_c'))
    wet = (p > WET_MM) & np.isfinite(s) & np.isfinite(t)
    centre, width = (grid.ravel() for grid in np.meshgrid(CENTRES, WIDTHS, indexing='ij'))
    curves = {}
    for project in np.unique(projects[rows]):
        at = wet & (projects[rows] == project)
        if at.sum() < MIN_DAYS:
            raise ValueError(f'{project}: {at.sum()} wet days with AORC; need {MIN_DAYS} for the snow fraction')
        fraction = np.clip(s[at] / p[at], 0, 1)
        error = (p[at] * (snow_fraction(t[at], centre[:, None], width[:, None]) - fraction) ** 2).sum(axis=1)
        best = np.argmin(error)
        curves[project] = centre[best], width[best]
    return curves


def finish(values, land, projects, dates, latitudes, phase=None):
    """Rain and snow, ordered temperatures, RH within 0-100 % and PET from the given values.

    Snow is the precipitation times the phase curve of the catchment at the daily mean
    temperature, or without curves, times ERA5-Land's own snow fraction.
    """
    if phase is None:
        with np.errstate(divide='ignore', invalid='ignore'):
            fraction = np.where(land['precipitation_mm'] > 0,
                                np.clip(land['snowfall_mm'] / land['precipitation_mm'], 0, 1), 0.)
    else:
        centre, width = np.array([phase[project] for project in projects]).reshape(-1, 2).T
        fraction = snow_fraction(values['tmean_c'], centre, width)
    values['snowfall_mm'] = values['precipitation_mm'] * fraction
    values['rainfall_mm'] = values['precipitation_mm'] - values['snowfall_mm']
    values['tmin_c'] = np.fmin(values['tmin_c'], values['tmean_c'])
    values['tmax_c'] = np.fmax(values['tmax_c'], values['tmean_c'])
    values['relative_humidity_pct'] = np.clip(values['relative_humidity_pct'], 0, 100)
    pet = np.full(len(dates), np.nan)
    for i, (project, day) in enumerate(zip(projects, dates)):
        stats = [values[name][i] for name in ('tmean_c', 'tmin_c', 'tmax_c')]
        if project in latitudes and all(math.isfinite(value) for value in stats):
            pet[i] = hargreaves_pet(*stats, latitudes[project], day)
    values['pet_hargreaves_mm'] = pet
    return values


def correct(aorc, land, projects, dates, latitudes, train, target):
    """Corrected values on the target rows, mapped with a fit on the train rows."""
    months = np.array([day.month for day in dates])
    values = {}
    for variable in MAPPED:
        x, y = land[variable], aorc[variable]
        maps = fit(x, y, projects, months, train[np.isfinite(x[train]) & np.isfinite(y[train])])
        result = np.full(len(target), np.nan)
        for key in set(zip(projects[target], months[target])):
            at = (projects[target] == key[0]) & (months[target] == key[1])
            result[at] = mapped(x[target[at]], *maps[key], variable in RATIO)
        values[variable] = result
    subset = {name: array[target] for name, array in land.items()}
    return finish(values, subset, projects[target], [dates[i] for i in target], latitudes,
                  fit_phase(aorc, projects, train))


def uncorrected(land, projects, dates, latitudes, target):
    """ERA5-Land on the target rows as it is, finished like the corrected values."""
    subset = {name: array[target] for name, array in land.items()}
    return finish({v: subset[v] for v in MAPPED}, subset, projects[target], [dates[i] for i in target], latitudes)


def fitting(projects, count):
    """Rows a mapping may be fitted on: the first count (the table rows), and for REPLACED
    catchments instead the REFERENCE rows after them (see inputs)."""
    return np.flatnonzero(np.isin(projects, REPLACED) == (np.arange(len(projects)) >= count))


def paired_rows(aorc, land, train=None):
    """Rows with both sources for every mapped variable, among train if given."""
    paired = np.flatnonzero(np.logical_and.reduce([np.isfinite(aorc[v]) & np.isfinite(land[v]) for v in MAPPED]))
    return paired if train is None else np.intersect1d(paired, train)


def held_out(aorc, land, projects, dates, latitudes, train=None):
    """Corrected values on every row: a year with AORC is mapped with a fit on the other years.

    train limits the rows the mapping is fitted on (default: every row)."""
    paired = paired_rows(aorc, land, train)
    years = np.array([day.year for day in dates])
    result = {v: np.full(len(dates), np.nan) for v in VARIABLES}
    for year in sorted(set(years)):
        target = np.flatnonzero(years == year)
        values = correct(aorc, land, projects, dates, latitudes, paired[years[paired] != year], target)
        for v in VARIABLES:
            result[v][target] = values[v]
    return result


def statistics(variable, project, a, r, c):
    """One check row from paired AORC, raw ERA5-Land and corrected values."""
    wet = lambda x: float(np.mean(x > WET_MM)) if variable == 'precipitation_mm' else ''
    return [variable, project, len(a), a.mean(), r.mean(), c.mean(), np.abs(r - a).mean(), np.abs(c - a).mean(),
            *np.percentile([a, r, c], 99, axis=1), wet(a), wet(r), wet(c)]


def check(aorc, land, projects, dates, latitudes, path, train=None):
    """Correct each overlap year with a fit on the others and compare with AORC, on the train rows if given."""
    held = paired_rows(aorc, land, train)
    years = {dates[i].year for i in held}
    if len(years) < 2:
        print('Fewer than two overlap years; no check written', flush=True)
        return
    corrected = held_out(aorc, land, projects, dates, latitudes, train)
    raw = uncorrected(land, projects, dates, latitudes, held)
    rows = []
    for v in VARIABLES:
        a, r, c = aorc[v][held], raw[v], corrected[v][held]
        ok = np.isfinite(a) & np.isfinite(r) & np.isfinite(c)
        for project in [*sorted(set(projects[held])), 'ALL']:
            at = ok & ((projects[held] == project) if project != 'ALL' else True)
            if at.any():
                rows.append(statistics(v, project, a[at], r[at], c[at]))
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(CHECK_COLUMNS)
        writer.writerows([[f'{x:.6g}' if isinstance(x, float) else x for x in row] for row in rows])
    print(f'Check of {len(years)} overlap years ({min(years)}-{max(years)}): {path}', flush=True)


def inputs(aorc_path, era5_path):
    """Rows of the --era5 table with its requested columns, and AORC, ERA5-Land and PET latitudes on them.

    Returns keys, columns ({(source, variable): full-table column}), data (column arrays), aorc and
    land ({variable: array}), projects, dates and latitudes. For the REPLACED catchments, aorc, land,
    projects and dates run on past the table rows with the --aorc table's own AORC and ERA5-Land on
    the REFERENCE dates, which their mapping is fitted on.
    """
    aorc_path, era5_path = Path(aorc_path), Path(era5_path)
    aorc_manifest, aorc_columns = load(aorc_path)
    era5_manifest, era5_columns = load(era5_path)
    land_names = [*MAPPED, 'snowfall_mm']
    columns = {}
    for item in REQUESTED:
        key = item['source'], item['variable']
        columns[key] = aorc_columns.get(key) if item['source'] == AORC else era5_columns.get(key)
    missing = [f'{source} {variable}' for (source, variable), column in columns.items() if column is None]
    missing += [f'{LAND} {v}' for v in land_names if (LAND, v) not in era5_columns]
    if missing:
        raise ValueError(f'Missing columns: {", ".join(missing)}')
    other = [column for (source, _), column in columns.items() if source != AORC]
    keys, data = read(era5_path, era5_manifest, sorted({*other, *(era5_columns[LAND, v] for v in land_names)}))
    index = {key: i for i, key in enumerate(keys)}
    aorc = {v: np.full(len(keys), np.nan) for v in VARIABLES}
    replaced = set(REPLACED) & {project for _, project in keys}
    start, end = REFERENCE
    wanted = {**{(AORC, v): columns[AORC, v] for v in VARIABLES},
              **{(LAND, v): aorc_columns.get((LAND, v)) for v in land_names}}
    if replaced and None in wanted.values():
        raise ValueError(f'{aorc_path.name} has no ERA5-Land columns to map {", ".join(sorted(replaced))} '
                         f'with a fit on {start} to {end}')
    extra, reference = [], {key: [] for key in wanted}
    number = lambda value: math.nan if value is None or value == '' else float(value)
    for record in table_rows(aorc_path, aorc_manifest['columns']):
        key = (str(record['date']), record['project_id'])
        i = index.get(key)
        if i is not None:
            for v in VARIABLES:
                aorc[v][i] = number(record[columns[AORC, v]])
        if key[1] in replaced and start <= key[0] <= end:
            extra.append(key)
            for name, column in wanted.items():
                reference[name].append(number(record[column]))
    found = Counter(project for _, project in extra)
    for project in sorted(replaced):
        if found[project] < MIN_DAYS:
            raise ValueError(f'{project}: {found[project]} days from {start} to {end} in {aorc_path.name}; '
                             'its AORC columns are replaced with a fit on these days')
    aorc = {v: np.r_[aorc[v], reference[AORC, v]] for v in VARIABLES}
    land = {v: np.r_[data[era5_columns[LAND, v]], reference[LAND, v]] for v in land_names}
    projects = np.array([project for _, project in keys + extra])
    dates = [date.fromisoformat(day) for day, _ in keys + extra]
    latitudes = next((entry['metadata'].get('pet_centroid_latitudes') or {} for entry in aorc_manifest['inputs']
                      if entry['metadata'].get('source_id') == AORC), {})
    if set(projects) - set(latitudes):
        raise ValueError('The AORC table lacks PET centroid latitudes for some projects')
    return keys, columns, data, aorc, land, projects, dates, latitudes


def run(aorc_path, era5_path, output, raw=False):
    output = Path(output)
    keys, columns, data, aorc, land, projects, dates, latitudes = inputs(aorc_path, era5_path)
    count = len(keys)
    train = fitting(projects, count)
    replaced = np.isin(projects[:count], REPLACED)
    values = {v: aorc[v][:count].copy() for v in VARIABLES}
    filled = {v: np.zeros(count, bool) for v in VARIABLES}
    target = np.flatnonzero(replaced | np.logical_or.reduce([np.isnan(values[v]) for v in VARIABLES]))
    method = 'uncorrected ERA5-Land' if raw else 'quantile-mapped ERA5-Land'
    print(f'{count:,} rows, {count - len(target):,} with complete AORC kept; '
          f'filling {len(target):,} with {method}', flush=True)
    if replaced.any():
        print(f'AORC replaced on every row of {", ".join(sorted(set(projects[:count][replaced])))}'
              + ('' if raw else f'; fitted on {REFERENCE[0]} to {REFERENCE[1]}'), flush=True)
    if len(target):
        corrected = (uncorrected(land, projects, dates, latitudes, target) if raw
                     else correct(aorc, land, projects, dates, latitudes, train, target))
        for v in VARIABLES:
            use = replaced[target] | np.isnan(values[v][target])
            values[v][target[use]] = corrected[v][use]
            filled[v][target[use]] = np.isfinite(corrected[v][use])
    names = {key: column.split('__', 1)[1] for key, column in columns.items()}
    header = ['date', 'project_id', *names.values(), FILL_COLUMN]
    blank = None if output.suffix == '.parquet' else ''

    def rows():
        for i, (day, project) in enumerate(keys):
            row = [values[variable][i] if source == AORC else data[columns[source, variable]][i]
                   for source, variable in columns]
            flag = ';'.join(names[AORC, v] for v in VARIABLES if filled[v][i])
            yield [day, project, *[blank if math.isnan(x) else x for x in row], flag or blank]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.part')
    _write(temporary, output, header, rows(), text_columns={FILL_COLUMN})
    temporary.replace(output)
    counts = ', '.join(f'{names[AORC, v]} {int(filled[v].sum()):,}' for v in VARIABLES)
    print(f'Wrote {output}; filled values: {counts}', flush=True)
    check(aorc, land, projects, dates, latitudes, companion(output, '_check', '.csv'), train)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--aorc', required=True, type=Path, help='Full table with AORC columns (download_daily.py)')
    parser.add_argument('--era5', required=True, type=Path, help='Table written by download_era5.py, or a full table')
    parser.add_argument('--output', required=True, type=Path, help='Requested-variable table (.csv, .csv.gz or .parquet)')
    parser.add_argument('--raw', action='store_true', help='Fill with ERA5-Land as it is, without quantile mapping')
    args = parser.parse_args()
    try:
        run(args.aorc, args.era5, args.output, args.raw)
    except (ValueError, OSError) as error:
        print(f'Failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
