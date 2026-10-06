"""Export verified daily values and QC as aligned UTC project-day tables.

Requires completed annual or monthly manifests covering every requested day
for each source. Missing observations within that coverage remain null with
missing_record QC. CSV/CSV.gz use the standard library; Parquet needs pyarrow.
"""

import argparse
import calendar
from contextlib import closing
import csv
from datetime import date, timedelta
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from aggregate_daily import DAILY_FIELDS


QUALITY = ('qc', 'valid_hours', 'expected_hours', 'min_valid_area_fraction')
UNIT_SUFFIXES = {
    'mm': ('mm', ('mm',)), 'mm/day': ('mm_day', ('mm_day', 'mm')),
    'degC': ('degC', ('degc', 'c')), 'm': ('m', ('m',)),
    'm/s': ('m_s', ('m_s', 'ms')), 'kg/kg': ('kg_kg', ('kg_kg', 'kgkg')),
    'Pa': ('Pa', ('pa',)), 'kPa': ('kPa', ('kpa',)),
    'W/m2': ('W_m2', ('w_m2', 'wm2')), 'MJ/m2': ('MJ_m2', ('mj_m2', 'mjm2')),
    'kg/m3': ('kg_m3', ('kg_m3', 'kgm3')), 'm3/m3': ('m3_m3', ('m3_m3', 'm3m3')),
    '%': ('pct', ('pct',)), '1': ('fraction', ('fraction',)),
}
AORC_URL = 'https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/'
AORC_DAILY = {
    'APCP_surface': [('precipitation_mm', 'mm', 'Sum of 24 hour-ending water-equivalent amounts')],
    'TMP_2maboveground': [('tmean_c', 'degC', 'Mean of 24 hourly polygon means'),
        ('tmin_c', 'degC', 'Minimum of 24 hourly polygon means'),
        ('tmax_c', 'degC', 'Maximum of 24 hourly polygon means')],
    'SPFH_2maboveground': [('specific_humidity_kgkg', 'kg/kg', 'Mean of 24 hourly polygon means')],
    'PRES_surface': [('surface_pressure_pa', 'Pa', 'Mean of 24 hourly polygon means')],
    'UGRD_10maboveground': [('u_wind_ms', 'm/s', 'Mean of 24 hourly polygon means')],
    'VGRD_10maboveground': [('v_wind_ms', 'm/s', 'Mean of 24 hourly polygon means')],
    'DSWRF_surface': [('shortwave_down_mean_wm2', 'W/m2', 'Mean of 24 hourly polygon means'),
        ('shortwave_down_energy_mjm2', 'MJ/m2', 'Rectangular hourly integration of downward shortwave')],
    'DLWRF_surface': [('longwave_down_mean_wm2', 'W/m2', 'Mean of 24 hourly polygon means'),
        ('longwave_down_energy_mjm2', 'MJ/m2', 'Rectangular hourly integration of downward longwave')],
}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _date(text):
    result = date.fromisoformat(text)
    if result.isoformat() != text:
        raise ValueError('Dates must use YYYY-MM-DD')
    return result


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError(f'Unsafe source or variable identifier: {value!r}')
    return re.sub(r'[.-]', '_', value).lower()


def _value_column(source, variable, units):
    """Keep source identity and make the declared unit explicit without conversion."""
    if units not in UNIT_SUFFIXES:
        raise ValueError(f'Unsupported export unit: {units!r}')
    suffix, aliases = UNIT_SUFFIXES[units]
    name = _name(variable)
    for alias in aliases:
        if name.endswith('_' + alias):
            name = name[:-(len(alias)+1)]
            break
    return f'{_name(source)}__{name}_{suffix}'


def _schema(run):
    source = run.get('source_id')
    if source is None and run.get('source') == AORC_URL:
        source = 'aorc_v1.1'
    _name(source)
    schema = run.get('daily_schema')
    if schema is None and run.get('source') == AORC_URL:
        variables = run.get('variables', [])
        if not variables or any(v not in AORC_DAILY for v in variables):
            raise ValueError('Unidentified legacy AORC variable schema')
        schema = {name: dict(units=units, statistic=statistic)
                  for variable in variables for name, units, statistic in AORC_DAILY[variable]}
    if not isinstance(schema, dict) or not schema:
        raise ValueError('run.json requires an identified daily_schema')
    for variable, definition in schema.items():
        _name(variable)
        if (not isinstance(definition, dict) or not isinstance(definition.get('units'), str)
                or not definition['units'].strip()
                or not isinstance(definition.get('statistic', definition.get('description')), str)
                or not definition.get('statistic', definition.get('description', '')).strip()):
            raise ValueError(f'{source}/{variable}: schema needs units and a statistic or description')
    return source, schema


def _period(path):
    match = re.fullmatch(r'(year|month)_(\d{4})(?:-(\d{2}))?\.json', path.name)
    if not match or (match[1] == 'month') != (match[3] is not None):
        raise ValueError(f'Unrecognized period manifest: {path.name}')
    year = int(match[2])
    month = int(match[3]) if match[3] else None
    first = date(year, month or 1, 1)
    last = date(year, month, calendar.monthrange(year, month)[1]) if month else date(year, 12, 31)
    return path.stem.split('_', 1)[1], first, last


def _inputs(input_dirs, geo_hash, projects, start, end):
    inputs, tables, schemas, intervals, paths = [], [], {}, {}, set()
    for directory in map(Path, input_dirs):
        run_path = directory / 'run.json'
        run = json.loads(run_path.read_text())
        if run.get('geometry_sha256') != geo_hash:
            raise ValueError(f'{directory.name}: geometry hash differs from supplied GeoJSON')
        run_projects = run.get('projects')
        if (not isinstance(run_projects, list) or any(not isinstance(p, str) for p in run_projects)
                or len(set(run_projects)) != len(run_projects) or set(run_projects) != set(projects)):
            raise ValueError('Each source run must contain exactly the GeoJSON project set')
        if run.get('day') != 'UTC':
            raise ValueError('Source runs must use UTC days')
        first, last = _date(run['start']), _date(run['end'])
        if first > last or first > end or last < start:
            raise ValueError('Source run does not overlap the requested interval')
        for identifier, props in projects.items():
            recorded = run.get('project_forcing', {}).get(identifier, {})
            for key in ('catchment_role', 'diversion_intake_project', 'routing_requires_operations'):
                if key in props and recorded.get(key) != props[key]:
                    raise ValueError(f'{identifier}: source routing metadata differs from GeoJSON')
        source, schema = _schema(run)
        if source in schemas and schemas[source] != schema:
            raise ValueError(f'{source}: conflicting daily schemas across source runs')
        schemas[source] = schema
        intervals.setdefault(source, [])
        configuration_hash = hashlib.sha256(json.dumps(run, sort_keys=True).encode()).hexdigest()
        entry = dict(run_file=run_path.name, run_sha256=digest(run_path), metadata=run, periods=[])
        inputs.append(entry)
        paths.add(run_path.resolve())
        manifests = sorted([*directory.glob('year_*.json'), *directory.glob('month_*.json')])
        paths.update(path.resolve() for path in manifests)
        for table in directory.glob('daily*.csv*'):
            if not (table.name.endswith('.csv') or table.name.endswith('.csv.gz')):
                continue
            match = re.fullmatch(r'daily_(\d{4}(?:-\d{2})?)\.csv(?:\.gz)?', table.name)
            if not match:
                raise ValueError(f'Unidentified daily table: {table.name}')
            prefix = 'month' if len(match[1]) == 7 else 'year'
            if not (directory / f'{prefix}_{match[1]}.json').is_file():
                raise ValueError(f'Unmanifested daily table: {table.name}')
            paths.add(table.resolve())
        for manifest_path in manifests:
            token, period_first, period_last = _period(manifest_path)
            if period_first > end or period_last < start:
                continue
            record = json.loads(manifest_path.read_text())
            if record.get('configuration_sha256') != configuration_hash:
                raise ValueError(f'{manifest_path.name}: configuration hash mismatch')
            if record.get('status') not in {'complete', 'computed_with_gaps', 'computed_all_hours_valid'}:
                raise ValueError(f'{manifest_path.name}: period is not completed')
            covered_first = _date(record.get('start', str(max(first, period_first))))
            covered_last = _date(record.get('end', str(min(last, period_last))))
            if not max(first, period_first) <= covered_first <= covered_last <= min(last, period_last):
                raise ValueError(f'{manifest_path.name}: invalid period coverage')
            candidates = [directory / f'daily_{token}{suffix}' for suffix in ('.csv', '.csv.gz')]
            candidates = [path for path in candidates if path.is_file()]
            if len(candidates) != 1:
                raise ValueError(f'{manifest_path.name}: requires exactly one daily input file')
            table = candidates[0]
            if digest(table) != record.get('daily_sha256'):
                raise ValueError(f'{table.name}: daily hash mismatch')
            period = dict(manifest_file=manifest_path.name, manifest_sha256=digest(manifest_path),
                          daily_file=table.name, daily_sha256=record['daily_sha256'],
                          start=str(covered_first), end=str(covered_last), metadata=record)
            entry['periods'].append(period)
            tables.append((table, source, schema, period, covered_first, covered_last))
            if covered_first <= end and covered_last >= start:
                intervals[source].append((max(start, covered_first), min(end, covered_last)))
        if not entry['periods']:
            raise ValueError(f'{directory.name}: no completed manifests for requested coverage')
    for source, spans in intervals.items():
        cursor = start
        for first, last in sorted(spans):
            if first != cursor:
                raise ValueError(f'{source}: requested coverage has a gap or duplicate period')
            cursor = last + timedelta(days=1)
        if cursor != end + timedelta(days=1):
            raise ValueError(f'{source}: incomplete requested manifest coverage')
    return inputs, tables, schemas, paths


def _ingest(db, tables, projects, start, end):
    for path, source, schema, period, first, last in tables:
        column_names = {variable: _value_column(source, variable, definition['units'])
                        for variable, definition in schema.items()}
        count, batch = 0, []
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream, strict=True)
            if (reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames)
                    or set(reader.fieldnames) != set(DAILY_FIELDS)):
                raise ValueError(f'{path.name}: invalid daily schema')
            for row in reader:
                count += 1
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(f'{path.name}: malformed daily row')
                stamp = _date(row['date'])
                variable = row['variable']
                if (row['source'] != source or row['project_id'] not in projects
                        or variable not in schema or not first <= stamp <= last):
                    raise ValueError(f'{path.name}: unidentified source, variable, project or period')
                if row['units'] != schema[variable]['units']:
                    raise ValueError(f'{source}/{variable}: units conflict with declared schema')
                expected, valid = int(row['expected_hours']), int(row['valid_hours'])
                fraction = float(row['min_valid_area_fraction'])
                qc = row['qc']
                if (expected != 24 or not 0 <= valid <= expected or not math.isfinite(fraction)
                        or not 0 <= fraction <= 1 + 1e-9 or not qc or qc.strip() != qc):
                    raise ValueError(f'{path.name}: invalid daily QC or coverage')
                value = float(row['value']) if row['value'] else None
                if ((qc == 'valid' and (value is None or not math.isfinite(value)
                        or valid != expected or fraction < 1 - 1e-9))
                        or (qc != 'valid' and value is not None)):
                    raise ValueError(f'{path.name}: value contradicts QC; invalid values must be blank')
                if start <= stamp <= end:
                    batch.append((str(stamp), row['project_id'], column_names[variable],
                                  value, qc, valid, expected, min(fraction, 1.)))
                    if len(batch) >= 10000:
                        db.executemany('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)', batch)
                        batch.clear()
            db.executemany('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)', batch)
        declared_count = period['metadata'].get('daily_rows', period['metadata'].get('rows'))
        if declared_count is not None and count != declared_count:
            raise ValueError(f'{path.name}: daily row count differs from manifest')
        if digest(path) != period['daily_sha256']:
            raise ValueError(f'{path.name}: daily hash changed during export')
        period['rows_read'] = count
        db.commit()


def _rows(db, projects, bases, start, end, *, quality=False):
    offsets = {base: 2 + index * 5 for index, base in enumerate(bases)}
    cursor = iter(db.execute('SELECT * FROM observations ORDER BY date, project, column_name'))
    observation = next(cursor, None)
    stamp = start
    while stamp <= end:
        for project in sorted(projects):
            key = str(stamp), project
            row = [*key, *([None, 'missing_record', 0, 24, 0.] * len(bases))]
            while observation is not None and observation[:2] == key:
                offset = offsets[observation[2]]
                row[offset:offset + 5] = observation[3:]
                observation = next(cursor, None)
            yield row[:2] + ([value for i in range(len(bases)) for value in row[3+i*5:7+i*5]]
                             if quality else row[2::5])
        stamp += timedelta(days=1)


def _write(path, output, columns, rows, *, quality=False):
    if output.suffix == '.parquet':
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as error:
            raise ValueError('Parquet export requires the optional pyarrow package') from error
        types = [pa.date32(), pa.string()]
        if quality:
            types.extend([pa.string(), pa.int16(), pa.int16(), pa.float64()] * ((len(columns)-2)//4))
        else:
            types.extend([pa.float64()] * (len(columns)-2))
        schema = pa.schema(zip(columns, types))
        with pq.ParquetWriter(path, schema, compression='snappy') as writer:
            batch = []
            for row in rows:
                row[0] = _date(row[0])
                batch.append(dict(zip(columns, row)))
                if len(batch) >= 2048:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    else:
        opener = gzip.open if output.name.endswith('.csv.gz') else open
        with opener(path, 'wt', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)


def export_daily(geojson, input_dirs, start, end, output):
    """Verify periods, stage both tables, and roll back failed file publication."""
    geojson, output = Path(geojson), Path(output)
    companion = Path(str(output) + '.manifest.json')
    if start > end or not input_dirs:
        raise ValueError('Specify input directories and an ordered date interval')
    if not output.name.endswith(('.csv', '.csv.gz', '.parquet')):
        raise ValueError('Output must end in .csv, .csv.gz or .parquet')
    suffix = '.csv.gz' if output.name.endswith('.csv.gz') else output.suffix
    qc_output = output.with_name(output.name[:-len(suffix)] + '_qc' + suffix)
    destinations = [output, qc_output, companion]
    if any(path.is_dir() for path in destinations):
        raise ValueError('Outputs and companion manifest must be files, not directories')
    doc = json.loads(geojson.read_text())
    if doc.get('type') != 'FeatureCollection' or doc.get('crs') or not doc.get('features'):
        raise ValueError('Use a nonempty WGS84 GeoJSON FeatureCollection')
    projects, parts = {}, set()
    for feature in doc['features']:
        props = feature.get('properties', {})
        identifier, part = props.get('id'), props.get('part')
        if (not isinstance(identifier, str) or not identifier or identifier.strip() != identifier
                or identifier in projects or part not in {'local', 'total'}):
            raise ValueError('GeoJSON requires unique project IDs and explicit local/total part')
        projects[identifier] = props
        parts.add(part)
    if len(parts) != 1:
        raise ValueError('Do not mix local and total catchments')
    geo_hash = digest(geojson)
    inputs, tables, schemas, paths = _inputs(input_dirs, geo_hash, projects, start, end)
    paths.add(geojson.resolve())
    if any(path.resolve() in paths for path in destinations):
        raise ValueError('Output must not overwrite an input file')
    if len({path.resolve() for path in destinations}) != len(destinations):
        raise ValueError('Output files must not refer to the same path')
    columns = {'date': dict(description='UTC calendar date'),
               'project_id': dict(description='Canonical GeoJSON project identifier')}
    qc_columns = dict(columns)
    if len({_name(source) for source in schemas}) != len(schemas):
        raise ValueError('Source prefix collision after sanitizing identifiers')
    bases = []
    for source, schema in sorted(schemas.items()):
        for variable, definition in sorted(schema.items()):
            base = _value_column(source, variable, definition['units'])
            names = [base, *[base + '__' + key for key in QUALITY]]
            if base in columns:
                raise ValueError(f'Export column name collision: {base}')
            bases.append(base)
            columns[base] = dict(source=source, variable=variable, **definition)
            for key, name in zip(QUALITY, names[1:]):
                qc_columns[name] = dict(source=source, variable=variable, value_column=base, quality_field=key,
                                        description=f'{key} for {base}')
    report = dict(schema_version=2, output_file=output.name, qc_file=qc_output.name, start=str(start), end=str(end),
                  day='UTC', row_key=['date', 'project_id'],
                  row_count=((end - start).days + 1) * len(projects),
                  project_count=len(projects), catchment_part=next(iter(parts)),
                  geometry_file=geojson.name, geometry_sha256=geo_hash,
                  project_metadata=projects, geometry_metadata=doc.get('metadata', {}),
                  columns=columns, qc_columns=qc_columns, inputs=inputs,
                  missing_policy='Null value and missing_record QC for absent observations within verified period coverage; incomplete manifest coverage is rejected',
                  exporter_sha256=digest(__file__))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.daily-export-', dir=output.parent) as folder:
        temporary, quality_table, metadata = (Path(folder)/name for name in ('values', 'quality', 'manifest.json'))
        with closing(sqlite3.connect(Path(folder) / 'pivot.sqlite')) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.execute('PRAGMA cache_size=-16384')
            db.execute('CREATE TABLE observations (date TEXT, project TEXT, column_name TEXT, '
                       'value REAL, qc TEXT, valid_hours INTEGER, expected_hours INTEGER, '
                       'min_valid_area_fraction REAL, PRIMARY KEY(date, project, column_name)) WITHOUT ROWID')
            try:
                _ingest(db, tables, projects, start, end)
            except sqlite3.IntegrityError as error:
                raise ValueError('Duplicate source-variable observation for a project and date') from error
            _write(temporary, output, list(columns), _rows(db, projects, bases, start, end))
            _write(quality_table, qc_output, list(qc_columns),
                   _rows(db, projects, bases, start, end, quality=True), quality=True)
        report['output_sha256'] = digest(temporary)
        report['qc_output_sha256'] = digest(quality_table)
        metadata.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        backups, published = {}, []
        for index, target in enumerate(destinations):
            if target.exists() or target.is_symlink():
                backups[target] = Path(folder)/f'previous_{index}'
                os.link(target, backups[target], follow_symlinks=False)
        try:
            # The manifest is the commit marker: consumers verify both output hashes.
            for staged, target in zip([temporary, quality_table, metadata], destinations):
                staged.replace(target)
                published.append(target)
        except OSError:
            for target in reversed(published):
                if target in backups:
                    backups[target].replace(target)
                else:
                    target.unlink()
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geojson', required=True, type=Path)
    parser.add_argument('--input-dir', required=True, nargs='+', type=Path)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    parser.add_argument('--output', required=True, type=Path, help='Values table; also writes _qc and manifest siblings')
    args = parser.parse_args()
    try:
        report = export_daily(args.geojson, args.input_dir, args.start, args.end, args.output)
    except (ValueError, OSError, csv.Error) as error:
        parser.error(str(error))
    print(f"Wrote {report['row_count']:,} daily project rows to {args.output}")
    print(f"Quality fields: {args.output.with_name(report['qc_file'])}")


if __name__ == '__main__':
    main()
