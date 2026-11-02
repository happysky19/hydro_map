"""Compare forecast lead days with the history table on the same dates and catchments.

    python forecast/scripts/compare_history.py OUTPUT/aifs [OUTPUT/wn2] --history DELIVERY.parquet [--lead 1 5 10]

Each FORECAST is a folder of one system's start tables, or one table file, and
is compared on its own. For each lead day the ensemble mean of every column the
history table also has is compared with the history value of the same date and
catchment: the number of pairs, both means, their mean difference and the
correlation across dates and catchments.
"""

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


def compare(paths, history_path, leads):
    forecast = pa.concat_tables([pq.read_table(path) for path in paths])
    forecast = forecast.filter(pc.is_in(forecast['lead_day'], pa.array(leads, forecast['lead_day'].type)))
    history_columns = pq.read_schema(history_path).names
    columns = [name for name in forecast.column_names[5:] if name in history_columns]
    means = forecast.group_by(['lead_day', 'valid_date', 'project_id']).aggregate([(name, 'mean') for name in columns])
    history = pq.read_table(history_path, columns=['date', 'project_id', *columns])
    pairs = means.join(history, keys=['valid_date', 'project_id'], right_keys=['date', 'project_id'])
    print(f"{'lead':>4} {'column':<28} {'pairs':>6} {'forecast':>9} {'history':>9} {'diff':>8} {'corr':>6}")
    for lead in leads:
        rows = pairs.filter(pc.equal(pairs['lead_day'], lead))
        for name in columns:
            x = rows[f'{name}_mean'].to_numpy(zero_copy_only=False).astype(float)
            y = rows[name].to_numpy(zero_copy_only=False).astype(float)
            ok = np.isfinite(x) & np.isfinite(y)
            if ok.sum() > 2:
                corr = np.corrcoef(x[ok], y[ok])[0, 1]
                print(f'{lead:>4} {name:<28} {ok.sum():>6} {x[ok].mean():>9.3f} {y[ok].mean():>9.3f} '
                      f'{(x[ok] - y[ok]).mean():>8.3f} {corr:>6.2f}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('forecast', nargs='+', type=Path)
    parser.add_argument('--history', required=True, type=Path)
    parser.add_argument('--lead', nargs='+', type=int, default=[1, 5, 10])
    args = parser.parse_args()
    for item in args.forecast:
        paths = sorted(item.glob('[0-9]' * 10 + '.parquet')) if item.is_dir() else [item]
        print(f'\n{item}: {len(paths)} start tables; ensemble means against {args.history.name}')
        compare(paths, args.history, args.lead)


if __name__ == '__main__':
    main()
