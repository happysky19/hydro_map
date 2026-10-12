"""Check a daily delivery and its QC companion; write reports and diagnostic plots."""

import argparse
from collections import Counter
import csv
from datetime import date, timedelta
import gzip
from itertools import zip_longest
import json
from pathlib import Path
import sys
import warnings

import numpy as np

from export_daily import QUALITY, digest


SOURCES = ('aorc_v1.1', 'era5_land_cds')
# Daily catchment-mean plausibility limits; exceeding one is a failed check, not a correction.
PLAUSIBLE = {
    **{name: (0, 300) for name in ('precipitation_mm', 'rainfall_mm', 'snowfall_mm')},
    'snowmelt_mm': (0, 200), 'actual_evapotranspiration_mm': (-3, 12), 'pet_hargreaves_mm': (0, 12),
    **{name: (-60, 50) for name in ('tmean_c', 'tmin_c', 'tmax_c', 'wet_bulb_temperature_c')},
    **{f'soil_temperature_layer{i}_c': (-40, 45) for i in range(1, 5)},
    'specific_humidity_kgkg': (0, .03), 'relative_humidity_pct': (0, 105),
    'vapor_pressure_deficit_kpa': (0, 7), 'surface_pressure_pa': (50000, 105000),
    'u_wind_ms': (-30, 30), 'v_wind_ms': (-30, 30), 'wind_speed_ms': (0, 30),
    'shortwave_down_mean_wm2': (0, 450), 'longwave_down_mean_wm2': (100, 500),
    'net_shortwave_mean_wm2': (0, 400), 'net_longwave_mean_wm2': (-250, 50),
    'net_radiation_mean_wm2': (-200, 350), 'snow_water_equivalent_mm': (0, 3000),
    'snow_depth_m': (0, 10), 'snow_density_kgm3': (50, 600), 'snow_cover_pct': (0, 100),
    'perennial_snow_area_pct': (0, 100),
    **{f'soil_moisture_layer{i}_m3m3': (0, .8) for i in range(1, 5)},
    'root_zone_soil_moisture_0_100cm_m3m3': (0, .8), 'cloud_cover_fraction': (0, 1),
    # ERA5 GRIB packing stores 'no 0 degC level above ground' as -0.0002 m rather than 0.
    'freezing_level_above_ground_m': (-.001, 6000), 'freezing_level_above_sea_level_m': (0, 7000),
}
# Mean-ratio comparisons are only meaningful for nonnegative quantities.
SEASONS = {'DJF': (12, 1, 2), 'MAM': (3, 4, 5), 'JJA': (6, 7, 8), 'SON': (9, 10, 11)}
RATIO_VARIABLES = {'precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'specific_humidity_kgkg',
                   'surface_pressure_pa', 'wind_speed_ms', 'shortwave_down_mean_wm2',
                   'longwave_down_mean_wm2', 'vapor_pressure_deficit_kpa', 'relative_humidity_pct'}


def table_rows(path, columns):
    if path.suffix == '.parquet':
        import pyarrow.parquet as pq
        table = pq.ParquetFile(path)
        if set(table.schema.names) != set(columns):
            raise ValueError(f'{path.name}: columns differ from manifest')
        for batch in table.iter_batches(batch_size=2048):
            yield from batch.to_pylist()
    else:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', newline='') as stream:
            reader = csv.DictReader(stream)
            if (len(reader.fieldnames or []) != len(columns)
                    or set(reader.fieldnames or []) != set(columns)):
                raise ValueError(f'{path.name}: columns differ from manifest')
            yield from reader


def _number(value):
    return np.nan if value is None or value == '' else float(value)


def load_delivery(path):
    manifest = json.loads(Path(str(path)+'.manifest.json').read_text())
    if manifest.get('schema_version') != 2 or manifest.get('day') != 'UTC':
        raise ValueError('Requires a schema-version 2 UTC delivery with separate QC')
    qc_name = manifest['qc_file']
    if Path(qc_name).name != qc_name or qc_name == path.name:
        raise ValueError('QC must be a separate sibling file')
    qc_path = path.with_name(qc_name)
    for table, key in ((path, 'output_sha256'), (qc_path, 'qc_output_sha256')):
        if digest(table) != manifest[key]:
            raise ValueError(f'{table.name}: hash differs from delivery manifest')
    projects = sorted(manifest['project_metadata'])
    start, end = map(date.fromisoformat, (manifest['start'], manifest['end']))
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    columns = [name for name in manifest['columns'] if name not in ('date', 'project_id')]
    if (not days or not projects or not columns or manifest['project_count'] != len(projects)
            or manifest['row_count'] != len(days)*len(projects)):
        raise ValueError('Manifest dimensions do not define a complete project-day grid')
    # The 43-project, 30-year, 45-variable matrix occupies about 162 MiB.
    # QC strings are counted while streaming rather than retained for every row.
    values = np.full((len(days), len(projects), len(columns)), np.nan)
    counts, qc_errors = Counter(), []
    failure_count = 0
    row_count = 0
    for i, (row, quality) in enumerate(zip_longest(
            table_rows(path, manifest['columns']), table_rows(qc_path, manifest['qc_columns']))):
        if row is None or quality is None or i >= manifest['row_count']:
            raise ValueError('Values and QC row counts differ from the manifest')
        d, p = divmod(i, len(projects))
        key = (str(days[d]), projects[p])
        if any((r['date'], r['project_id']) != key for r in (row, quality)):
            raise ValueError(f'Values/QC keys do not align in date/project order at row {i+1}')
        for j, column in enumerate(columns):
            value = _number(row[column])
            qc, hours, expected, area = (quality[column+'__'+field] for field in QUALITY)
            counts[(projects[p], column, str(qc))] += 1
            try:
                hours, expected, area = map(float, (hours, expected, area))
                valid = (qc == 'valid' and hours == expected == 24 and 1-1e-9 <= area <= 1+1e-9)
                coherent = (isinstance(qc, str) and bool(qc.strip()) and 0 <= hours <= expected == 24
                            and hours.is_integer() and 0 <= area <= 1+1e-9
                            and ((valid and np.isfinite(value))
                                 or (qc != 'valid' and np.isnan(value))))
            except (TypeError, ValueError):
                valid, coherent = False, False
            if not coherent:
                failure_count += 1
                if len(qc_errors) < 5:
                    qc_errors.append(dict(date=key[0], project_id=key[1], column=column))
            if valid and np.isfinite(value):
                values[d, p, j] = value
        row_count += 1
    if row_count != manifest['row_count']:
        raise ValueError('Missing project-day rows')
    check = dict(check='QC/value agreement', evaluated=values.size, failed=failure_count,
                 examples=qc_errors)
    return manifest, days, projects, columns, values, counts, check


def consistency_checks(manifest, days, projects, columns, values):
    lookup = {(info['source'], info['variable']): i for i, column in enumerate(columns)
              for info in [manifest['columns'][column]]}
    checks = []

    def check(source, label, names, calculate, relation='equal'):
        if any((source, name) not in lookup for name in names):
            return
        arrays = [values[:, :, lookup[source, name]] for name in names]
        eligible = np.logical_and.reduce([np.isfinite(a) for a in arrays])
        left, right = calculate(*arrays)
        tolerance = 5e-5+1e-6*np.abs(right)
        bad = (np.abs(left-right) > tolerance if relation == 'equal'
               else left < right-tolerance) & eligible
        examples = [dict(date=str(days[d]), project_id=projects[p])
                    for d, p in np.argwhere(bad)[:5]]
        checks.append(dict(check=f'{source}: {label}', evaluated=int(eligible.sum()),
                           failed=int(bad.sum()), examples=examples))

    for source in sorted({s for s, _ in lookup}):
        check(source, 'minimum <= mean temperature', ['tmean_c', 'tmin_c'],
              lambda mean, low: (mean, low), 'greater')
        check(source, 'mean <= maximum temperature', ['tmax_c', 'tmean_c'],
              lambda high, mean: (high, mean), 'greater')
        check(source, 'wet bulb <= air temperature', ['tmean_c', 'wet_bulb_temperature_c'],
              lambda air, wet: (air, wet), 'greater')
        check(source, 'rain + snow = precipitation', ['rainfall_mm', 'snowfall_mm', 'precipitation_mm'],
              lambda rain, snow, total: (rain+snow, total))
        check(source, 'mean speed >= magnitude of mean vector',
              ['wind_speed_ms', 'u_wind_ms', 'v_wind_ms'],
              lambda speed, u, v: (speed, np.hypot(u, v)), 'greater')
        for kind in ('shortwave', 'longwave'):
            check(source, f'{kind} energy = mean flux * 0.0864',
                  [f'{kind}_down_energy_mjm2', f'{kind}_down_mean_wm2'],
                  lambda energy, flux: (energy, flux*.0864))
        check(source, 'net energy = mean flux * 0.0864',
              ['net_radiation_energy_mjm2', 'net_radiation_mean_wm2'],
              lambda energy, flux: (energy, flux*.0864))
        check(source, 'net shortwave + net longwave = net radiation',
              ['net_shortwave_energy_mjm2', 'net_longwave_energy_mjm2', 'net_radiation_energy_mjm2'],
              lambda sw, lw, total: (sw+lw, total))
        check(source, 'root-zone thickness weighting',
              ['root_zone_soil_moisture_0_100cm_m3m3']+
              [f'soil_moisture_layer{i}_m3m3' for i in (1, 2, 3)],
              lambda root, a, b, c: (root, .07*a+.21*b+.72*c))
    for (source, name), index in sorted(lookup.items()):
        units = manifest['columns'][columns[index]]['units']
        low, high = PLAUSIBLE.get(name, (0, 1) if units in ('m3/m3', 'kg/kg') else
                                  (-273.15, None) if units == 'degC' else (None, None))
        if low is not None:
            check(source, f'{name} lower bound', [name], lambda a, b=low: (a, b), 'greater')
        if high is not None:
            check(source, f'{name} upper bound', [name], lambda a, b=high: (b, a), 'greater')
    return checks, lookup


def comparison(x, y, project, variable, season='all'):
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    diff = y-x
    ratio = (float(y.mean()/x.mean()) if len(x) and variable in RATIO_VARIABLES
             and abs(x.mean()) > 1e-12 else None)
    return dict(project_id=project, variable=variable, season=season, paired_days=len(x),
                mean_aorc=float(x.mean()) if len(x) else None,
                mean_land=float(y.mean()) if len(x) else None,
                bias_land_minus_aorc=float(diff.mean()) if len(x) else None,
                ratio_land_to_aorc=ratio,
                mae=float(np.abs(diff).mean()) if len(x) else None,
                rmse=float(np.sqrt(np.mean(diff**2))) if len(x) else None,
                correlation=float(np.corrcoef(x, y)[0, 1])
                if len(x) >= 3 and np.ptp(x) > 1e-12 and np.ptp(y) > 1e-12 else None)


def shared_variables(lookup):
    return sorted({name for source, name in lookup if source == SOURCES[0]}
                  & {name for source, name in lookup if source == SOURCES[1]})


def persistent_snow(manifest, days, projects, columns, values, lookup):
    """Catchments whose daily SWE never falls below 50 mm in a full year (glacier or ice cells)."""
    if len(days) < 365:
        return []
    found = []
    for (source, name), index in sorted(lookup.items()):
        if name != 'snow_water_equivalent_mm':
            continue
        for p, project in enumerate(projects):
            series = values[:, p, index]
            if np.isfinite(series).sum() >= 365 and np.nanmin(series) > 50:
                found.append(dict(source=source, project_id=project,
                                  minimum_swe_mm=float(np.nanmin(series)),
                                  mean_swe_mm=float(np.nanmean(series))))
    return found


# Review flags: values inside the physical limits that are unusual for their catchment, day,
# other source or neighbours. A score of 1 is the threshold; flags are for inspection, not failures.
AMOUNTS = {'precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'snowmelt_mm'}
# States that sit at a floor for long spells and leave it in real events: snow (zero for months,
# checked against snowfall and melt instead) and the 0 degC level (zero, or the terrain height above
# sea level, until a warm air mass arrives). Climatology, neighbours and runs of equal values
# mislead for them; only the plausibility bounds apply.
FLOORED = {'snow_water_equivalent_mm', 'snow_depth_m', 'snow_cover_pct', 'snow_density_kgm3',
           'perennial_snow_area_pct', 'freezing_level_above_ground_m', 'freezing_level_above_sea_level_m'}
# One day against the next only judges air temperature and pressure: rain makes a one-day jump in
# soil moisture, and fluxes vary from day to day.
SMOOTH = {'tmean_c', 'tmin_c', 'tmax_c', 'wet_bulb_temperature_c', 'surface_pressure_pa'}
STEP_MINIMUM = {'degC': .5, 'm/s': .3, 'W/m2': 5, 'kPa': .05, 'pct': 2}
OUTLIER_FIELDS = ['rank', 'kind', 'source', 'variable', 'project_id', 'date', 'value', 'reference',
                  'score', 'detail']


def _spread(data, axis=0, floor=1e-9):
    """Robust standard deviation: 1.4826 x MAD, or the 10-90% range for series that mostly sit at
    one value (SWE at zero in summer), never below floor."""
    median = np.nanmedian(data, axis=axis, keepdims=True)
    mad = 1.4826 * np.nanmedian(np.abs(data - median), axis=axis)
    low, high = np.nanpercentile(data, [10, 90], axis=axis)
    return np.maximum(np.maximum(mad, (high - low) / 2.563), floor)


def seasonal_windows(days):
    """Half-month periods: the rows to judge, and the rows (that period and the two either side) to
    judge them by, so a fast seasonal change does not make the end of a month look unusual."""
    half = np.array([(d.month - 1) * 2 + (d.day > 15) for d in days])
    for k in range(24):
        judged = np.flatnonzero(half == k)
        if len(judged):
            yield judged, np.flatnonzero(np.isin(half, [(k - 1) % 24, k, (k + 1) % 24]))


def neighbours(manifest, projects, count=3):
    """Nearest catchments by outlet distance; empty when outlets lack coordinates."""
    points = [manifest['project_metadata'][p] for p in projects]
    if len(projects) <= count or any(not {'lat', 'lon'} <= set(m) for m in points):
        return {}
    lat, lon = (np.radians([float(m[key]) for m in points]) for key in ('lat', 'lon'))
    angle = np.arccos(np.clip(np.sin(lat)[:, None] * np.sin(lat) +
                              np.cos(lat)[:, None] * np.cos(lat) * np.cos(lon[:, None] - lon), -1, 1))
    np.fill_diagonal(angle, np.inf)
    return {p: np.argsort(angle[p])[:count] for p in range(len(projects))}


def screen_outliers(manifest, days, projects, columns, values, lookup):
    """Rank values that deserve a look; returns (rows, counts by kind)."""
    years = np.array([d.year for d in days])
    names = {index: key for key, index in lookup.items()}
    rows = []
    # Seasonal, cross-source and neighbour statistics need at least a year of days.
    long_record = len(days) >= 365

    def flag(kind, j, p, d, value, reference, score, detail):
        source, variable = names[j]
        rows.append(dict(kind=kind, source=source, variable=variable, project_id=projects[p],
                         date=str(days[d]) if isinstance(d, (int, np.integer)) else d,
                         value=round(float(value), 4), reference=round(float(reference), 4),
                         score=round(float(score), 2), detail=detail))

    # All-blank series and windows are skipped, not errors.
    with np.errstate(all='ignore'), warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        for j, (source, variable) in sorted(names.items()):
            x = values[:, :, j]
            if not np.isfinite(x).any() or variable in FLOORED:
                continue
            amount = variable in AMOUNTS
            # Unusual for the catchment and time of year.
            for judged, window in seasonal_windows(days) if long_record else []:
                block, base = x[judged], x[window]
                if amount:
                    wet = np.where(base > .1, base, np.nan)
                    if not np.isfinite(wet).any():
                        continue
                    center = np.nanpercentile(wet, 99, axis=0)
                    score = block / np.maximum(3 * center, 20)
                else:
                    center = np.nanmedian(base, axis=0)
                    score = np.abs(block - center) / np.maximum(_spread(base), .1 * _spread(x)) / 6
                for d, p in np.argwhere(score > 1):
                    flag('climatology', j, p, judged[d], block[d, p], center[p], score[d, p],
                         'far from this catchment\'s values for the time of year')
            if variable in SMOOTH and len(days) >= 3:
                # One day out of line with both neighbours in time.
                middle = x[1:-1]
                dev = middle - (x[:-2] + x[2:]) / 2
                same_side = (middle - x[:-2]) * (middle - x[2:]) > 0
                score = np.where(same_side, np.abs(dev) / np.maximum(_spread(dev), .05 * _spread(x)) / 8, 0)
                for d, p in np.argwhere(score > 1):
                    flag('spike', j, p, d + 1, middle[d, p], (x[d, p] + x[d + 2, p]) / 2, score[d, p],
                         'single day out of line with the days before and after')
            if not amount and not variable.startswith(('soil_', 'root_zone_')):
                # Values are written at full precision, so five equal days of a weather variable are a
                # stuck series; zero is exempt (no PET in deep cold). Deep soil changes by less than
                # the stored precision for days.
                for p in range(len(projects)):
                    same = np.r_[False, np.abs(np.diff(x[:, p])) < 1e-9, False]
                    edges = np.flatnonzero(np.diff(same.astype(int)))
                    for begin, end in zip(edges[::2], edges[1::2]):
                        length = end - begin + 1
                        if length >= 5 and abs(x[begin, p]) > 1e-3:
                            flag('constant', j, p, int(begin), x[begin, p], length, length / 5,
                                 f'same value for {length} days')

        land = SOURCES[1]
        if all((land, v) in lookup for v in ('snow_water_equivalent_mm', 'precipitation_mm', 'snowmelt_mm')):
            # Daily-mean SWE can rise by at most two days of precipitation and fall by two days of melt.
            j = lookup[land, 'snow_water_equivalent_mm']
            swe, rain, melt = (values[:, :, lookup[land, v]]
                               for v in ('snow_water_equivalent_mm', 'precipitation_mm', 'snowmelt_mm'))
            change = swe[1:] - swe[:-1]
            gain = (change - rain[1:] - rain[:-1] - 5) / 10
            loss = (-change - melt[1:] - melt[:-1] - 10) / 10
            for d, p in np.argwhere(gain > 1):
                flag('snow', j, p, d + 1, change[d, p], rain[d + 1, p] + rain[d, p], gain[d, p],
                     'SWE rose more than two days of precipitation')
            for d, p in np.argwhere(loss > 1):
                flag('snow', j, p, d + 1, change[d, p], -(melt[d + 1, p] + melt[d, p]), loss[d, p],
                     'SWE fell more than two days of snowmelt')

        for variable in shared_variables(lookup) if long_record else []:
            j = lookup[SOURCES[0], variable]
            a, e = values[:, :, j], values[:, :, lookup[SOURCES[1], variable]]
            units = manifest['columns'][columns[j]]['units']
            if variable in AMOUNTS:
                high, low = np.fmax(a, e), np.fmin(a, e)
                score = np.where(high >= 25, high / (5 * (low + 1)), 0)
                for d, p in np.argwhere(score > 1):
                    flag('sources', j, p, d, a[d, p], e[d, p], score[d, p],
                         'heavy in one source, light in the other (reference: ERA5-Land)')
            else:
                diff = a - e
                floor = np.maximum(.1 * _spread(diff), 1e-6)
                for judged, window in seasonal_windows(days):
                    block, base = diff[judged], diff[window]
                    score = np.abs(block - np.nanmedian(base, axis=0)) / np.maximum(_spread(base), floor) / 8
                    for d, p in np.argwhere(score > 1):
                        flag('sources', j, p, judged[d], a[judged[d], p], e[judged[d], p], score[d, p],
                             'AORC and ERA5-Land differ far more than usual (reference: ERA5-Land)')
            # A lasting shift in the annual mean difference, e.g. from a change of input data.
            annual = np.array([[np.nanmean(diff_y) if np.isfinite(diff_y).sum() >= 300 else np.nan
                                for diff_y in (a - e)[years == y].T] for y in np.unique(years)])
            year_list = np.unique(years)
            for p in range(len(projects)):
                series = annual[:, p]
                good = np.flatnonzero(np.isfinite(series))
                if len(good) < 8:
                    continue
                best = (0, None, 0)
                for k in range(3, len(good) - 2):
                    before, after = series[good[:k]], series[good[k:]]
                    shift = after.mean() - before.mean()
                    noise = np.sqrt(before.var(ddof=1) / len(before) + after.var(ddof=1) / len(after))
                    t = abs(shift) / max(noise, 1e-9)
                    if t > best[0]:
                        best = (t, good[k], shift)
                t, k, shift = best
                minimum = STEP_MINIMUM.get(units, .1 * abs(np.nanmean(a[:, p])))
                if k is not None and t > 6 and abs(shift) > minimum:
                    flag('step', j, p, f'{year_list[k]}', shift, minimum, t / 6,
                         f'mean AORC minus ERA5-Land shifts by {shift:+.3g} {units} from {year_list[k]}')

        near = neighbours(manifest, projects)
        if near and long_record:
            for j, (source, variable) in sorted(names.items()):
                if variable in FLOORED:
                    continue
                x = values[:, :, j]
                for p, others in near.items():
                    # Compare only on days all neighbours have a value: catchments differ in elevation,
                    # so a median over two of them instead of three jumps (by kPa for pressure).
                    block = x[:, others]
                    complete = np.isfinite(block).all(axis=1)
                    reference = np.where(complete, np.median(block, axis=1), np.nan)
                    if variable in AMOUNTS:
                        score = np.where(complete & (x[:, p] >= 30),
                                         x[:, p] / (5 * (np.where(complete, block.max(axis=1), 0) + 1)), 0)
                    else:
                        score = np.zeros(len(days))
                        gaps = x[:, p] - reference
                        floor = max(.1 * float(_spread(gaps)), 1e-6)
                        for judged, window in seasonal_windows(days):
                            score[judged] = (np.abs(gaps[judged] - np.nanmedian(gaps[window]))
                                             / max(float(_spread(gaps[window])), floor) / 8)
                    for d in np.flatnonzero(np.nan_to_num(score) > 1):
                        flag('neighbours', j, p, d, x[d, p], reference[d], score[d],
                             'unlike the nearest catchments (reference: their median)')
    rows.sort(key=lambda row: -row['score'])
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return rows, dict(Counter(row['kind'] for row in rows))


def plot_outliers(folder, manifest, days, projects, columns, values, lookup, rows, count=24):
    """One panel per flagged event: 30 days either side, the other source and the neighbours."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from plot_daily import label

    blue, orange, grey, ink = '#2a78d6', '#eb6834', '#898781', '#0b0b0b'
    events, seen = [], set()
    for row in rows:
        key = (row['project_id'], row['variable'], row['date'][:7] if row['kind'] != 'step' else row['date'])
        if key not in seen:
            seen.add(key)
            events.append(row)
        if len(events) == count:
            break
    near = neighbours(manifest, projects)
    index = {str(d): i for i, d in enumerate(days)}
    with PdfPages(folder / 'outliers.pdf') as pdf:
        for page in range(0, len(events), 6):
            fig, axes = plt.subplots(3, 2, figsize=(12, 10), layout='constrained', squeeze=False)
            for ax in axes.flat[len(events[page:page + 6]):]:
                ax.axis('off')
            for ax, row in zip(axes.flat, events[page:page + 6]):
                p, variable = projects.index(row['project_id']), row['variable']
                units = manifest['columns'][columns[lookup[row['source'], variable]]]['units']
                name = manifest['project_metadata'][row['project_id']].get('name', row['project_id'])
                ax.set_title(f"{name} · {label(variable)[0]} ({units}) · {row['date']} · {row['kind']}",
                             fontsize=10, color=ink, loc='left')
                ax.spines[['top', 'right']].set_visible(False)
                ax.grid(alpha=.2)
                if row['kind'] == 'step':
                    a, e = (values[:, p, lookup[s, variable]] for s in SOURCES)
                    year_list = sorted({d.year for d in days})
                    yearly = [np.nanmean((a - e)[[d.year == y for d in days]]) for y in year_list]
                    ax.bar(year_list, yearly, color=blue, width=.7)
                    ax.axvline(int(row['date']) - .5, color=grey, lw=1)
                    continue
                center = index[row['date']]
                window = slice(max(center - 30, 0), min(center + 31, len(days)))
                span = days[window]
                for source, color in zip(SOURCES, (blue, orange)):
                    if (source, variable) in lookup:
                        ax.plot(span, values[window, p, lookup[source, variable]], color=color, lw=1.6,
                                label={'aorc_v1.1': 'AORC', 'era5_land_cds': 'ERA5-Land'}[source])
                if p in near:
                    ax.plot(span, np.nanmedian(values[window][:, near[p], lookup[row['source'], variable]], axis=1),
                            color=grey, lw=1.2, label='Nearest catchments')
                ax.plot(days[center], values[center, p, lookup[row['source'], variable]], 'o', ms=7,
                        color=ink, mfc='none', mew=1.5)
                ax.legend(frameon=False, fontsize=8)
                ax.tick_params(axis='x', labelsize=8, rotation=30)
            pdf.savefig(fig)
            plt.close(fig)


def write_csv(path, rows, columns):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def plot_delivery(folder, manifest, days, projects, columns, values, lookup):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    colors = {'aorc_v1.1': '#176b91', 'era5_land_cds': '#ce6c32', 'era5_cds': '#6c52a3'}
    labels = {'aorc_v1.1': 'AORC', 'era5_land_cds': 'ERA5-Land', 'era5_cds': 'ERA5'}
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.titleweight': 'bold', 'savefig.facecolor': 'white'})
    span = f'{days[0]} to {days[-1]} UTC'
    fig, ax = plt.subplots(figsize=(max(10, len(projects)*.25+6), max(5, len(columns)*.26+2.8)),
                           layout='constrained')
    coverage = np.isfinite(values).mean(axis=0).T*100
    im = ax.imshow(coverage, aspect='auto', vmin=0, vmax=100, cmap='cividis')
    ax.set_xticks(range(len(projects)), projects, rotation=90, fontsize=8)
    names = [f"{labels.get(manifest['columns'][c]['source'], manifest['columns'][c]['source'])} | "
             + c.split('__', 1)[-1] for c in columns]
    ax.set_yticks(range(len(columns)), names, fontsize=8)
    ax.set_title('Valid daily coverage by project and variable\n'+span, pad=14)
    fig.colorbar(im, ax=ax, label='Valid days (%)', shrink=.6)
    fig.savefig(folder/'coverage.png', dpi=160)
    plt.close(fig)

    shared = shared_variables(lookup)
    if shared:
        ncols = 4
        nrows = int(np.ceil(len(shared)/ncols))
        fig, axes = plt.subplots(nrows, ncols, figsize=(4.2*ncols, 4*nrows), layout='constrained',
                                 squeeze=False)
        for ax in axes.flat[len(shared):]:
            ax.axis('off')
        for ax, variable in zip(axes.flat, shared):
            x, y = [values[:, :, lookup[s, variable]].ravel() for s in SOURCES]
            valid = np.isfinite(x) & np.isfinite(y)
            x, y = x[valid], y[valid]
            title = f"{variable} ({manifest['columns'][columns[lookup[SOURCES[0], variable]]]['units']})"
            if len(x):
                stats = comparison(x, y, 'ALL', variable)
                # Metrics use all pairs; only scatter markers are thinned for long records.
                take = np.linspace(0, len(x)-1, min(10000, len(x)), dtype=int)
                ax.scatter(x[take], y[take], s=6, alpha=.25, color='#176b91', edgecolors='none')
                lo, hi = min(x.min(), y.min()), max(x.max(), y.max())
                pad = max((hi-lo)*.05, 1e-6)
                ax.plot([lo-pad, hi+pad], [lo-pad, hi+pad], '--', color='#6b7280', lw=1)
                ax.set_xlim(lo-pad, hi+pad)
                ax.set_ylim(lo-pad, hi+pad)
                r = stats['correlation']
                title += f"\nbias={stats['bias_land_minus_aorc']:.3g}  r={r:.2f}" if r is not None else ''
            else:
                ax.text(.5, .5, 'No paired valid days', ha='center', transform=ax.transAxes)
            ax.set_title(title, fontsize=9)
            ax.set_xlabel('AORC', fontsize=8)
            ax.set_ylabel('ERA5-Land', fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(alpha=.15)
        fig.suptitle('AORC versus ERA5-Land daily catchment means · '+span)
        fig.savefig(folder/'source_comparison.png', dpi=130)
        plt.close(fig)

    plot_days, plot_values = days, values
    scale = 'Daily values'
    if len(days) > 366:
        month_keys = np.array([d.strftime('%Y-%m') for d in days])
        months = np.unique(month_keys)
        plot_days = [date.fromisoformat(m+'-01') for m in months]
        monthly = []
        for month in months:
            block = values[month_keys == month]
            count = np.isfinite(block).sum(axis=0)
            monthly.append(np.divide(np.nansum(block, axis=0), count,
                           out=np.full(count.shape, np.nan), where=count > 0))
        plot_values = np.array(monthly)
        scale = 'Monthly means of valid daily values'
    panels = [(['tmean_c'], '2 m air temperature', '°C'),
              (['precipitation_mm'], 'Precipitation', 'mm/day'),
              (['shortwave_down_mean_wm2', 'longwave_down_mean_wm2', 'net_radiation_mean_wm2'],
               'Surface radiation', 'W/m²'),
              (['snow_water_equivalent_mm'], 'Snow water equivalent', 'mm'),
              (['wind_speed_ms'], '10 m wind speed', 'm/s'),
              (['freezing_level_above_sea_level_m', 'freezing_level_above_ground_m'],
               'ERA5 0 °C level', 'm')]
    with PdfPages(folder/'timeseries.pdf') as pdf:
        for p, project in enumerate(projects):
            fig, axes = plt.subplots(3, 2, figsize=(12, 10), layout='constrained')
            for ax, (variables, title, units) in zip(axes.flat, panels):
                plotted = False
                for variable in variables:
                    for source in colors:
                        if (source, variable) not in lookup:
                            continue
                        y = plot_values[:, p, lookup[source, variable]]
                        if not np.isfinite(y).any():
                            continue
                        label = labels[source]
                        if len(variables) > 1:
                            label += ' '+{'shortwave_down_mean_wm2': 'SW down',
                                          'longwave_down_mean_wm2': 'LW down',
                                          'net_radiation_mean_wm2': 'net total',
                                          'freezing_level_above_sea_level_m': 'above sea level',
                                          'freezing_level_above_ground_m': 'above ground'}[variable]
                        ax.plot(plot_days, y, label=label, color=colors[source], lw=1.5,
                                linestyle='--' if variable in ('longwave_down_mean_wm2',
                                                               'freezing_level_above_ground_m') else '-',
                                marker='o' if len(plot_days) <= 14 else None, markersize=3)
                        plotted = True
                if not plotted:
                    present = any((s, v) in lookup for s in colors for v in variables)
                    ax.text(.5, .5, 'No valid values' if present else 'Variable not in this delivery',
                            ha='center', va='center', transform=ax.transAxes)
                    ax.set_yticks([])
                else:
                    ax.legend(frameon=False, fontsize=8)
                    if variables[0] in ('precipitation_mm', 'snow_water_equivalent_mm',
                                        'wind_speed_ms', 'freezing_level_above_sea_level_m'):
                        ax.set_ylim(bottom=0)
                ax.set_title(title)
                ax.set_ylabel(units)
                ax.grid(alpha=.18)
                if len(days) <= 14:
                    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(days)//5)))
                    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
                else:
                    locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
                    ax.xaxis.set_major_locator(locator)
                    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
                if len(plot_days) > 1:
                    ax.set_xlim(plot_days[0], plot_days[-1])
                else:
                    ax.set_xlim(plot_days[0]-timedelta(days=1), plot_days[0]+timedelta(days=1))
            name = manifest['project_metadata'][project].get('name', project)
            fig.suptitle(f'{name} · {project}\n{scale} · {span}', fontsize=15)
            pdf.savefig(fig)
            plt.close(fig)


def check_file(path, *, plots=True):
    path = Path(path).resolve()
    manifest, days, projects, columns, values, counts, qc_check = load_delivery(path)
    checks, lookup = consistency_checks(manifest, days, projects, columns, values)
    checks.insert(0, qc_check)
    comparisons = []
    months = np.array([day.month for day in days])
    for variable in shared_variables(lookup):
        x, y = [values[:, :, lookup[s, variable]] for s in SOURCES]
        comparisons += [comparison(x[:, p], y[:, p], project, variable)
                        for p, project in enumerate(projects)]
        comparisons.append(comparison(x.ravel(), y.ravel(), 'ALL', variable))
        for season, selected in SEASONS.items():
            mask = np.isin(months, selected)
            if mask.any():
                comparisons.append(comparison(x[mask].ravel(), y[mask].ravel(), 'ALL', variable, season))
    summaries = []
    for p, project in enumerate(projects):
        for j, column in enumerate(columns):
            y = values[:, p, j]
            y = y[np.isfinite(y)]
            summaries.append(dict(project_id=project, column=column,
                                  units=manifest['columns'][column]['units'], days=len(days),
                                  valid_days=len(y), missing_days=len(days)-len(y),
                                  valid_pct=100*len(y)/len(days),
                                  minimum=float(y.min()) if len(y) else None,
                                  mean=float(y.mean()) if len(y) else None,
                                  maximum=float(y.max()) if len(y) else None))
    missing = {column: int(np.isnan(values[:, :, j]).sum()) for j, column in enumerate(columns)
               if np.isnan(values[:, :, j]).any()}
    missing_qc = {}
    for (project, column, qc), number in counts.items():
        if qc != 'valid':
            missing_qc.setdefault(column, Counter())[qc] += number
    snow = persistent_snow(manifest, days, projects, columns, values, lookup)
    folder = Path(str(path)+'.checks')
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'summary.json').unlink(missing_ok=True)
    write_csv(folder/'variables.csv', summaries, list(summaries[0]))
    write_csv(folder/'qc_summary.csv', [dict(project_id=p, column=c, qc=q, days=n)
              for (p, c, q), n in sorted(counts.items())], ['project_id', 'column', 'qc', 'days'])
    write_csv(folder/'checks.csv', [{**r, 'examples': json.dumps(r['examples'])} for r in checks],
              ['check', 'evaluated', 'failed', 'examples'])
    write_csv(folder/'source_comparison.csv', comparisons, list(comparison(np.array([]), np.array([]), '', '')))
    flags, flagged = screen_outliers(manifest, days, projects, columns, values, lookup)
    write_csv(folder/'outliers.csv', flags, OUTLIER_FIELDS)
    (folder/'outliers.pdf').unlink(missing_ok=True)
    if plots:
        plot_delivery(folder, manifest, days, projects, columns, values, lookup)
        if flags:
            plot_outliers(folder, manifest, days, projects, columns, values, lookup, flags)
    report = dict(input_file=path.name, output_sha256=manifest['output_sha256'],
                  qc_output_sha256=manifest['qc_output_sha256'], checker_sha256=digest(Path(__file__)),
                  row_count=manifest['row_count'], project_count=len(projects), start=str(days[0]),
                  end=str(days[-1]), failed_checks=sum(r['failed'] > 0 for r in checks),
                  missing_values=int(np.isnan(values).sum()), total_values=values.size,
                  missing_by_column={column: dict(missing=number, qc=dict(missing_qc.get(column, {})))
                                     for column, number in missing.items()},
                  persistent_snow=snow, review_flags=flagged, checks=checks, comparisons=comparisons, plots=plots,
                  scope='Delivery integrity, physical plausibility and AORC/ERA5-Land agreement; '
                        'no independent observation validation.')
    (folder/'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(f"Checks: {report['failed_checks']} failed; {report['missing_values']:,} missing values; {folder}",
          flush=True)
    for column, detail in report['missing_by_column'].items():
        reasons = ', '.join(f'{qc}={n}' for qc, n in sorted(detail['qc'].items()))
        print(f"  {column}: {detail['missing']} missing project-days ({reasons})", flush=True)
    for item in snow:
        print(f"  Persistent snow: {item['source']} {item['project_id']} SWE never below "
              f"{item['minimum_swe_mm']:.0f} mm", flush=True)
    kinds = ', '.join(f'{kind} {number:,}' for kind, number in sorted(flagged.items()))
    print(f"Review flags: {len(flags):,}" + (f" ({kinds}); ranked in {folder/'outliers.csv'}" if flags else ''),
          flush=True)
    groups = Counter((row['kind'], row['source'], row['variable']) for row in flags)
    for kind in sorted(flagged):
        largest = sorted(((n, s, v) for (k, s, v), n in groups.items() if k == kind), reverse=True)[:3]
        print(f'  {kind}: ' + '; '.join(f'{s} {v} {n:,}' for n, s, v in largest), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='CSV, CSV.gz or Parquet; finds sibling QC and manifest')
    args = parser.parse_args()
    try:
        return 2 if check_file(args.input)['failed_checks'] else 0
    except (OSError, ValueError, KeyError) as error:
        print(f'Check failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
