#!/usr/bin/env python3
"""Merge the requested tables of separate deliveries into one table with its README.

Each input is a requested table, for example the AORC delivery of the Columbia
catchments (or that delivery extended with merge_daily.py) and the ERA5-Land
delivery of the Peace River catchments. The inputs must have the same columns
and dates and different catchments. A table with its full table's manifest
beside it (INPUT_full.csv.manifest.json; .csv.gz and .parquet full tables also
work) is described by that manifest; a table without one is taken as an AORC
delivery, and at least one input needs a manifest. --geojson gives the polygons
of every input. The output keeps the column order and sorts rows by date and
project_id. Beside it are written OUTPUT_README.md, OUTPUT.manifest.json (column
variables, units and sources, read by plot_daily.py) and the combined catchment
polygons as OUTPUT_catchments.geojson.
"""
import argparse
from collections import Counter
import csv
import gzip
import json
import os
from pathlib import Path
import shutil
import tempfile

from deliver_daily import FILL_COLUMN, column_units, companion, readme_text, requested_columns
from delivery_variables import AORC, LAND
from export_daily import digest


def full_manifest(table):
    """Manifest of the full table that a requested table was made from, or None."""
    for suffix in ('.csv', '.csv.gz', '.parquet'):
        path = Path(str(companion(table, '_full', suffix)) + '.manifest.json')
        if path.exists():
            return json.loads(path.read_text())
    return None


def read_table(path):
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq
    if path.suffix == '.parquet':
        table = pq.read_table(path)
    else:
        with (gzip.open if path.name.endswith('.gz') else open)(path, 'rt', newline='') as stream:
            columns = next(csv.reader(stream))
        types = {name: pa.float64() for name in columns[2:-1]}
        types.update({'date': pa.date32(), 'project_id': pa.string(), FILL_COLUMN: pa.string()})
        table = pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(column_types=types, strings_can_be_null=True))
    names = table.column_names
    if names[:2] != ['date', 'project_id'] or names[-1] != FILL_COLUMN:
        raise ValueError(f'{path.name} is not a requested-variable table')
    schema = pa.schema([('date', pa.date32()), ('project_id', pa.string()),
                        *[(name, pa.float64()) for name in names[2:-1]], (FILL_COLUMN, pa.string())])
    return table.cast(schema)


def merge(inputs, output, geojsons):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq
    inputs, output = [Path(p) for p in inputs], Path(output)
    if len(inputs) < 2:
        raise ValueError('Give at least two requested tables')
    if output.resolve() in {p.resolve() for p in inputs}:
        raise ValueError('The output must not overwrite an input')
    if not output.name.endswith(('.csv', '.csv.gz', '.parquet')):
        raise ValueError('Output must end in .csv, .csv.gz or .parquet')
    features = [feature for geojson in geojsons for feature in json.loads(Path(geojson).read_text())['features']]
    metadata = {feature['properties']['id']: feature['properties'] for feature in features}
    if len(metadata) != len(features):
        raise ValueError('A catchment appears in more than one GeoJSON')
    parts, tables, primary, seen = [], [], {}, set()
    for path in inputs:
        table = read_table(path)
        names = table.column_names[2:-1]
        manifest = full_manifest(path)
        selected = requested_columns(manifest) if manifest else None
        if selected and [name for name, _, _ in selected] != names:
            raise ValueError(f'{path.name}: columns differ from its full-table manifest')
        catchments = set(table['project_id'].unique().to_pylist())
        if seen & catchments:
            raise ValueError(f'{path.name}: catchments already in another input: {sorted(seen & catchments)}')
        if catchments - metadata.keys():
            raise ValueError(f'{path.name}: catchments missing from the GeoJSON: {sorted(catchments - metadata.keys())}')
        seen |= catchments
        source = LAND if selected and any(item.get('replaces') == AORC for _, _, item in selected) else AORC
        primary.setdefault(source, []).extend(sorted(catchments))
        dates = (pc.min(table['date']).as_py(), pc.max(table['date']).as_py())
        parts.append((path, manifest, selected, names, dates))
        tables.append(table)
    if seen != metadata.keys():
        raise ValueError('The GeoJSON catchments differ from the merged tables')
    described = [part for part in parts if part[1]]
    if not described:
        raise ValueError('At least one input needs its full-table manifest beside it')
    path, manifest, selected, names, dates = described[0]
    for other, other_manifest, other_selected, other_names, other_dates in parts:
        if other_names != names:
            raise ValueError(f'{other.name}: requested columns differ from {path.name}')
        if other_dates != dates:
            raise ValueError(f'{other.name} covers {other_dates[0]} to {other_dates[1]}, '
                             f'{path.name} {dates[0]} to {dates[1]}; give both the same dates')
        if other_manifest:
            for (name, full, _), (_, other_full, _) in zip(selected, other_selected):
                if column_units(manifest, full) != column_units(other_manifest, other_full):
                    raise ValueError(f'{name}: units differ between {path.name} and {other.name}')
    table = pa.concat_tables(tables).sort_by([('date', 'ascending'), ('project_id', 'ascending')])
    missing = Counter({name: table[name].null_count for name in names})
    filled, filled_dates = Counter(), set()
    for day, flags in zip(table['date'].to_pylist(), table[FILL_COLUMN].to_pylist()):
        if flags:
            filled.update(flags.split(';'))
            filled_dates.add(day.isoformat())
    merged = dict(start=dates[0].isoformat(), end=dates[1].isoformat(), row_count=table.num_rows,
                  project_metadata=dict(sorted(metadata.items())), columns=manifest['columns'])
    # The inputs may fill AORC gaps in different ways (a later year joined with merge_daily.py,
    # for example), so the README describes the filled values without naming one method.
    readme = readme_text(merged, selected, output.name, missing,
                         dict(counts={name: filled[name] for name in names if filled[name]},
                              dates=sorted(filled_dates), generic=True), primary=primary)
    output.parent.mkdir(parents=True, exist_ok=True)
    targets = {'table': output, 'readme': companion(output, '_README', '.md'),
               'manifest': Path(str(output) + '.manifest.json'),
               'geojson': companion(output, '_catchments', '.geojson')}
    with tempfile.TemporaryDirectory(prefix='.merge-', dir=output.parent) as folder:
        staged = {key: Path(folder) / key for key in targets}
        if output.suffix == '.parquet':
            pq.write_table(table, staged['table'], compression='snappy')
        else:
            pacsv.write_csv(table, staged['table'])
            if output.name.endswith('.gz'):
                with staged['table'].open('rb') as source, gzip.open(Path(folder) / 'gz', 'wb') as target:
                    shutil.copyfileobj(source, target)
                os.replace(Path(folder) / 'gz', staged['table'])
        staged['readme'].write_text(readme)
        staged['geojson'].write_text(json.dumps(dict(type='FeatureCollection', features=features)) + '\n')
        record = dict(schema_version=1, kind='requested_table', output_file=output.name,
                      output_sha256=digest(staged['table']), start=merged['start'], end=merged['end'], day='UTC',
                      row_count=table.num_rows, project_metadata=merged['project_metadata'], primary_sources=primary,
                      fill_column=FILL_COLUMN,
                      requested_columns={name: dict(variable=item['variable'], units=column_units(manifest, full),
                                                    source=item['source'])
                                         for name, full, item in selected},
                      inputs=[dict(file=p.name, sha256=digest(p)) for p, *_ in parts])
        staged['manifest'].write_text(json.dumps(record, indent=2) + '\n')
        for key, target in targets.items():
            os.replace(staged[key], target)
    return dict(rows=table.num_rows, catchments=len(metadata), files=[t.name for t in targets.values()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path, help='Requested tables of the deliveries to merge')
    parser.add_argument('--output', required=True, type=Path, help='Merged requested table (.parquet, .csv or .csv.gz)')
    parser.add_argument('--geojson', nargs='+', required=True, type=Path, help='Catchment GeoJSON of every input')
    args = parser.parse_args()
    summary = merge(args.inputs, args.output, args.geojson)
    print(f"Wrote {summary['rows']:,} rows for {summary['catchments']} catchments: {', '.join(summary['files'])}")


if __name__ == '__main__':
    main()
