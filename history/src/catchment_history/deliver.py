"""Write the requested-variable table and its README from a verified full daily export.

The requested table copies selected columns of the full values table. A
delivery without AORC (catchments north of AORC's coverage, 53°N) takes the AORC
columns from ERA5-Land and computes PET from its temperatures. Nothing is
filled: AORC blanks stay blank and `aorc_gap_filled` is empty. The debias command
writes the delivered AORC columns from the full table, with the blanks filled
and the REPLACED catchments replaced. The README describes the requested table:
every column's source, whether it is a native source field or calculated here,
its units and daily definition, the limitations of the data and the matching
operational forecast fields.
"""

import argparse
from collections import Counter
from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np

from .check import table_rows
from .delivery_variables import AORC, ERA5, LAND, LAND_ORIGIN, RECENT, REFERENCE, REQUESTED, SOURCE_NAMES
from .export import _value_column, _write
from .files import digest
from .meteorology import hargreaves_pet


FILL_COLUMN = 'aorc_gap_filled'


def companion(path, tag, suffix=None):
    """Sibling path with a tag before the table suffix, e.g. x.csv -> x_full.csv."""
    path = Path(path)
    table_suffix = '.csv.gz' if path.name.endswith('.csv.gz') else path.suffix
    stem = path.name[:-len(table_suffix)] if table_suffix else path.name
    return path.with_name(stem + tag + (table_suffix if suffix is None else suffix))


def requested_columns(manifest):
    """Map requested column names to full-table columns; every requested field must exist.

    Without AORC columns the AORC entries read ERA5-Land; PET, which ERA5-Land lacks,
    has no full-table column (None) and is computed from the ERA5-Land temperatures.
    """
    by_variable = {(info.get('source'), info.get('variable')): column
                   for column, info in manifest['columns'].items()}
    has_aorc = any(source == AORC for source, _ in by_variable)
    selected = []
    for item in REQUESTED:
        if item['source'] == AORC and not has_aorc:
            item = dict(item, source=LAND, replaces=AORC)
            if (LAND, item['variable']) not in by_variable and item['variable'] == 'pet_hargreaves_mm':
                selected.append((_value_column(LAND, item['variable'], 'mm/day').split('__', 1)[1], None, item))
                continue
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


def column_units(manifest, full):
    """Units of a requested column; a column computed here (full is None) is PET in mm/day."""
    return 'mm/day' if full is None else _units(manifest['columns'][full])


def _gap_text(gaps):
    if not gaps or not gaps['counts']:
        return []
    dates = gaps['dates']
    when = ', '.join(dates) if len(dates) <= 20 else f'{len(dates)} dates from {dates[0]} to {dates[-1]}'
    counts = ', '.join(f'`{name}` {count:,}' for name, count in gaps['counts'].items())
    return [f'- Where AORC has no data ({when}), the AORC columns are filled from ERA5-Land quantile-mapped onto '
            f'AORC for that catchment and calendar month. `{FILL_COLUMN}` names the filled columns of each row. '
            f'Filled project-days: {counts}.']


def _names(projects, identifiers):
    order = sorted(identifiers, key=lambda i: (-float(projects[i].get('lat', 0)), i))   # north to south
    names = [projects[i].get('name', i).split(' (')[0] for i in order]
    return ', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else names[0]


def readme_text(manifest, selected, name, missing, gaps=None, primary=None, replaced=(), precipitation_from=None):
    """gaps gives the counts and dates of filled values (None: nothing is filled); primary maps AORC and
    ERA5-Land to the catchments whose AORC columns they supply; replaced lists the AORC catchments whose
    AORC columns hold mapped ERA5-Land on every day; precipitation_from is the date from which the other
    AORC catchments' precipitation, rain and snow hold mapped ERA5-Land."""
    start, end = manifest['start'], manifest['end']
    projects = manifest['project_metadata']
    role = lambda item: item.get('replaces', item['source'])
    if primary is None:
        primary = {item['source']: sorted(projects) for _, _, item in selected if role(item) == AORC}
    north = primary.get(LAND, [])
    both = AORC in primary and LAND in primary
    others = [*north, *replaced]
    primary_name = ' or '.join(SOURCE_NAMES[source] for source in (AORC, LAND)
                               if source in primary or (source == LAND and replaced))
    used = lambda source: ', '.join(dict.fromkeys(item['quantity'].split(' (')[0]
                                                  for _, _, item in selected if role(item) == source))
    origin = lambda item: (item['origin'] if role(item) != AORC or LAND not in primary
                           else LAND_ORIGIN.get(item['variable'], item['origin']) if not both
                           else f"{item['origin']} (ERA5-Land: {LAND_ORIGIN[item['variable']]})"
                           if LAND_ORIGIN.get(item['variable'], item['origin']) != item['origin'] else item['origin'])
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
        (f'- `{FILL_COLUMN}` lists the columns of that row filled where AORC has no data'
         + (' or is replaced' if replaced or precipitation_from else '') + ' (see Limitations); it is empty otherwise.'
         if replaced or precipitation_from or _gap_text(gaps) else f'- `{FILL_COLUMN}` is empty: no values are filled.'),
        f"- Blank values: {blanks or 'none'}.", '',
        '## Sources', '',
        _table([*([['AORC v1.1', 'NOAA Office of Water Prediction', 'Analysis of observations',
                    'Hourly, 30 arc-seconds (about 1 km)',
                    used(AORC) + (f' (all catchments except {_names(projects, others)})' if others else '')]]
                  if AORC in primary else []),
                ['ERA5-Land', 'ECMWF (Copernicus)', 'Land reanalysis', 'Hourly, 0.1° (about 9 km)',
                 used(LAND) + (f'; {used(AORC).lower()} for {_names(projects, north)}' if north else '')
                 + (f"; {'the same' if north else used(AORC).lower()} mapped onto AORC for "
                    f'{_names(projects, replaced)}' if replaced else '')
                 + (f'; precipitation, rainfall and snowfall mapped onto AORC from {precipitation_from[:4]}'
                    if precipitation_from else '')],
                ['ERA5', 'ECMWF (Copernicus)', 'Atmospheric reanalysis', 'Hourly, 0.25° (about 31 km)', used(ERA5)]],
               ['Source', 'Producer', 'Type', 'Grid', 'Used for']), '',
        *(['- **AORC** (Analysis of Record for Calibration) is the forcing NOAA uses to calibrate the National '
           'Water Model. It combines NLDAS-2, the URMA analysis and, over the United States, NEXRAD Stage IV '
           'radar-gauge precipitation, adjusted to the PRISM and Livneh gauge climatologies '
           '([methods](https://www.weather.gov/media/owp/operations/aorc_v1_1_methods.pdf)). '
           'Read from the AORC v1.1 Zarr archive on AWS.'] if AORC in primary else []),
        '- **ERA5-Land** reruns the land component of ERA5 at 0.1°, driven by ERA5 weather with temperature '
        'adjusted for elevation. Read from the Copernicus Climate Data Store or its copy on the Destination '
        'Earth Data Hub.' + (f" For {_names(projects, north)}, which {'lies' if len(north) == 1 else 'lie'} "
                             "north of AORC's coverage (53°N), "
                             'ERA5-Land also supplies the AORC variables: precipitation is its 24-hour total, '
                             'snowfall is its snowfall and rainfall the rest, temperature and humidity are at 2 m, '
                             'and PET uses the same Hargreaves formula with ERA5-Land temperature.' if north else ''),
        '- **ERA5** is the ECMWF global atmospheric reanalysis. Read from the Copernicus Climate Data Store or '
        'the ARCO-ERA5 copy on Google Cloud.', '',
        '## Variables', '',
        f"{len({item['quantity'].split(' (')[0] for _, _, item in selected})} variables in {len(selected)} "
        'columns; soil temperature is given for the four ERA5-Land layers. "Native" means the source field '
        '(units converted only); "computed" means calculated here from native fields as described. '
        + ('AORC precipitation sums the hour-ending amounts 01-24 UTC; ' if AORC in primary else '')
        + 'ERA5-Land amounts and fluxes use the 24 hours ending at 24 UTC.', '',
        _table([[item['category'], item['quantity'], f'`{column}`', column_units(manifest, full),
                 primary_name if role(item) == AORC else SOURCE_NAMES[item['source']], origin(item),
                 item['definition']]
                for column, full, item in selected],
               ['Category', 'Variable', 'Column', 'Units', 'Source', 'Native or computed', 'Daily definition']), '',
        '## Limitations', '',
        *(['- AORC is an estimate built from observations, not a measurement. Over the United States its '
           'precipitation follows the Stage IV radar-gauge analyses from 2002; over Canada it follows NLDAS-2, '
           'and NOAA reports lower accuracy there because gauges are sparse. Precipitation at high elevation is '
           'the least certain.',
           '- AORC changes its inputs over time and does not make the record homogeneous. Temperature, humidity, '
           'pressure and longwave radiation switch from NLDAS-2 to URMA in 2016, and temperature is adjusted to '
           'the earlier record only in 2016-2017. Means and trends across 2016-2018 can include these changes, '
           'also in rainfall, snowfall, relative humidity, VPD and PET, which are computed from them. AORC wind '
           'changes source at the start of 2018 and drops by about 1.5-3 m/s at these catchments, so wind is '
           'taken from ERA5-Land.'] if AORC in primary else []),
        '- ERA5-Land and ERA5 are model reanalyses on 9 km and 31 km grids and smooth out valleys and ridges. '
        "ERA5-Land wind is ERA5's 31 km wind interpolated to the finer grid and is likely too low in complex "
        'terrain ([Chen et al. 2024](https://rmets.onlinelibrary.wiley.com/doi/10.1002/joc.8339)).',
        '- Snow, soil moisture, soil temperature and actual evapotranspiration are ERA5-Land model output '
        'driven by ERA5 precipitation' + (', not by AORC, so they do not close a water balance with the AORC '
                                          'precipitation' if AORC in primary else '')
        + ". Absolute soil moisture depends on the model's soil properties; changes and anomalies "
        'are more reliable than levels.',
        '- Glacier cells in ERA5-Land carry permanent deep snow. SWE and snow depth leave out cells holding at '
        'least 5 m water equivalent all day; snowmelt includes them.',
        ('- Rainfall and snowfall are split from AORC precipitation by a wet-bulb threshold of 0.5 °C in each '
         'cell and hour; precipitation phase is not observed.' if AORC in primary else
         "- Rainfall and snowfall are ERA5-Land's modelled snowfall and the remaining precipitation.")
        + (f" For {_names(projects, north)} they are ERA5-Land's modelled snowfall and the remaining "
           'precipitation.' if both else ''),
        '- Daily maximum and minimum temperature are extremes of the hourly catchment-mean series, so they are '
        'milder than station or grid-cell extremes.',
        '- PET uses Hargreaves-Samani, which depends only on temperature and extraterrestrial radiation and is '
        'not calibrated for these catchments.',
        '- Freezing level above ground is 0 when ERA5 finds no 0 °C level; ERA5 stores this as about '
        '-0.0002 m, so small negative values mean 0.',
        *([f'- From late 2007 to 2017 AORC precipitation over {_names(projects, replaced)} falls well below '
            'other years, in winter to about a third in the northern catchments, while snow courses and snow '
            "pillows in the Mica basin show no drop. Until 2015 AORC's Canadian precipitation is NLDAS-2 "
            'adjusted to the Livneh gauge climatology; from 2016 it follows gauge analyses without that '
            'adjustment. In these catchments every AORC column therefore holds ERA5-Land quantile-mapped onto '
            f'AORC of {REFERENCE[0][:4]}-{REFERENCE[1][:4]} for that catchment and calendar month, on every day. '
            "Precipitation is split into rain and snow by AORC's snow fraction against daily mean temperature "
            'over the same years, and PET is recomputed from the mapped temperatures. The levels and seasonal '
            'cycle follow AORC of those years; the day-to-day and year-to-year changes follow ERA5-Land. '
            f'`{FILL_COLUMN}` names these columns on every row of these catchments.'] if replaced else []),
        *([f'- From {precipitation_from} AORC precipitation east of the Cascades has far more July-September wet '
           'days than before, relative to SNOTEL gauges and to ERA5-Land, after tracking both through 2022, while '
           'its temperature, humidity and radiation did not change. From that date `precipitation_mm`, '
           '`rainfall_mm` and `snowfall_mm` of every AORC catchment therefore hold ERA5-Land quantile-mapped onto '
           f'AORC of {RECENT[0][:4]}-{RECENT[1][:4]} for that catchment and calendar month, split into rain and '
           "snow by AORC's snow fraction against daily mean temperature at the day's own AORC temperature; the "
           f'other AORC columns keep AORC. `{FILL_COLUMN}` names these three columns on those rows.']
          if precipitation_from else []),
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
        'and ERA5 columns' + ('; the AORC columns need the correction most.' if AORC in primary else '.'), '',
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


def write_delivery(full_output, requested_output, readme):
    """Write the requested columns and the README; returns a summary."""
    full_output, requested_output, readme = map(Path, (full_output, requested_output, readme))
    manifest = json.loads(Path(str(full_output) + '.manifest.json').read_text())
    if digest(full_output) != manifest['output_sha256']:
        raise ValueError(f'{full_output.name}: hash differs from its manifest')
    if requested_output.resolve() in {full_output.resolve(), full_output.with_name(manifest['qc_file']).resolve()}:
        raise ValueError('Requested table must not overwrite the full delivery')
    selected = requested_columns(manifest)
    by_variable = {(info.get('source'), info.get('variable')): column
                   for column, info in manifest['columns'].items()}
    # Without AORC, PET is the same Hargreaves estimate from the ERA5-Land temperature statistics.
    pet = [name for name, column, _ in selected if column is None]
    temperatures = [by_variable[LAND, variable] for variable in ('tmean_c', 'tmin_c', 'tmax_c')] if pet else []
    columns = {column for _, column, _ in selected if column} | set(temperatures)
    keys, data = [], {column: [] for column in columns}
    for record in table_rows(full_output, manifest['columns']):
        keys.append((str(record['date']), record['project_id']))
        for column in columns:
            data[column].append(_float(record[column]))
    data = {column: np.array(series, dtype=float) for column, series in data.items()}
    computed = {}
    if pet:
        dates = [date.fromisoformat(day) for day, _ in keys]
        projects = [project for _, project in keys]
        latitudes = next((entry['metadata'].get('pet_centroid_latitudes') or {}
                          for entry in manifest.get('inputs', [])
                          if entry['metadata'].get('source_id') == AORC), None) or manifest.get('pet_centroid_latitudes', {})
        if set(projects) - set(latitudes):
            raise ValueError('PET needs catchment centroid latitudes; export the full table again')
        computed = {name: np.array([hargreaves_pet(*stats, latitudes[project], day)
                                    if all(math.isfinite(value) for value in stats) else math.nan
                                    for stats, project, day in zip(zip(*(data[c] for c in temperatures)),
                                                                   projects, dates)])
                    for name in pet}
    parquet = requested_output.suffix == '.parquet'
    blank = None if parquet else ''
    missing = Counter()

    def rows():
        for row, (day, project) in enumerate(keys):
            output = []
            for name, column, _ in selected:
                value = data[column][row] if column else computed[name][row]
                if math.isnan(value):
                    missing[name] += 1
                    value = blank
                output.append(value)
            yield [day, project, *output, blank]

    header = ['date', 'project_id', *[name for name, _, _ in selected], FILL_COLUMN]
    with tempfile.TemporaryDirectory(prefix='.delivery-', dir=requested_output.parent) as folder:
        staged_table, staged_readme = Path(folder)/'requested', Path(folder)/'readme'
        _write(staged_table, requested_output, header, rows(), text_columns={FILL_COLUMN})
        staged_readme.write_text(readme_text(manifest, selected, requested_output.name, missing))
        os.replace(staged_table, requested_output)
        os.replace(staged_readme, readme)
    return dict(requested_file=requested_output.name, readme=readme.name, columns=len(selected),
                blank_values=dict(missing))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='catchment-history deliver', description=__doc__)
    parser.add_argument('full', type=Path, help='Full values table written by catchment-history download')
    parser.add_argument('--output', type=Path, help='Requested table; default: drop _full from the name')
    args = parser.parse_args(argv)
    output = args.output or args.full.with_name(args.full.name.replace('_full', '', 1))
    if output == args.full:
        parser.error('Specify --output for a full table without _full in its name')
    summary = write_delivery(args.full, output, companion(output, '_README', '.md'))
    print(f"Wrote {summary['requested_file']} ({summary['columns']} variables) and {summary['readme']}")
