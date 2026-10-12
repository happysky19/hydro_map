"""Write the requested-variable table and its README from a verified full daily export.

The requested table copies selected columns of the full values table. Where an
AORC value is blank (a source gap), it is filled from the same ERA5-Land
quantity adjusted to AORC's mean for that catchment and calendar month, and the
row's `aorc_gap_filled` column names every filled column. The full table is
never filled. The README describes the requested table: every column's source,
whether it is a native source field or calculated here, its units and daily
definition, the limitations of the data and the matching operational forecast
fields.
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
from delivery_variables import AORC, ERA5, LAND, REQUESTED, SOURCE_NAMES
from export_daily import _write, digest
from meteorology import hargreaves_pet


FILL_COLUMN = 'aorc_gap_filled'
# Signed quantities are shifted by the mean difference; nonnegative ones scaled by the mean ratio.
ADDITIVE = {'tmean_c', 'tmin_c', 'tmax_c', 'relative_humidity_pct'}
RATIO = {'precipitation_mm', 'shortwave_down_mean_wm2', 'vapor_pressure_deficit_kpa'}
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


def _gap_text(gaps):
    if not gaps['counts']:
        return []
    dates = gaps['dates']
    when = ', '.join(dates) if len(dates) <= 20 else f'{len(dates)} dates from {dates[0]} to {dates[-1]}'
    counts = ', '.join(f'`{name}` {count:,}' for name, count in gaps['counts'].items())
    return [f'- AORC has missing hours on {when}. There, the AORC value is replaced by the same '
            'ERA5-Land quantity, shifted (temperature, relative humidity) or scaled (precipitation, '
            "shortwave radiation, VPD) to AORC's mean for that catchment and calendar month; filled "
            "precipitation is split into rain and snow with ERA5-Land's snow fraction and PET is recomputed. "
            f'`{FILL_COLUMN}` names the filled columns of each row. Filled project-days: {counts}.']


def readme_text(manifest, selected, name, missing, gaps):
    start, end = manifest['start'], manifest['end']
    projects = manifest['project_metadata']
    used = lambda source: ', '.join(dict.fromkeys(item['quantity'].split(' (')[0]
                                                  for _, _, item in selected if item['source'] == source))
    blanks = ', '.join(f'`{column}` {count:,}' for column, count in missing.items() if count)
    if name.endswith('.parquet'):
        reading = f"Read it with `pandas.read_parquet('{name}')`; `date` is a date column and blank values are nulls."
    else:
        reading = f"Read it with `pandas.read_csv('{name}', parse_dates=['date'])`; blank values are empty."
    lines = [
        '# Daily catchment weather and land-surface data', '',
        f"Daily weather and land-surface variables for {len(projects)} dam catchments, {start} to {end}.", '',
        '## File', '',
        f"`{name}` has {manifest['row_count']:,} rows, one per catchment and day, and {len(selected) + 3} "
        f"columns: `date`, `project_id`, {len(selected)} value columns and `{FILL_COLUMN}`.", '',
        f'- {reading}',
        '- `date` is a UTC calendar day, which begins in the late afternoon of the previous local day '
        '(00 UTC is 16:00 PST).',
        '- `project_id` identifies the catchment (list at the end). Each catchment is the area draining to '
        'the dam minus the areas draining to listed dams upstream of it.',
        '- Every value is an area-weighted mean over the catchment, with grid cells on the boundary weighted '
        'by the fraction inside it. Daily values are computed from hourly catchment means as described '
        'for each variable.',
        '- Column names end with their units: `mm` is a daily amount (mm/day), `W_m2` a daily mean flux, '
        'and `degC`, `kPa`, `m3_m3`, `m_s`, `m`, `pct` (%) and `fraction` (0-1) are as written.',
        f'- `{FILL_COLUMN}` lists the columns of that row filled where AORC has no data (see Limitations); '
        'it is empty otherwise.',
        f"- Blank values: {blanks or 'none'}.", '',
        '## Sources', '',
        _table([['AORC v1.1', 'NOAA Office of Water Prediction', 'Analysis of observations',
                 'Hourly, 30 arc-seconds (about 1 km)', used(AORC)],
                ['ERA5-Land', 'ECMWF (Copernicus)', 'Land reanalysis', 'Hourly, 0.1° (about 9 km)', used(LAND)],
                ['ERA5', 'ECMWF (Copernicus)', 'Atmospheric reanalysis', 'Hourly, 0.25° (about 31 km)', used(ERA5)]],
               ['Source', 'Producer', 'Type', 'Grid', 'Used for']), '',
        '- **AORC** (Analysis of Record for Calibration) is the forcing NOAA uses to calibrate the National '
        'Water Model. It combines NLDAS-2, the URMA analysis and, over the United States, NEXRAD Stage IV '
        'radar-gauge precipitation, adjusted to the PRISM and Livneh gauge climatologies '
        '([methods](https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf)). '
        'Read from the AORC v1.1 Zarr archive on AWS.',
        '- **ERA5-Land** reruns the land component of ERA5 at 0.1°, driven by ERA5 weather with temperature '
        'adjusted for elevation. Read from the Copernicus Climate Data Store or its copy on the Destination '
        'Earth Data Hub.',
        '- **ERA5** is the ECMWF global atmospheric reanalysis. Read from the Copernicus Climate Data Store or '
        'the ARCO-ERA5 copy on Google Cloud.', '',
        '## Variables', '',
        f"{len({item['quantity'].split(' (')[0] for _, _, item in selected})} variables in {len(selected)} "
        'columns; soil temperature is given for the four ERA5-Land layers. "Native" means the source field '
        '(units converted only); "computed" means calculated here from native fields as described. AORC '
        'precipitation sums the hour-ending amounts 01-24 UTC; ERA5-Land amounts and fluxes use the '
        '24 hours ending at 24 UTC.', '',
        _table([[item['category'], item['quantity'], f'`{column}`', _units(manifest['columns'][full]),
                 SOURCE_NAMES[item['source']], item['origin'], item['definition']]
                for column, full, item in selected],
               ['Category', 'Variable', 'Column', 'Units', 'Source', 'Native or computed', 'Daily definition']), '',
        '## Limitations', '',
        '- AORC is an estimate built from observations, not a measurement. Over the United States its '
        'precipitation follows the Stage IV radar-gauge analyses from 2002; over Canada it follows NLDAS-2, '
        'and NOAA reports lower accuracy there because gauges are sparse. Precipitation at high elevation is '
        'the least certain.',
        '- AORC changes its inputs over time and does not make the record homogeneous. Temperature, humidity, '
        'pressure and longwave radiation switch from NLDAS-2 to URMA in 2016, and temperature is adjusted to '
        'the earlier record only in 2016-2017. Means and trends across 2016-2018 can include these changes, '
        'also in rainfall, snowfall, relative humidity, VPD and PET, which are computed from them. AORC wind '
        'changes source at the start of 2018 and drops by about 1.5-3 m/s at these catchments, so wind is '
        'taken from ERA5-Land.',
        '- ERA5-Land and ERA5 are model reanalyses on 9 km and 31 km grids and smooth out valleys and ridges. '
        "ERA5-Land wind is ERA5's 31 km wind interpolated to the finer grid and is likely too low in complex "
        'terrain ([Chen et al. 2024](https://rmets.onlinelibrary.wiley.com/doi/10.1002/joc.8339)).',
        '- Snow, soil moisture, soil temperature and actual evapotranspiration are ERA5-Land model output '
        'driven by ERA5 precipitation, not by AORC, so they do not close a water balance with the AORC '
        "precipitation. Absolute soil moisture depends on the model's soil properties; changes and anomalies "
        'are more reliable than levels.',
        '- Glacier cells in ERA5-Land carry permanent deep snow. SWE and snow depth leave out cells holding at '
        'least 5 m water equivalent all day; snowmelt includes them.',
        '- Rainfall and snowfall are split from AORC precipitation by a wet-bulb threshold of 0.5 °C in each '
        'cell and hour; precipitation phase is not observed.',
        '- Daily maximum and minimum temperature are extremes of the hourly catchment-mean series, so they are '
        'milder than station or grid-cell extremes.',
        '- PET uses Hargreaves-Samani, which depends only on temperature and extraterrestrial radiation and is '
        'not calibrated for these catchments.',
        '- Freezing level above ground is 0 when ERA5 finds no 0 °C level; ERA5 stores this as about '
        '-0.0002 m, so small negative values mean 0.',
        *_gap_text(gaps), '',
        '## Matching forecast variables', '',
        'Fields in operational forecasts that correspond to each column. IFS names are ECMWF open-data '
        'parameters (0.25°) unless marked as full archive. AIFS names are ECMWF AIFS Single v2 open-data '
        'parameters (0.25°, 6-hourly, operational since 12 May 2026; AIFS ENS v2 has the same fields); its '
        'land output is limited to snow-cover fraction and the top two soil layers (0-28 cm). GFS names are '
        '0.25° GRIB2 `VARIABLE:level`. WeatherNext 2 output is 6-hourly (2 m temperature, 10 m and 100 m '
        'wind, mean sea-level pressure, 6-hour precipitation and pressure-level fields). ECMWF accumulations '
        '(tp, ssrd) run from the forecast start, so daily amounts are differences between steps.', '',
        'To use a forecast with a model trained on this table, aggregate it the same way (same catchment '
        'polygons, UTC days, catchment means before daily statistics, same formulas for computed columns) '
        'and bias-correct it toward this table over a common past period, because every forecast model has '
        'its own climatology. IFS and AIFS (trained on ERA5 and IFS analyses) are closest to the ERA5-Land '
        'and ERA5 columns; the AORC columns need the correction most.', '',
        _table([[item['quantity'], f'`{column}`', item['ifs'], item['aifs'], item['gfs'], item['wn2'], item['note']]
                for column, _, item in selected],
               ['Variable', 'Column', 'ECMWF IFS', 'ECMWF AIFS', 'NOAA GFS', 'WeatherNext 2', 'Note']), '',
        '## Catchments', '',
        _table([[identifier, props.get('name', ''), props.get('river', ''), props.get('country', ''),
                 _number(props.get('area_local', props.get('area')), 6)]
                for identifier, props in sorted(projects.items())],
               ['project_id', 'Name', 'River', 'Country', 'Area (km2)']), '']
    return '\n'.join(lines)


def _float(value):
    return math.nan if value is None or value == '' else float(value)


def fill_aorc_gaps(values, land, projects, dates, latitudes):
    """Fill blank AORC values from ERA5-Land adjusted to AORC per catchment and calendar month.

    values/land map AORC variable names to arrays over rows (land may lack a variable).
    Returns {variable: boolean array of filled rows}; values are updated in place.
    Precipitation is split by ERA5-Land's snow fraction so rain + snow = total, and
    temperature order and the RH range stay consistent.
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
    if {'pet_hargreaves_mm', 'tmean_c', 'tmin_c', 'tmax_c'} <= set(values):
        for row in np.flatnonzero(gaps['pet_hargreaves_mm']):
            stats = [values[name][row] for name in ('tmean_c', 'tmin_c', 'tmax_c')]
            latitude = latitudes.get(projects[row])
            if latitude is not None and all(math.isfinite(value) for value in stats):
                values['pet_hargreaves_mm'][row] = hargreaves_pet(*stats, latitude, dates[row])
                filled['pet_hargreaves_mm'][row] = True
    return filled


def write_delivery(full_output, requested_output, readme):
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

    header = ['date', 'project_id', *[name for name, _, _ in selected], FILL_COLUMN]
    with tempfile.TemporaryDirectory(prefix='.delivery-', dir=requested_output.parent) as folder:
        staged_table, staged_readme = Path(folder)/'requested', Path(folder)/'readme'
        _write(staged_table, requested_output, header, rows(), text_columns={FILL_COLUMN})
        staged_readme.write_text(readme_text(manifest, selected, requested_output.name, missing,
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
    summary = write_delivery(args.full, output, companion(output, '_README', '.md'))
    print(f"Wrote {summary['requested_file']} ({summary['columns']} variables) and {summary['readme']}")


if __name__ == '__main__':
    main()
