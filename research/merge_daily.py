"""Join requested-variable tables: the first file whole, each later file only after the last date so far.

    python research/merge_daily.py outputs/catchment_daily_1996_2025.parquet outputs/catchment_daily_2022_2026.csv --output outputs/catchment_daily_1996_2026.parquet

The files need the same columns in the same order and the same catchments on
every date, and the dates must run on without a gap. A REPLACED catchment
(delivery_variables.py) must have its AORC replaced by debias_era5.py, every
row flagged, in all the files or in none. CSV and Parquet inputs can be mixed;
the output format follows its suffix.
"""

import argparse
import csv
from datetime import date, timedelta
import gzip
from pathlib import Path
import sys

from deliver_daily import FILL_COLUMN
from delivery_variables import REPLACED
from export_daily import _write


def read(path):
    """Column names and rows: ISO date, project, floats or None, and the fill flag or None."""
    if path.suffix == '.parquet':
        import pyarrow.parquet as pq
        source = pq.ParquetFile(path)
        names = source.schema_arrow.names

        def rows():
            for batch in source.iter_batches(batch_size=4096):
                for record in batch.to_pylist():
                    yield [str(record['date']), *[record[name] for name in names[1:]]]
        return names, rows()
    stream = (gzip.open if path.name.endswith('.gz') else open)(path, 'rt', newline='')
    reader = csv.reader(stream)
    names = next(reader)

    def rows():
        with stream:
            for row in reader:
                yield [row[0], row[1], *[float(value) if value else None for value in row[2:-1]], row[-1] or None]
    return names, rows()


def merge(paths, output):
    paths, output = [Path(path) for path in paths], Path(output)
    if output.resolve() in {path.resolve() for path in paths}:
        raise ValueError('The output must not overwrite an input')
    tables = [read(path) for path in paths]
    header = tables[0][0]
    if header[:2] != ['date', 'project_id'] or header[-1] != FILL_COLUMN:
        raise ValueError(f'{paths[0].name} is not a requested-variable table')
    for path, (names, _) in zip(paths, tables):
        if names != header:
            raise ValueError(f'{path.name}: columns differ from {paths[0].name}')
    added, flagged = [], {}

    def rows():
        day, projects, expected = None, [], None
        for path, (_, source) in zip(paths, tables):
            start, count = day, 0
            for row in source:
                current = date.fromisoformat(row[0])
                if start is not None and current <= start:
                    continue
                if current != day:
                    if day is not None:
                        if current != day + timedelta(days=1):
                            raise ValueError(f'{path.name}: {current} follows {day}; dates must be consecutive')
                        if expected is None:
                            expected = projects
                        elif projects != expected:
                            raise ValueError(f'{path.name}: catchments on {day} differ from the first date')
                    day, projects = current, []
                projects.append(row[1])
                if row[1] in REPLACED:
                    flagged.setdefault(row[1], {}).setdefault(path.name, set()).add(bool(row[-1]))
                count += 1
                yield row
            added.append((path.name, count, start))
        if expected is not None and projects != expected:
            raise ValueError(f'Catchments on {day} differ from the first date')
        for project, files in sorted(flagged.items()):
            replaced = [name for name, kinds in files.items() if kinds == {True}]
            if replaced and len(replaced) < len(files):
                raise ValueError(f'{project}: AORC is replaced in {", ".join(replaced)} only; '
                                 'write every file with debias_era5.py')

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.part')
    try:
        _write(temporary, output, header, rows(), text_columns={FILL_COLUMN})
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    for name, count, start in added:
        print(f'{name}: {count:,} rows' + (f' after {start}' if start else ''), flush=True)
    print(f'Wrote {output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tables', nargs='+', type=Path, help='Requested tables, earliest first')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        merge(args.tables, args.output)
    except (ValueError, OSError) as error:
        print(f'Failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
