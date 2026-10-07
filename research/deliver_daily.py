"""Write the requested-variable table and its README from a verified full daily export.

The requested table copies selected columns of the full values table. Where an
AORC value is blank (a source gap), it is filled from the same ERA5-Land
quantity adjusted to AORC's mean for that catchment and calendar month, and the
row's `aorc_gap_filled` column names every filled column. The full table is
never filled. The README documents every column's source, whether it is a
native source field or calculated here, its units and daily definition, the
corresponding operational forecast fields, gap filling and the QC results.
"""

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np

from check_daily import table_rows
from delivery_variables import AORC, LAND, REQUESTED, SOURCE_LABELS
from export_daily import _write, digest
from meteorology import hargreaves_pet


FILL_COLUMN = 'aorc_gap_filled'
# Signed quantities are shifted by the mean difference; nonnegative ones scaled by the mean ratio.
ADDITIVE = {'tmean_c', 'tmin_c', 'tmax_c', 'u_wind_ms', 'v_wind_ms', 'relative_humidity_pct'}
RATIO = {'precipitation_mm', 'wind_speed_ms', 'shortwave_down_mean_wm2', 'vapor_pressure_deficit_kpa'}
RATIO_LIMITS = (.25, 4.)


def companion(path, tag, suffix=None):
    """Sibling path with a tag before the table suffix, e.g. x.csv -> x_full.csv."""
    path = Path(path)
    table_suffix = '.csv.gz' if path.name.endswith('.csv.gz') else path.suffix
    stem = path.name[:-len(table_suffix)] if table_suffix else path.name
    return path.with_name(stem + tag + (table_suffix if suffix is None else suffix))


def requested_columns(manifest):
    """Map requested column names to full-table columns; every requested field must exist."""
    by_variable = {(info.get('source'), info.get('variable')): column
                   for column, info in manifest['columns'].items()}
    selected = []
    for item in REQUESTED:
        column = by_variable.get((item['source'], item['variable']))
        if column is None:
            raise ValueError(f"Full delivery lacks {item['source']}/{item['variable']}")
        selected.append((column.split('__', 1)[1], column, item))
    if len({name for name, _, _ in selected}) != len(selected):
        raise ValueError('Requested column names are not unique')
    return selected


def _table(rows, headers, aligns=None):
    aligns = aligns or ['---']*len(headers)
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(aligns) + ' |']
    lines += ['| ' + ' | '.join(str(value) for value in row) + ' |' for row in rows]
    return '\n'.join(lines)


def _number(value, digits=3):
    if value is None:
        return '–'
    return f'{value:.{digits}g}'


def _units(info):
    """Daily amounts are stored in mm per UTC day; say so in the table."""
    amount = info['units'] == 'mm' and info.get('statistic') in ('sum', '24h_endpoint')
    return 'mm/day' if amount else info['units']


def _gap_lines(gaps):
    if not gaps:
        return []
    lines = ['## Gap filling', '',
             f'AORC has no data for some hours (for example 2024-06-18 in the whole archive, and the '
             f'2026-01-01 00 UTC hour needed for 2025-12-31 precipitation). In the requested table, such a '
             f'blank AORC value is replaced by the same ERA5-Land quantity, adjusted to AORC for that '
             f'catchment and calendar month over this delivery: temperatures, wind components and relative '
             f'humidity are shifted by the mean difference; precipitation, wind speed, shortwave radiation and '
             f'vapor pressure deficit are scaled by the mean ratio (limited to 0.25-4). Filled precipitation '
             f'is split into rain and snow with ERA5-Land\'s snow fraction, and PET is recalculated from the '
             f'filled temperatures. `{FILL_COLUMN}` lists the filled columns of each row; it is empty when '
             f'nothing was filled. The full table keeps these values blank.', '']
    if gaps['counts']:
        lines += [_table([[f'`{name}`', count] for name, count in gaps['counts'].items()],
                         ['Column', 'Filled project-days']), '',
                  'Dates with filled values: ' + ', '.join(gaps['dates']) + '.', '']
    else:
        lines += ['No values needed filling in this delivery.', '']
    return lines


def readme_text(manifest, selected, files, missing, checks, gaps=None):
    start, end = manifest['start'], manifest['end']
    projects = manifest['project_metadata']
    by_column = {column: info for column, info in manifest['columns'].items()}
    lines = [
        '# Daily catchment weather and land-surface data', '',
        f"{len(projects)} project catchments, {start} to {end} (UTC days), "
        f"{manifest['row_count']:,} rows: one per project and day.", '',
        '## Files', '',
        _table([[f"`{files['requested']}`", f'Requested variables ({len(selected)} columns plus `date`, `project_id`)'],
                [f"`{files['full']}`", f"All {len(by_column)-2} variables from all sources, including cross-check duplicates"],
                [f"`{files['qc']}`", 'Per value: QC flag, valid hours, expected hours, minimum valid area fraction'],
                [f"`{files['manifest']}`", 'Machine-readable units, methods, provenance and file hashes'],
                [f"`{files['checks']}/`", 'QC reports, AORC/ERA5-Land comparisons, coverage and time-series plots']],
               ['File', 'Contents']), '',
        '## Conventions', '',
        '- `date` is a UTC calendar day; `project_id` is the catchment identifier in the geometry file.',
        '- Every value is an area-weighted mean over the local catchment polygon, using fractional '
        'native grid cells. Instantaneous fields use hours 00-23 UTC; AORC precipitation sums '
        'hour-ending amounts 01-24 UTC; ERA5-Land fluxes use the 24-hour accumulation ending at 24 UTC.',
        '- In the full table a value is blank only when part of the catchment or day is missing in the '
        'source; blanks are never filled there, and the QC file gives the reason. In the requested table, '
        f'blank AORC values are filled from ERA5-Land and flagged in `{FILL_COLUMN}` (see Gap filling).',
        '- "Native" means the source field itself (units converted only); "computed" means calculated '
        'here from native fields as described.',
        '- Column names end with their units: `mm` is a daily amount (mm/day), `W_m2` a daily mean flux, '
        '`degC`, `Pa`, `kPa`, `kg_kg`, `m3_m3`, `kg_m3`, `m_s`, `m`, `pct` (%) and `fraction` (0-1).', '',
        '## Sources', '',
        *[f'- `{source}`: {label}' for source, label in SOURCE_LABELS.items()], '',
        '## Requested variables', '',
        f"{len({item['quantity'].split(' (')[0] for _, _, item in selected})} requested variables in "
        f"{len(selected)} columns, in the order of the request; soil temperature is given for the four "
        'ERA5-Land layers.', '',
        _table([[item['category'], item['quantity'], f'`{name}`', _units(by_column[column]), item['source'],
                 item['origin'], item['definition'], f"{missing.get(name, 0):,}"]
                for name, column, item in selected],
               ['Category', 'Variable', 'Column', 'Units', 'Source', 'Native or computed', 'Daily definition',
                'Blank values']), '',
        *_gap_lines(gaps),
        '## Matching operational weather forecasts', '',
        'Forecast fields that correspond to each column. ECMWF names are IFS open-data (0.25 deg) '
        'parameters unless marked as full archive; GFS names are 0.25 deg GRIB2 `VARIABLE:level`; '
        'WeatherNext 2 lists its output fields (6-hourly; 2 m temperature, 10/100 m wind, mean '
        'sea-level pressure, 6-hour precipitation and pressure-level fields). To use a forecast as '
        'model input, aggregate it exactly as described above: same catchment polygons, UTC day, '
        'hourly values before daily statistics, and the same formulas for computed variables. ERA5 and '
        'ERA5-Land come from the ECMWF model family, so IFS fields are the closest match for those columns.', '',
        'The requested forcing comes from AORC, an observation-based analysis. A forecast model has its own '
        'climatology, so a model trained on AORC should receive forecasts bias-corrected toward AORC over a '
        'common historical period. Alternatively, the full table repeats every AORC forcing variable from '
        'ERA5-Land (same names with the `era5_land_cds__` prefix), which is closer to ECMWF IFS forecasts. '
        'The quality-control section below quantifies how far the two sources differ.', '',
        _table([[item['quantity'], f'`{name}`', item['ifs'], item['gfs'], item['wn2'], item['note']]
                for name, _, item in selected],
               ['Variable', 'Column', 'ECMWF IFS', 'NOAA GFS', 'WeatherNext 2', 'Note']), '',
    ]
    if checks:
        failed = [row for row in checks['checks'] if row['failed']]
        lines += ['## Quality control', '',
                  f"{len(checks['checks'])} automated checks: value/QC agreement, physical identities "
                  f"(for example rain + snow = precipitation, net = shortwave + longwave), and plausible "
                  f"ranges for every variable. Failed checks: {len(failed)}.", '']
        if failed:
            lines += [_table([[row['check'], row['evaluated'], row['failed']] for row in failed],
                             ['Failed check', 'Evaluated', 'Failed']), '']
        blank = checks.get('missing_by_column', {})
        if blank:
            lines += ['Blank values in the full table:', '',
                      _table([[f'`{column}`', detail['missing'],
                               ', '.join(f'{qc} ({n})' for qc, n in sorted(detail['qc'].items())) or '–']
                              for column, detail in sorted(blank.items())],
                             ['Column', 'Blank project-days', 'QC reasons']), '']
        comparisons = checks.get('comparisons', [])
        rows = [row for row in comparisons if row['project_id'] == 'ALL' and row.get('season', 'all') == 'all']
        seasonal = {}
        for row in comparisons:
            if row['project_id'] == 'ALL' and row.get('season', 'all') != 'all':
                seasonal.setdefault(row['variable'], {})[row['season']] = row
        if rows:
            lines += ['AORC and ERA5-Land are independent estimates of the shared quantities. Their '
                      'agreement over all catchments and days (ERA5-Land minus AORC):', '',
                      _table([[f"`{row['variable']}`", f"{row['paired_days']:,}", _number(row['mean_aorc']),
                               _number(row['mean_land']), _number(row['bias_land_minus_aorc']),
                               _number(row['ratio_land_to_aorc']), _number(row['correlation'], 2)]
                              for row in rows],
                             ['Variable', 'Pairs', 'AORC mean', 'ERA5-Land mean', 'Bias', 'Ratio', 'Correlation']),
                      '']
        if seasonal:
            seasons = ['DJF', 'MAM', 'JJA', 'SON']
            describe = lambda row: ('–' if row is None or row['bias_land_minus_aorc'] is None else
                                    _number(row['bias_land_minus_aorc']) +
                                    (f" (x{row['ratio_land_to_aorc']:.2f})" if row['ratio_land_to_aorc'] else ''))
            lines += ['Seasonal bias, ERA5-Land minus AORC (mean ratio in brackets):', '',
                      _table([[f'`{variable}`', *[describe(by_season.get(season)) for season in seasons]]
                              for variable, by_season in sorted(seasonal.items())],
                             ['Variable', *seasons]), '']
        if rows:
            lines += [f"Per-catchment statistics: `{files['checks']}/source_comparison.csv`.", '']
        if checks.get('persistent_snow'):
            lines += ['Catchments where daily SWE never fell below 50 mm during a year (permanent snow '
                      'or glacier cells in the source grid; treat SWE, snow depth and snowmelt there with care):', '',
                      _table([[item['project_id'], item['source'], f"{item['minimum_swe_mm']:.0f}",
                               f"{item['mean_swe_mm']:.0f}"] for item in checks['persistent_snow']],
                             ['Project', 'Source', 'Minimum SWE (mm)', 'Mean SWE (mm)']), '']
    else:
        lines += ['## Quality control', '', 'Automated checks did not complete; see the run log.', '']
    lines += ['## Catchments', '',
              _table([[identifier, props.get('name', ''), props.get('river', ''), props.get('country', ''),
                       _number(props.get('area_local', props.get('area')), 6)]
                      for identifier, props in sorted(projects.items())],
                     ['project_id', 'Name', 'River', 'Country', 'Local area (km2)']), '']
    return '\n'.join(lines)


def _float(value):
    return math.nan if value is None or value == '' else float(value)


def fill_aorc_gaps(values, land, projects, dates, latitudes):
    """Fill blank AORC values from ERA5-Land adjusted to AORC per catchment and calendar month.

    values/land map AORC variable names to arrays over rows (land may lack a variable).
    Returns {variable: boolean array of filled rows}; values are updated in place.
    Precipitation is split by ERA5-Land's snow fraction so rain + snow = total, and
    temperature order, wind-speed magnitude and the RH range stay consistent.
    """
    months = np.array([day.month for day in dates])
    gaps = {variable: np.isnan(array) for variable, array in values.items()}
    filled = {variable: np.zeros(len(dates), bool) for variable in values}
    groups = defaultdict(list)
    for index, key in enumerate(zip(projects, months)):
        groups[key].append(index)
    groups = {key: np.array(rows) for key, rows in groups.items()}
    for variable in ADDITIVE | RATIO:
        if variable not in values or variable not in land:
            continue
        x, y = values[variable], land[variable]
        need = gaps[variable] & np.isfinite(y)
        for key in {key for key in zip(projects[need], months[need])}:
            rows = groups[key]
            pair = rows[np.isfinite(x[rows]) & np.isfinite(y[rows])]
            target = rows[need[rows]]
            if variable in ADDITIVE:
                x[target] = y[target] + (x[pair].mean() - y[pair].mean() if len(pair) else 0.)
            else:
                total = y[pair].sum()
                ratio = x[pair].sum()/total if len(pair) and total > 0 else 1.
                x[target] = y[target]*min(max(ratio, RATIO_LIMITS[0]), RATIO_LIMITS[1])
            filled[variable][target] = True
    if {'rainfall_mm', 'snowfall_mm', 'precipitation_mm'} <= set(values) and {'precipitation_mm', 'snowfall_mm'} <= set(land):
        total = values['precipitation_mm']
        phase = (gaps['rainfall_mm'] | gaps['snowfall_mm']) & np.isfinite(total)
        with np.errstate(divide='ignore', invalid='ignore'):
            fraction = np.where(land['precipitation_mm'] > 0,
                                np.clip(land['snowfall_mm']/land['precipitation_mm'], 0, 1), 0.)
        phase &= np.isfinite(fraction)
        values['snowfall_mm'][phase] = total[phase]*fraction[phase]
        values['rainfall_mm'][phase] = total[phase] - values['snowfall_mm'][phase]
        filled['snowfall_mm'] |= phase
        filled['rainfall_mm'] |= phase
    temperature = filled.get('tmean_c', 0) | filled.get('tmin_c', 0) | filled.get('tmax_c', 0)
    if {'tmean_c', 'tmin_c', 'tmax_c'} <= set(values) and np.any(temperature):
        values['tmin_c'][temperature] = np.fmin(values['tmin_c'], values['tmean_c'])[temperature]
        values['tmax_c'][temperature] = np.fmax(values['tmax_c'], values['tmean_c'])[temperature]
    if 'relative_humidity_pct' in values:
        rows = filled['relative_humidity_pct']
        values['relative_humidity_pct'][rows] = np.clip(values['relative_humidity_pct'][rows], 0, 100)
    if {'wind_speed_ms', 'u_wind_ms', 'v_wind_ms'} <= set(values):
        rows = filled['wind_speed_ms'] | filled['u_wind_ms'] | filled['v_wind_ms']
        values['wind_speed_ms'][rows] = np.fmax(values['wind_speed_ms'],
                                                np.hypot(values['u_wind_ms'], values['v_wind_ms']))[rows]
    if {'pet_hargreaves_mm', 'tmean_c', 'tmin_c', 'tmax_c'} <= set(values):
        for row in np.flatnonzero(gaps['pet_hargreaves_mm']):
            stats = [values[name][row] for name in ('tmean_c', 'tmin_c', 'tmax_c')]
            latitude = latitudes.get(projects[row])
            if latitude is not None and all(math.isfinite(value) for value in stats):
                values['pet_hargreaves_mm'][row] = hargreaves_pet(*stats, latitude, dates[row])
                filled['pet_hargreaves_mm'][row] = True
    return filled


def write_delivery(full_output, requested_output, readme, checks=None):
    """Write the requested columns (AORC gaps filled and flagged) and the README; returns a summary."""
    full_output, requested_output, readme = map(Path, (full_output, requested_output, readme))
    manifest = json.loads(Path(str(full_output) + '.manifest.json').read_text())
    if digest(full_output) != manifest['output_sha256']:
        raise ValueError(f'{full_output.name}: hash differs from its manifest')
    if requested_output.resolve() in {full_output.resolve(), full_output.with_name(manifest['qc_file']).resolve()}:
        raise ValueError('Requested table must not overwrite the full delivery')
    selected = requested_columns(manifest)
    by_variable = {(info.get('source'), info.get('variable')): column
                   for column, info in manifest['columns'].items()}
    aorc = {item['variable']: column for _, column, item in selected if item['source'] == AORC}
    counterparts = {variable: by_variable[LAND, variable] for variable in [*aorc, 'snowfall_mm']
                    if (LAND, variable) in by_variable}
    columns = {column for _, column, _ in selected} | set(counterparts.values())
    keys, data = [], {column: [] for column in columns}
    for record in table_rows(full_output, manifest['columns']):
        keys.append((str(record['date']), record['project_id']))
        for column in columns:
            data[column].append(_float(record[column]))
    data = {column: np.array(series, dtype=float) for column, series in data.items()}
    dates = [date.fromisoformat(day) for day, _ in keys]
    projects = np.array([project for _, project in keys])
    latitudes = next((entry['metadata'].get('pet_centroid_latitudes') or {} for entry in manifest.get('inputs', [])
                      if entry['metadata'].get('source_id') == AORC), {})
    values = {variable: data[column] for variable, column in aorc.items()}
    filled = fill_aorc_gaps(values, {variable: data[column] for variable, column in counterparts.items()},
                            projects, dates, latitudes)
    names = {item['variable']: name for name, _, item in selected if item['source'] == AORC}
    flags = [';'.join(names[variable] for variable in aorc if filled[variable][row]) for row in range(len(keys))]
    filled_counts = {names[variable]: int(rows.sum()) for variable, rows in filled.items() if rows.any()}
    filled_dates = sorted({keys[row][0] for row, flag in enumerate(flags) if flag})
    parquet = requested_output.suffix == '.parquet'
    missing = Counter()

    def rows():
        for row, (day, project) in enumerate(keys):
            output = []
            for name, column, _ in selected:
                value = data[column][row]
                if math.isnan(value):
                    missing[name] += 1
                    value = None if parquet else ''
                output.append(value)
            yield [day, project, *output, flags[row] if flags[row] or not parquet else None]

    files = dict(requested=requested_output.name, full=full_output.name, qc=manifest['qc_file'],
                 manifest=full_output.name + '.manifest.json', checks=full_output.name + '.checks')
    header = ['date', 'project_id', *[name for name, _, _ in selected], FILL_COLUMN]
    with tempfile.TemporaryDirectory(prefix='.delivery-', dir=requested_output.parent) as folder:
        staged_table, staged_readme = Path(folder)/'requested', Path(folder)/'readme'
        _write(staged_table, requested_output, header, rows(), text_columns={FILL_COLUMN})
        staged_readme.write_text(readme_text(manifest, selected, files, missing, checks,
                                             dict(counts=filled_counts, dates=filled_dates)))
        os.replace(staged_table, requested_output)
        os.replace(staged_readme, readme)
    return dict(requested_file=requested_output.name, readme=readme.name, columns=len(selected),
                blank_values=dict(missing), filled_values=filled_counts, filled_dates=filled_dates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('full', type=Path, help='Full values table written by export_daily.py')
    parser.add_argument('--output', type=Path, help='Requested table; default: drop _full from the name')
    args = parser.parse_args()
    output = args.output or args.full.with_name(args.full.name.replace('_full', '', 1))
    if output == args.full:
        parser.error('Specify --output for a full table without _full in its name')
    checks_path = Path(str(args.full) + '.checks') / 'summary.json'
    checks = json.loads(checks_path.read_text()) if checks_path.exists() else None
    summary = write_delivery(args.full, output, companion(output, '_README', '.md'), checks)
    print(f"Wrote {summary['requested_file']} ({summary['columns']} variables) and {summary['readme']}")


if __name__ == '__main__':
    main()
