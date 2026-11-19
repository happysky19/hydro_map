"""The extended-range and seasonal table of one day, from the latest run of each system, mapped onto the history.

    python -m catchment_forecast.extended --date 2026-11-18 --output OPS/extended/20261118.parquet \\
        --table ec46=OPS/ec46_adjusted/2026111600.parquet --table cfs=OPS/cfs_adjusted/2026111718.parquet ...

The runs are older than the date, as each source comes later than the daily
runs (scripts/daily_extended.sbatch picks them):

- ec46: ECMWF's extended range of two days before, which the S2S archive opens
  48 h after its start: 101 members to 46 days, so to day 44 from the date;
- gefs: GEFS of the day before, which reaches 35 days a day after its start;
- cfs: CFSv2, the four runs of each of the last days, a lagged ensemble to
  about six months;
- seas5: SEAS5 of the latest month released, 51 members to six months.

Each --table is one run's adjusted table (adjust apply with the system's
mapping). The table of the day has init_time, system, member, lead_day (from
the run's own start), valid_date, project_id and the twelve columns of COLUMNS,
for valid dates from the date on. A CFSv2 member is named after its run, as in
2026111718_01, so every member of a system has its own name.
"""

import argparse
from datetime import date
from pathlib import Path
import sys


KEYS = ['init_time', 'system', 'member', 'lead_day', 'valid_date', 'project_id']
COLUMNS = ['precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'tmean_degC', 'tmax_degC', 'tmin_degC',
           'shortwave_down_mean_W_m2', 'relative_humidity_pct', 'net_radiation_mean_W_m2', 'wind_speed_m_s',
           'vapor_pressure_deficit_kPa', 'soil_temperature_layer1_degC']
SYSTEMS = ('ec46', 'gefs', 'cfs', 'seas5')


def assemble(day, tables):
    """One table from [(system, path)], rows valid from day on."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    parts = []
    for system, path in tables:
        table = pq.read_table(path)
        table = table.filter(pc.greater_equal(table['valid_date'], pa.scalar(day, pa.date32())))
        if not table.num_rows:
            continue
        member = table['member'].to_numpy(zero_copy_only=False).astype(str)
        if system == 'cfs':
            init = table['init_time'][0].as_py()
            member = np.char.add(f'{init:%Y%m%d%H}_', member)
        columns = {'init_time': table['init_time'], 'system': pa.array([system] * table.num_rows),
                   'member': pa.array(member), 'lead_day': table['lead_day'], 'valid_date': table['valid_date'],
                   'project_id': table['project_id']}
        for column in COLUMNS:
            columns[column] = (table[column].cast(pa.float64()) if column in table.column_names
                               else pa.array(np.full(table.num_rows, np.nan)))
        parts.append(pa.table(columns))
    if not parts:
        raise ValueError(f'No forecast rows valid from {day}')
    return pa.concat_tables(parts)


def main(argv=None):
    import pyarrow.parquet as pq
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--date', required=True, type=date.fromisoformat, help='The day of the table')
    parser.add_argument('--output', required=True, type=Path, help='The table to write (Parquet)')
    parser.add_argument('--table', action='append', default=[], metavar='SYSTEM=PATH',
                        help=f'An adjusted run of one of {", ".join(SYSTEMS)}; repeat for each run')
    args = parser.parse_args(argv)
    tables = []
    for item in args.table:
        system, _, path = item.partition('=')
        if system not in SYSTEMS or not path:
            parser.error(f'--table {item}: not SYSTEM=PATH with SYSTEM one of {", ".join(SYSTEMS)}')
        tables.append((system, Path(path)))
    if not tables:
        print(f'No run for {args.date}: nothing written', file=sys.stderr)
        return 1
    table = assemble(args.date, tables)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix('.part')
    pq.write_table(table, partial, compression='zstd')
    partial.replace(args.output)
    systems = table['system'].to_numpy(zero_copy_only=False)
    members = table['member'].to_numpy(zero_copy_only=False)
    valid = table['valid_date'].to_numpy(zero_copy_only=False)
    for system in SYSTEMS:
        rows = systems == system
        if rows.any():
            print(f'{system}: {len(set(members[rows]))} members, valid {valid[rows].min()} to {valid[rows].max()}, '
                  f'{rows.sum():,} rows', flush=True)
    print(f'Wrote {args.output}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
