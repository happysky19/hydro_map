"""Map forecast catchment values onto the history table the inflow model was trained on.

    python -m catchment_forecast.adjust hindcast --forecast DIR/aifs --history DELIVERY.parquet \\
        --geojson CATCHMENTS --output DIR/aifs_adjusted
    python -m catchment_forecast.adjust apply --mapping DIR/aifs_adjusted/mapping.parquet \\
        --forecast START.parquet [START.parquet ...] --output DIR

Each column is quantile-mapped onto the history values of the same dates and
catchments, per catchment, lead group (LEAD_GROUPS) and calendar month of the
valid date with the months either side, as debias maps ERA5-Land onto AORC.
The fit pools every member and each member is mapped on its own, so the member
count may differ between the hindcast and operations. Precipitation is then
split into rain and snow by the catchment's snow-fraction curve, fitted on the
history table, at the mapped mean temperature; the temperatures are put in
order and PET is recalculated from them, as in the history table.

hindcast maps the starts of each year with a fit on the other years, so each
adjusted hindcast is as independent of its own weather as an operational
forecast, and writes check.csv, which compares the raw and adjusted ensembles
with the history. It then fits every year together into mapping.parquet, which
apply uses on operational starts: one mapping and one code path for both.
"""

import argparse
import json
from pathlib import Path
import sys

import numpy as np

from catchment_history.debias import LEVELS, best_curves, mapped, phase_errors, snow_fraction
from catchment_history.meteorology import hargreaves_pet

from .aggregate import COLUMNS, Catchments


KEYS = ['init_time', 'member', 'lead_day', 'valid_date', 'project_id']
START = '[0-9]' * 10 + '.parquet'
# First lead day of each group; the last group runs on to every later lead day.
LEAD_GROUPS = (1, 2, 3, 4, 6, 8, 11, 15)
# Beyond the fitted quantiles signed quantities shift and nonnegative ones scale (see debias.mapped).
ADDITIVE = {'tmean_degC', 'tmax_degC', 'tmin_degC', 'relative_humidity_pct', 'u_wind_m_s', 'v_wind_m_s',
            'net_radiation_mean_W_m2', 'actual_evapotranspiration_mm', 'freezing_level_above_sea_level_m',
            *(f'soil_temperature_layer{i}_degC' for i in range(1, 5))}
RATIO = {'precipitation_mm', 'shortwave_down_mean_W_m2', 'vapor_pressure_deficit_kPa', 'wind_speed_m_s',
         'cloud_cover_fraction', 'snow_water_equivalent_mm', 'snow_depth_m', 'snowmelt_mm',
         'freezing_level_above_ground_m', 'soil_moisture_layer1_m3_m3', 'root_zone_soil_moisture_0_100cm_m3_m3',
         'soil_moisture_layer4_m3_m3'}
MIN_ROWS = 200
PHASE = ('precipitation_mm', 'snowfall_mm', 'tmean_degC')
OUTPUT = ['precipitation_mm', 'rainfall_mm', 'snowfall_mm', *COLUMNS[1:]]
CHECK = ['column', 'lead_day', 'project_id', 'cases', 'history_mean', 'raw_bias', 'adjusted_bias',
         'raw_mae', 'adjusted_mae', 'raw_crps', 'adjusted_crps']


def read_starts(paths):
    """(start tables sorted by member, lead day and catchment, their rows as one dict of arrays)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    tables = [pq.read_table(path).sort_by([('member', 'ascending'), ('lead_day', 'ascending'),
                                           ('project_id', 'ascending')]) for path in paths]
    table = pa.concat_tables(tables)
    # The keys stay in the tables; the rows keep what the mapping needs.
    rows = {name: table[name].to_numpy(zero_copy_only=False) for name in table.column_names
            if name not in ('init_time', 'member')}
    rows['valid_date'] = rows['valid_date'].astype('datetime64[D]')
    rows['year'] = table['init_time'].to_numpy(zero_copy_only=False).astype('datetime64[Y]').astype(int) + 1970
    return tables, rows


def lookup(path, projects, dates, columns):
    """History values of these columns at the given catchments and dates; NaN where it has none."""
    import pyarrow.parquet as pq
    table = pq.read_table(path, columns=['date', 'project_id', *columns])
    days = table['date'].to_numpy(zero_copy_only=False).astype('datetime64[D]')
    names, codes = np.unique(np.r_[table['project_id'].to_numpy(zero_copy_only=False), projects],
                             return_inverse=True)
    first = min(days.min(), dates.min())
    index = (np.r_[days, dates] - first).astype(int)
    out = {}
    for column in columns:
        grid = np.full((len(names), index.max() + 1), np.nan)
        grid[codes[:len(days)], index[:len(days)]] = table[column].to_numpy(zero_copy_only=False)
        out[column] = grid[codes[len(days):], index[len(days):]]
    return out


def group_codes(rows, projects):
    """Group of each row: (catchment * len(LEAD_GROUPS) + lead group) * 12 + month of the valid date - 1."""
    position = {project: i for i, project in enumerate(projects)}
    missing = set(rows['project_id']) - set(position)
    if missing:
        raise ValueError(f'No mapping for {", ".join(sorted(missing))}')
    codes = np.array([position[project] for project in rows['project_id']])
    lead = np.searchsorted(LEAD_GROUPS, rows['lead_day'], side='right') - 1
    month = rows['valid_date'].astype('datetime64[M]').astype(int) % 12
    return (codes * len(LEAD_GROUPS) + lead) * 12 + month


def fit_quantiles(x, y, codes, count):
    """Quantiles at LEVELS of forecast x and history y for each group, from the rows of its month and
    the months either side that hold both; NaN for a group with fewer than MIN_ROWS of them."""
    ok = np.flatnonzero(np.isfinite(x) & np.isfinite(y))
    order = ok[np.argsort(codes[ok], kind='stable')]
    bounds = np.searchsorted(codes[order], np.arange(count + 1))
    xq, yq = np.full((count, len(LEVELS)), np.nan), np.full((count, len(LEVELS)), np.nan)
    for group in range(count):
        base, month = divmod(group, 12)
        near = np.concatenate([order[bounds[g]:bounds[g + 1]] for g in base * 12 + (month + np.arange(-1, 2)) % 12])
        if len(near) >= MIN_ROWS:
            xq[group], yq[group] = np.quantile(x[near], LEVELS), np.quantile(y[near], LEVELS)
    return xq, yq


def fit(rows, history, columns, projects, curves, latitudes, info):
    """The mapping of these forecast rows onto the history values of the same rows."""
    codes = group_codes(rows, projects)
    count = len(projects) * len(LEAD_GROUPS) * 12
    return dict(projects=list(projects), curves=curves, latitudes=latitudes, info=info,
                quantiles={column: fit_quantiles(rows[column], history[column], codes, count) for column in columns})


def map_column(values, codes, xq, yq, ratio, projects):
    result = np.full(len(values), np.nan)
    order = np.argsort(codes, kind='stable')
    for at in np.split(order, np.flatnonzero(np.diff(codes[order])) + 1):
        group = codes[at[0]]
        if np.isnan(xq[group, 0]):
            base, month = divmod(group, 12)
            project, lead = divmod(base, len(LEAD_GROUPS))
            raise ValueError(f'No fit for {projects[project]}, lead days from {LEAD_GROUPS[lead]}, month {month + 1}')
        q, index = np.unique(xq[group], return_inverse=True)
        result[at] = mapped(values[at], q, np.bincount(index, yq[group]) / np.bincount(index), ratio)
    return result


def adjust(rows, mapping):
    """Adjusted columns of forecast rows: mapped, then rain and snow, ordered temperatures and PET."""
    codes = group_codes(rows, mapping['projects'])
    values = {column: map_column(rows[column], codes, xq, yq, column in RATIO, mapping['projects'])
              for column, (xq, yq) in mapping['quantiles'].items() if column in rows}
    t = values['tmean_degC']
    values['tmin_degC'] = np.fmin(values['tmin_degC'], t)
    values['tmax_degC'] = np.fmax(values['tmax_degC'], t)
    if 'relative_humidity_pct' in values:
        values['relative_humidity_pct'] = np.clip(values['relative_humidity_pct'], 0, 100)
    if 'cloud_cover_fraction' in values:
        values['cloud_cover_fraction'] = np.minimum(values['cloud_cover_fraction'], 1.)
    centre, width = np.array([mapping['curves'][project] for project in rows['project_id']]).T
    values['snowfall_mm'] = values['precipitation_mm'] * snow_fraction(t, centre, width)
    values['rainfall_mm'] = values['precipitation_mm'] - values['snowfall_mm']
    # hargreaves_pet is (tmean + 17.8) sqrt(tmax - tmin) times a factor of latitude and day, its value at
    # tmean -16.8, tmin -16.8 and tmax -15.8; that factor is computed once per catchment and date.
    keys, inverse = np.unique(np.c_[codes // (len(LEAD_GROUPS) * 12), rows['valid_date'].astype(int)],
                              axis=0, return_inverse=True)
    factor = np.array([hargreaves_pet(-16.8, -16.8, -15.8, mapping['latitudes'][mapping['projects'][code]],
                                      np.datetime64(int(day), 'D').astype(object)) for code, day in keys])
    with np.errstate(invalid='ignore'):
        pet = factor[inverse.ravel()] * (t + 17.8) * np.sqrt(values['tmax_degC'] - values['tmin_degC'])
    values['pet_hargreaves_mm_day'] = np.maximum(pet, 0.)
    return {column: values[column] for column in OUTPUT if column in values}


def save(mapping, path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    parts = []
    for column, (xq, yq) in mapping['quantiles'].items():
        groups = np.flatnonzero(np.isfinite(xq[:, 0]))
        base, month = np.divmod(groups, 12)
        project, lead = np.divmod(base, len(LEAD_GROUPS))
        repeat = lambda array: np.repeat(array, len(LEVELS))
        parts.append(pa.table({
            'column': [column] * len(groups) * len(LEVELS),
            'project_id': repeat(np.array(mapping['projects'], dtype=object)[project]),
            'first_lead_day': repeat(np.array(LEAD_GROUPS)[lead]).astype('int16'),
            'month': repeat(month + 1).astype('int8'),
            'level': np.tile(LEVELS, len(groups)),
            'forecast': xq[groups].ravel(), 'history': yq[groups].ravel()}))
    info = dict(mapping['info'], projects=mapping['projects'], lead_groups=LEAD_GROUPS, levels=len(LEVELS),
                curves=mapping['curves'], latitudes=mapping['latitudes'])
    table = pa.concat_tables(parts)
    pq.write_table(table.replace_schema_metadata({'catchment_forecast': json.dumps(info)}), path,
                   compression='zstd')


def load(path):
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    info = json.loads(table.schema.metadata[b'catchment_forecast'])
    if tuple(info['lead_groups']) != LEAD_GROUPS or info['levels'] != len(LEVELS):
        raise ValueError(f'{path} was fitted with other lead groups or quantile levels')
    projects = info['projects']
    position = {project: i for i, project in enumerate(projects)}
    count = len(projects) * len(LEAD_GROUPS) * 12
    rows = {name: table[name].to_numpy(zero_copy_only=False) for name in table.column_names}
    quantiles = {}
    for column in np.unique(rows['column']):
        at = np.flatnonzero(rows['column'] == column)[::len(LEVELS)]
        groups = ((np.array([position[p] for p in rows['project_id'][at]]) * len(LEAD_GROUPS)
                   + np.searchsorted(LEAD_GROUPS, rows['first_lead_day'][at])) * 12 + rows['month'][at] - 1)
        xq, yq = np.full((count, len(LEVELS)), np.nan), np.full((count, len(LEVELS)), np.nan)
        span = at[:, None] + np.arange(len(LEVELS))
        xq[groups], yq[groups] = rows['forecast'][span], rows['history'][span]
        quantiles[column] = xq, yq
    curves = {project: tuple(curve) for project, curve in info.pop('curves').items()}
    return dict(projects=projects, curves=curves, latitudes=info.pop('latitudes'), info=info, quantiles=quantiles)


def write(tables, rows, values, output):
    """One adjusted table per start, beside the keys of its raw table."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    offset = 0
    for table in tables:
        part = slice(offset, offset + table.num_rows)
        adjusted = table.select(KEYS)
        for column, array in values.items():
            adjusted = adjusted.append_column(column, pa.array(array[part]))
        name = table['init_time'][0].as_py().strftime('%Y%m%d%H')
        pq.write_table(adjusted, output / f'{name}.part', compression='zstd')
        (output / f'{name}.part').replace(output / f'{name}.parquet')
        offset += table.num_rows


def crps(members, truth):
    """Ensemble CRPS of each column of members (members x cases) against truth."""
    m = len(members)
    ordered = np.sort(members, axis=0)
    spread = 2 * ((2 * np.arange(1, m + 1) - m - 1)[:, None] * ordered).sum(axis=0) / m ** 2
    return np.abs(members - truth).mean(axis=0) - spread / 2


def check(tables, rows, values, history, path):
    """check.csv: per column, lead day and catchment (and ALL), the history mean and the bias, mean absolute
    error of the ensemble mean and CRPS of the raw and the adjusted forecasts."""
    import csv
    projects = sorted(set(rows['project_id']))
    leads = int(rows['lead_day'].max())
    sums = {}
    offset = 0
    for table in tables:
        members = len(set(table['member'].to_pylist()))
        part = slice(offset, offset + table.num_rows)
        offset += table.num_rows
        # Rows run member by member over the same lead days and catchments (read_starts sorts them).
        shape = members, -1
        lead = rows['lead_day'][part].reshape(shape)[0] - 1
        place = np.searchsorted(projects, rows['project_id'][part].reshape(shape)[0])
        for column, adjusted in values.items():
            truth = history[column][part].reshape(shape)[0]
            pairs = [('raw', rows[column][part].reshape(shape)) if column in rows else None,
                     ('adjusted', adjusted[part].reshape(shape))]
            ok = np.isfinite(truth) & np.isfinite(pairs[1][1]).all(axis=0)
            total = sums.setdefault(column, {key: np.zeros((leads, len(projects))) for key in CHECK[3:]})
            np.add.at(total['cases'], (lead[ok], place[ok]), 1)
            np.add.at(total['history_mean'], (lead[ok], place[ok]), truth[ok])
            for kind, ensemble in filter(None, pairs):
                error = ensemble.mean(axis=0) - truth
                np.add.at(total[f'{kind}_bias'], (lead[ok], place[ok]), error[ok])
                np.add.at(total[f'{kind}_mae'], (lead[ok], place[ok]), np.abs(error[ok]))
                np.add.at(total[f'{kind}_crps'], (lead[ok], place[ok]), crps(ensemble[:, ok], truth[ok]))
            if column not in rows:
                for key in ('raw_bias', 'raw_mae', 'raw_crps'):
                    total[key][:] = np.nan
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(CHECK)
        for column, total in sums.items():
            for lead in range(leads):
                for project, select in [*((p, i) for i, p in enumerate(projects)), ('ALL', slice(None))]:
                    cases = total['cases'][lead, select].sum()
                    if cases:
                        writer.writerow([column, lead + 1, project, int(cases)] + [
                            f'{total[key][lead, select].sum() / cases:.6g}' for key in CHECK[4:]])


def hindcast(forecast, history_path, geojson, output):
    paths = sorted(forecast.glob(START))
    if not paths:
        raise ValueError(f'No start tables in {forecast}')
    tables, rows = read_starts(paths)
    import pyarrow.parquet as pq
    available = set(pq.read_schema(history_path).names)
    columns = [column for column in COLUMNS if column in rows and column in available and column in ADDITIVE | RATIO]
    history = lookup(history_path, rows['project_id'], rows['valid_date'], sorted(set(columns) | set(OUTPUT[:3])
                                                                                    | {'pet_hargreaves_mm_day'}))
    curves = phase_curves(history_path)
    catchments = Catchments(geojson)
    latitudes = dict(zip(catchments.ids, catchments.latitudes))
    projects = sorted(set(rows['project_id']))
    years = sorted(set(rows['year'].tolist()))
    print(f'{len(paths)} starts of {forecast.name}, {len(rows["year"]):,} rows, years {years}; '
          f'mapping {", ".join(columns)}', flush=True)
    values = None
    for year in years:
        train, target = rows['year'] != year, rows['year'] == year
        mapping = fit({k: v[train] for k, v in rows.items()}, {k: v[train] for k, v in history.items()},
                      columns, projects, curves, latitudes, {})
        part = adjust({k: v[target] for k, v in rows.items()}, mapping)
        if values is None:
            values = {column: np.full(len(target), np.nan) for column in part}
        for column, array in part.items():
            values[column][target] = array
        print(f'{year}: mapped with a fit on the other years', flush=True)
    output.mkdir(parents=True, exist_ok=True)
    write(tables, rows, values, output)
    check(tables, rows, values, history, output / 'check.csv')
    info = dict(system=forecast.name, starts=[paths[0].stem, paths[-1].stem, len(paths)],
                history=history_path.name)
    save(fit(rows, history, columns, projects, curves, latitudes, info), output / 'mapping.parquet')
    print(f'Wrote {len(paths)} adjusted starts, check.csv and mapping.parquet to {output}', flush=True)


def phase_curves(path):
    """{catchment: (centre, width)} of the history table's snow fraction against daily mean temperature."""
    import pyarrow.parquet as pq
    table = pq.read_table(path, columns=['project_id', *PHASE])
    values = {name: table[name].to_numpy(zero_copy_only=False).astype(float) for name in PHASE}
    projects = table['project_id'].to_numpy(zero_copy_only=False)
    errors = phase_errors(dict(precipitation_mm=values['precipitation_mm'], snowfall_mm=values['snowfall_mm'],
                               tmean_c=values['tmean_degC']), projects, np.arange(len(projects)),
                          np.zeros(len(projects), int))
    return {project: tuple(float(value) for value in curve) for project, curve in best_curves(errors).items()}


def apply(mapping_path, paths, output):
    mapping = load(mapping_path)
    tables, rows = read_starts(paths)
    output.mkdir(parents=True, exist_ok=True)
    write(tables, rows, adjust(rows, mapping), output)
    print(f'Wrote {len(paths)} adjusted starts to {output} with {mapping_path}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    fit_parser = commands.add_parser('hindcast', help='Map every hindcast year with a fit on the others')
    fit_parser.add_argument('--forecast', required=True, type=Path, help='Folder of one system\'s start tables')
    fit_parser.add_argument('--history', required=True, type=Path, help='Delivered history table (Parquet)')
    fit_parser.add_argument('--geojson', required=True, type=Path, help='Catchment polygons, for PET latitudes')
    fit_parser.add_argument('--output', required=True, type=Path)
    apply_parser = commands.add_parser('apply', help='Map start tables with a saved mapping')
    apply_parser.add_argument('--mapping', required=True, type=Path)
    apply_parser.add_argument('--forecast', required=True, nargs='+', type=Path, help='Start tables')
    apply_parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'hindcast':
        hindcast(args.forecast, args.history, args.geojson, args.output)
    else:
        apply(args.mapping, args.forecast, args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
