#!/usr/bin/env python3
"""Merge the requested tables of separate deliveries into one table with its README.

Each input is a requested table written by download_daily.py or deliver_daily.py,
with its full table's manifest beside it (INPUT_full.csv.manifest.json; .csv.gz
and .parquet full tables also work). The inputs must have the same columns and
dates and different catchments, for example the AORC delivery of the Columbia
catchments and the ERA5-Land delivery of the Peace River catchments. The output keeps
the column order and sorts rows by date and project_id. Beside it are written
OUTPUT_README.md, OUTPUT.manifest.json (column variables, units and sources,
read by plot_daily.py) and, with --geojson, the combined catchment polygons as
OUTPUT_catchments.geojson.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import tempfile

from deliver_daily import FILL_COLUMN, column_units, companion, readme_text, requested_columns
from delivery_variables import AORC, LAND
from export_daily import digest


def full_manifest(table):
    """Manifest of the full table that a requested table was made from."""
    for suffix in ('.csv', '.csv.gz', '.parquet'):
        path = Path(str(companion(table, '_full', suffix)) + '.manifest.json')
        if path.exists():
            return json.loads(path.read_text())
    raise ValueError(f'{table.name}: no full-table manifest found beside it ({companion(table, "_full", ".csv")}.manifest.json)')


def read_table(path, names):
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq
    if path.suffix == '.parquet':
        table = pq.read_table(path)
    else:
        types = {'date': pa.date32(), 'project_id': pa.string(), FILL_COLUMN: pa.string(),
                 **{name: pa.float64() for name in names}}
        table = pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(column_types=types, strings_can_be_null=True))
    columns = ['date', 'project_id', *names, FILL_COLUMN]
    if table.column_names != columns:
        raise ValueError(f'{path.name}: columns differ from the requested table layout')
    schema = pa.schema([('date', pa.date32()), ('project_id', pa.string()),
                        *[(name, pa.float64()) for name in names], (FILL_COLUMN, pa.string())])
    return table.cast(schema)


def merge(inputs, output, geojsons=()):
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
    parts, tables, primary, projects = [], [], {}, {}
    for path in inputs:
        manifest = full_manifest(path)
        selected = requested_columns(manifest)
        names = [name for name, _, _ in selected]
        source = LAND if any(item.get('replaces') == AORC for _, _, item in selected) else AORC
        overlap = projects.keys() & manifest['project_metadata'].keys()
        if overlap:
            raise ValueError(f'{path.name}: catchments already in another input: {sorted(overlap)}')
        projects.update(manifest['project_metadata'])
        primary.setdefault(source, []).extend(sorted(manifest['project_metadata']))
        parts.append((path, manifest, selected, names))
        tables.append(read_table(path, names))
    path, manifest, selected, names = next((p for p in parts if p[2] and any(
        item['source'] == AORC for _, _, item in p[2])), parts[0])
    for other, other_manifest, other_selected, other_names in parts:
        if other_names != names:
            raise ValueError(f'{other.name}: requested columns differ from {path.name}')
        if (other_manifest['start'], other_manifest['end']) != (manifest['start'], manifest['end']):
            raise ValueError(f"{other.name} covers {other_manifest['start']} to {other_manifest['end']}, "
                             f"{path.name} {manifest['start']} to {manifest['end']}; deliver both for the same dates")
        for (name, full, _), (_, other_full, _) in zip(selected, other_selected):
            if column_units(manifest, full) != column_units(other_manifest, other_full):
                raise ValueError(f'{name}: units differ between {path.name} and {other.name}')
    table = pa.concat_tables(tables).sort_by([('date', 'ascending'), ('project_id', 'ascending')])
    missing = Counter({name: table[name].null_count for name in names})
    filled, dates = Counter(), set()
    for day, flags in zip(table['date'].to_pylist(), table[FILL_COLUMN].to_pylist()):
        if flags:
            filled.update(flags.split(';'))
            dates.add(day.isoformat())
    merged = dict(start=manifest['start'], end=manifest['end'], row_count=table.num_rows,
                  project_metadata=dict(sorted(projects.items())), columns=manifest['columns'])
    readme = readme_text(merged, selected, output.name, missing,
                         dict(counts={name: filled[name] for name in names if filled[name]}, dates=sorted(dates)),
                         primary=primary)
    polygons = None
    if geojsons:
        features = [feature for geojson in geojsons for feature in json.loads(Path(geojson).read_text())['features']]
        ids = [feature['properties']['id'] for feature in features]
        if sorted(ids) != sorted(projects):
            raise ValueError('The GeoJSON catchments differ from the merged table')
        polygons = dict(type='FeatureCollection', features=features)
    output.parent.mkdir(parents=True, exist_ok=True)
    targets = {'table': output, 'readme': companion(output, '_README', '.md'),
               'manifest': Path(str(output) + '.manifest.json')}
    if polygons:
        targets['geojson'] = companion(output, '_catchments', '.geojson')
    with tempfile.TemporaryDirectory(prefix='.merge-', dir=output.parent) as folder:
        staged = {key: Path(folder) / key for key in targets}
        if output.suffix == '.parquet':
            pq.write_table(table, staged['table'], compression='snappy')
        else:
            pacsv.write_csv(table, staged['table'])
            if output.name.endswith('.gz'):
                import gzip
                import shutil
                with staged['table'].open('rb') as source, gzip.open(Path(folder) / 'gz', 'wb') as target:
                    shutil.copyfileobj(source, target)
                os.replace(Path(folder) / 'gz', staged['table'])
        staged['readme'].write_text(readme)
        if polygons:
            staged['geojson'].write_text(json.dumps(polygons) + '\n')
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
    return dict(rows=table.num_rows, catchments=len(projects), files=[t.name for t in targets.values()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path, help='Requested tables of the deliveries to merge')
    parser.add_argument('--output', required=True, type=Path, help='Merged requested table (.parquet, .csv or .csv.gz)')
    parser.add_argument('--geojson', nargs='+', type=Path, default=(),
                        help='Catchment GeoJSON of every input, merged into OUTPUT_catchments.geojson')
    args = parser.parse_args()
    summary = merge(args.inputs, args.output, args.geojson)
    print(f"Wrote {summary['rows']:,} rows for {summary['catchments']} catchments: {', '.join(summary['files'])}")


if __name__ == '__main__':
    main()
