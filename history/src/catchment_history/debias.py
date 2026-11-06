"""Correct ERA5-Land toward AORC by quantile mapping and fill the AORC columns where AORC has no data.

    catchment-history debias --aorc outputs/catchment_daily_1996_2025_full.csv --era5 outputs/era5_2026_full.csv --output outputs/catchment_daily_2026.csv

--aorc is a full table with AORC and ERA5-Land columns (from download); --era5
is a full table of the dates to write, with or without AORC (download
--primary era5-land). Each AORC variable gets its own mapping per catchment and
calendar month, fitted on the --aorc table's AORC and ERA5-Land on the days both
have in that month and the months either side: over REFERENCE for the REPLACED
catchments of delivery_variables.py, over RECENT for the others. AORC is kept
wherever it exists, except in the REPLACED catchments, where every row takes
the mapped ERA5-Land, and from REPLACED_FROM, where precipitation, rain and
snow of every catchment do. Corrected precipitation is split into rain and snow by a
curve of AORC's snow fraction against daily mean temperature, fitted for each
catchment and applied to the corrected temperature, and PET is recalculated
from the corrected temperatures. Catchments the --aorc table does not hold
(north of AORC) take ERA5-Land as it is, as in a delivery made without AORC.

OUTPUT has the requested-table columns for the --era5 dates, with
aorc_gap_filled naming the filled or replaced columns of each row.
plot-debias tests the method.
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

from .check import table_rows
from .delivery_variables import (AORC, LAND, RECENT, REFERENCE, REPLACED, REPLACED_FROM, REPLACED_VARIABLES,
                                 REQUESTED)
from .deliver import FILL_COLUMN
from .export import _write
from .files import digest
from .meteorology import hargreaves_pet


# Beyond the fitted quantiles signed quantities shift; nonnegative ones scale, within RATIO_LIMITS (see mapped).
ADDITIVE = {'tmean_c', 'tmin_c', 'tmax_c', 'relative_humidity_pct'}
RATIO = {'precipitation_mm', 'shortwave_down_mean_wm2', 'vapor_pressure_deficit_kpa'}
RATIO_LIMITS = (.25, 4.)
MAPPED = sorted(ADDITIVE | RATIO)
VARIABLES = [item['variable'] for item in REQUESTED if item['source'] == AORC]
LEVELS = (np.arange(100) + .5) / 100
MIN_DAYS = 60
WET_MM = .1
# Candidate snow-fraction curves: (centre, width) in degC.
CURVES = np.array(np.meshgrid(np.arange(-6, 6.01, .1), np.arange(.2, 4.01, .1), indexing='ij')).reshape(2, -1).T
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


def month_groups(projects, dates):
    """Catchment names, and for each row the group number catchment * 12 + month - 1."""
    names, codes = np.unique(projects, return_inverse=True)
    return names, codes * 12 + np.array([day.month for day in dates]) - 1


def fit(x, y, names, groups, rows, needed=None):
    """Quantile pairs per group, from the paired rows of that month and the months either side;
    needed limits the groups fitted, else every catchment and month."""
    rows = rows[np.argsort(groups[rows], kind='stable')]
    bounds = np.searchsorted(groups[rows], np.arange(len(names) * 12 + 1))
    maps = {}
    for code, project in enumerate(names):
        for month in range(12):
            if needed is not None and code * 12 + month not in needed:
                continue
            near = np.concatenate([rows[bounds[group]:bounds[group + 1]]
                                   for group in code * 12 + np.arange(month - 1, month + 2) % 12])
            if len(near) < MIN_DAYS:
                raise ValueError(f'{project}, month {month + 1}: {len(near)} days with both sources; '
                                 f'need {MIN_DAYS}')
            xq, index = np.unique(np.quantile(x[near], LEVELS), return_inverse=True)
            maps[code * 12 + month] = xq, np.bincount(index, np.quantile(y[near], LEVELS)) / np.bincount(index)
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


def phase_errors(aorc, projects, rows, parts):
    """Per catchment and part of the rows (parts numbers them), the wet days and the error of every curve.

    The error sums, over the wet days, the squared difference between the curve and AORC's snow
    fraction, weighted by precipitation so large events count most.
    """
    p, s, t, own, part = (array[rows] for array in
                          (aorc['precipitation_mm'], aorc['snowfall_mm'], aorc['tmean_c'], projects, parts))
    wet = (p > WET_MM) & np.isfinite(s) & np.isfinite(t)
    errors = {}
    for project in np.unique(own):
        mine = wet & (own == project)
        errors[project] = {}
        for number in np.unique(part[mine]):
            at = mine & (part == number)
            fraction = np.clip(s[at] / p[at], 0, 1)
            error = (p[at] * (snow_fraction(t[at], *CURVES.T[:, :, None]) - fraction) ** 2).sum(axis=1)
            errors[project][number] = at.sum(), error
    return errors


def best_curves(errors, without=None):
    """{project: (centre, width)} with the least error over every part except without: the logistic
    curve of AORC's snow fraction against daily mean temperature, half snow at the centre."""
    curves = {}
    for project, parts in errors.items():
        kept = [value for number, value in parts.items() if number != without]
        days = sum(count for count, _ in kept)
        if days < MIN_DAYS:
            raise ValueError(f'{project}: {days} wet days with AORC; need {MIN_DAYS} for the snow fraction')
        curves[project] = tuple(CURVES[np.argmin(np.sum([error for _, error in kept], axis=0))])
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


def correct(aorc, land, projects, dates, latitudes, train, target, phase=None):
    """Corrected values on the target rows, mapped with a fit on the train rows; phase gives the
    snow-fraction curves if they are already fitted on those rows."""
    names, groups = month_groups(projects, dates)
    # Only the groups of the target rows: the catchments without AORC never reach here.
    needed = set(np.unique(groups[target]).tolist())
    values = {}
    for variable in MAPPED:
        x, y = land[variable], aorc[variable]
        maps = fit(x, y, names, groups, train[np.isfinite(x[train]) & np.isfinite(y[train])], needed)
        result = np.full(len(target), np.nan)
        for group in sorted(needed):
            at = groups[target] == group
            result[at] = mapped(x[target[at]], *maps[group], variable in RATIO)
        values[variable] = result
    if phase is None:
        phase = best_curves(phase_errors(aorc, projects, train, np.zeros(len(projects), int)))
    subset = {name: array[target] for name, array in land.items()}
    return finish(values, subset, projects[target], [dates[i] for i in target], latitudes, phase)


def uncorrected(land, projects, dates, latitudes, target):
    """ERA5-Land on the target rows as it is, finished like the corrected values."""
    subset = {name: array[target] for name, array in land.items()}
    return finish({v: subset[v] for v in MAPPED}, subset, projects[target], [dates[i] for i in target], latitudes)


def paired_rows(aorc, land, train):
    """Rows among train with both sources for every mapped variable."""
    paired = np.flatnonzero(np.logical_and.reduce([np.isfinite(aorc[v]) & np.isfinite(land[v]) for v in MAPPED]))
    return np.intersect1d(paired, train)


def held_out(aorc, land, projects, dates, latitudes, train):
    """Corrected values on every row: a year with AORC is mapped with a fit on the train rows of the other years."""
    paired = paired_rows(aorc, land, train)
    years = np.array([day.year for day in dates])
    # Each year's share of the snow-fraction error is computed once and left out in turn.
    errors = phase_errors(aorc, projects, paired, years)
    result = {v: np.full(len(dates), np.nan) for v in VARIABLES}
    for year in sorted(set(years)):
        target = np.flatnonzero(years == year)
        values = correct(aorc, land, projects, dates, latitudes, paired[years[paired] != year], target,
                         best_curves(errors, without=year))
        for v in VARIABLES:
            result[v][target] = values[v]
    return result


def statistics(variable, project, a, r, c):
    """One check row from paired AORC, raw ERA5-Land and corrected values."""
    wet = lambda x: float(np.mean(x > WET_MM)) if variable == 'precipitation_mm' else ''
    return [variable, project, len(a), a.mean(), r.mean(), c.mean(), np.abs(r - a).mean(), np.abs(c - a).mean(),
            *np.percentile([a, r, c], 99, axis=1), wet(a), wet(r), wet(c)]


def check(aorc, land, projects, dates, latitudes, train, corrected, path):
    """Compare the held-out corrected values (held_out) with AORC on the train rows, beside ERA5-Land."""
    held = paired_rows(aorc, land, train)
    years = {dates[i].year for i in held}
    if len(years) < 2:
        print('Fewer than two overlap years; no check written', flush=True)
        return
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


def fit_period(project):
    return REFERENCE if project in REPLACED else RECENT


def inputs(aorc_path, era5_path):
    """Rows of the --era5 table with its requested columns, and AORC, ERA5-Land and PET latitudes on them.

    Returns keys, columns ({(source, variable): full-table column}), data (column arrays), aorc and
    land ({variable: array}), projects, dates and latitudes. aorc, land, projects and dates run on
    past the table rows with the --aorc table's own AORC and ERA5-Land on the dates each catchment's
    mapping is fitted on (fit_period); those rows are the ones after the first len(keys).
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
    counterparts = [era5_columns[LAND, v] for v in VARIABLES if (LAND, v) in era5_columns]
    keys, data = read(era5_path, era5_manifest,
                      sorted({*other, *counterparts, *(era5_columns[LAND, v] for v in land_names)}))
    index = {key: i for i, key in enumerate(keys)}
    aorc = {v: np.full(len(keys), np.nan) for v in VARIABLES}
    present = {project for _, project in keys}
    wanted = {**{(AORC, v): columns[AORC, v] for v in VARIABLES},
              **{(LAND, v): aorc_columns.get((LAND, v)) for v in land_names}}
    if None in wanted.values():
        raise ValueError(f'{aorc_path.name} has no ERA5-Land columns to fit the mapping on')
    extra, reference, held = [], {key: [] for key in wanted}, set()
    number = lambda value: math.nan if value is None or value == '' else float(value)
    for record in table_rows(aorc_path, aorc_manifest['columns']):
        key = (str(record['date']), record['project_id'])
        held.add(key[1])
        i = index.get(key)
        if i is not None:
            for v in VARIABLES:
                aorc[v][i] = number(record[columns[AORC, v]])
        start, end = fit_period(key[1])
        if key[1] in present and start <= key[0] <= end:
            extra.append(key)
            for name, column in wanted.items():
                reference[name].append(number(record[column]))
    found = Counter(project for _, project in extra)
    for project in sorted(present & held):
        if found[project] < MIN_DAYS:
            start, end = fit_period(project)
            raise ValueError(f'{project}: {found[project]} days from {start} to {end} in {aorc_path.name}; '
                             'its mapping is fitted on these days')
    aorc = {v: np.r_[aorc[v], reference[AORC, v]] for v in VARIABLES}
    land = {v: np.r_[data[era5_columns[LAND, v]], reference[LAND, v]] for v in land_names}
    projects = np.array([project for _, project in keys + extra])
    dates = [date.fromisoformat(day) for day, _ in keys + extra]
    latitudes = {**next((entry['metadata'].get('pet_centroid_latitudes') or {} for entry in aorc_manifest['inputs']
                         if entry['metadata'].get('source_id') == AORC), {}),
                 **era5_manifest.get('pet_centroid_latitudes', {})}
    if set(projects) - set(latitudes):
        raise ValueError('The tables lack PET centroid latitudes for some projects')
    columns.update({(LAND, v): era5_columns[LAND, v] for v in VARIABLES if (LAND, v) in era5_columns})
    return keys, columns, data, aorc, land, projects, dates, latitudes


def run(aorc_path, era5_path, output):
    output = Path(output)
    keys, columns, data, aorc, land, projects, dates, latitudes = inputs(aorc_path, era5_path)
    count = len(keys)
    train = np.arange(count, len(projects))
    replaced = np.isin(projects[:count], REPLACED)
    north = ~np.isin(projects[:count], projects[count:])
    since = date.fromisoformat(REPLACED_FROM)
    values = {v: aorc[v][:count].copy() for v in VARIABLES}
    # Rows whose AORC precipitation is overwritten; rows without it are filled like any gap.
    stale = (~north & ~replaced & np.isfinite(values['precipitation_mm'])
             & np.array([day >= since for day in dates[:count]], bool))
    filled = {v: np.zeros(count, bool) for v in VARIABLES}
    target = np.flatnonzero(~north & (replaced | stale |
                                      np.logical_or.reduce([np.isnan(values[v]) for v in VARIABLES])))
    print(f'{count:,} rows, {count - len(target) - north.sum():,} with complete AORC kept; '
          f'filling {len(target):,} with quantile-mapped ERA5-Land', flush=True)
    if north.any():
        # As deliver.py writes a delivery without AORC: the ERA5-Land columns, and PET from their temperatures.
        print(f'ERA5-Land as it is for {", ".join(sorted(set(projects[:count][north])))}', flush=True)
        rows = np.flatnonzero(north)
        for v in VARIABLES:
            if (LAND, v) in columns:
                values[v][rows] = data[columns[LAND, v]][rows]
        stats = [data[columns[LAND, v]][rows] for v in ('tmean_c', 'tmin_c', 'tmax_c')]
        values['pet_hargreaves_mm'][rows] = [
            hargreaves_pet(*s, latitudes[projects[i]], dates[i]) if all(map(math.isfinite, s)) else math.nan
            for i, *s in zip(rows, *stats)]
    if replaced.any():
        print(f'AORC replaced on every row of {", ".join(sorted(set(projects[:count][replaced])))}; '
              f'fitted on {REFERENCE[0]} to {REFERENCE[1]}, the other catchments on {RECENT[0]} to {RECENT[1]}',
              flush=True)
    if stale.any():
        print(f'AORC {", ".join(REPLACED_VARIABLES)} replaced from {REPLACED_FROM} on {stale.sum():,} rows of '
              f'{len(set(projects[:count][stale]))} catchments', flush=True)
    if len(target):
        corrected = correct(aorc, land, projects, dates, latitudes, train, target)
        # Rows that keep AORC's temperature split the mapped precipitation at that temperature.
        own = stale[target] & np.isfinite(values['tmean_c'][target])
        if own.any():
            phase = best_curves(phase_errors(aorc, projects, train, np.zeros(len(projects), int)))
            centre, width = np.array([phase[project] for project in projects[target[own]]]).reshape(-1, 2).T
            snow = corrected['precipitation_mm'][own] * snow_fraction(values['tmean_c'][target[own]], centre, width)
            corrected['snowfall_mm'][own] = snow
            corrected['rainfall_mm'][own] = corrected['precipitation_mm'][own] - snow
        for v in VARIABLES:
            use = replaced[target] | np.isnan(values[v][target])
            if v in REPLACED_VARIABLES:
                use |= stale[target]
            values[v][target[use]] = corrected[v][use]
            filled[v][target[use]] = np.isfinite(corrected[v][use])
    requested = [(item['source'], item['variable']) for item in REQUESTED]
    names = {key: columns[key].split('__', 1)[1] for key in requested}
    header = ['date', 'project_id', *names.values(), FILL_COLUMN]
    blank = None if output.suffix == '.parquet' else ''

    def rows():
        for i, (day, project) in enumerate(keys):
            row = [values[variable][i] if source == AORC else data[columns[source, variable]][i]
                   for source, variable in requested]
            flag = ';'.join(names[AORC, v] for v in VARIABLES if filled[v][i])
            yield [day, project, *[blank if math.isnan(x) else x for x in row], flag or blank]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.part')
    _write(temporary, output, header, rows(), text_columns={FILL_COLUMN})
    temporary.replace(output)
    counts = ', '.join(f'{names[AORC, v]} {int(filled[v].sum()):,}' for v in VARIABLES)
    print(f'Wrote {output}; filled values: {counts}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='catchment-history debias', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--aorc', required=True, type=Path, help='Full table with AORC and ERA5-Land columns (catchment-history download)')
    parser.add_argument('--era5', required=True, type=Path, help='Full table of the dates to write')
    parser.add_argument('--output', required=True, type=Path, help='Requested-variable table (.csv, .csv.gz or .parquet)')
    args = parser.parse_args(argv)
    try:
        run(args.aorc, args.era5, args.output)
    except (ValueError, OSError) as error:
        print(f'Failed: {error}', file=sys.stderr)
        return 1
    return 0
