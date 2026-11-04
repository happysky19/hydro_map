"""Complete adjusted forecast starts into every column of the history table, member by member.

    python -m catchment_forecast.complete fit --history DELIVERY.parquet --before 2023 --output DIR/completion.json
    python -m catchment_forecast.complete run --fit DIR/completion.json --history DELIVERY.parquet \\
        --forecast DIR/aifs_adjusted DIR/wn2_adjusted --gap DIR/gefs_day1_adjusted --output DIR/product \\
        [--start 2024-01-01 --end 2024-01-31]

A member brings its own weather (WEATHER): precipitation, rain and snow, the
temperatures, wind and PET, plus humidity and shortwave where its system
forecasts them. The other columns are made from that weather as a hydrological
model would make them, from the last state the history table knows, on the
anchor day ANCHOR days before the start (the history runs that late in
operations):

- Snow water equivalent and snowmelt (SNOW): a degree-day model started from
  the history SWE on the anchor day and driven by the member's snowfall and
  mean temperature; melt factor and threshold are fitted per catchment on the
  history over runs as long as a forecast.
- Humidity, shortwave, net radiation, cloud, actual evapotranspiration, the
  freezing levels, the top-layer soil temperature and snow depth (DERIVED): a
  linear regression per catchment and calendar month, with the months either
  side, on the member's weather and SWE (PREDICTORS), fitted on the history.
  A system uses the first predictor set it has every predictor of.
- Soil moisture and the deeper soil temperatures (PERSISTED): the history
  value on the anchor day plus the change of the day-of-year climatology.

The GAP days between the anchor and the start come from the gap tables: one
adjusted start table per day whose first lead day and first member give that
day's weather, completed the same way; every member shares them. Each member
is then one series from the day after the anchor (lead day 1 - GAP) to its
last lead day. Everything is fitted on history years before --before, so the
hindcasts never see their own years, and the same file serves operations.
run writes one table per start, with a system column, and check.csv against
the history where it has the dates.
"""

import argparse
import csv
import json
from pathlib import Path
import sys

import numpy as np

from .adjust import OUTPUT, START, crps


ANCHOR = 6
GAP = ANCHOR - 1
WEATHER = ['precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'tmean_degC', 'tmax_degC', 'tmin_degC',
           'relative_humidity_pct', 'vapor_pressure_deficit_kPa', 'shortwave_down_mean_W_m2', 'u_wind_m_s',
           'v_wind_m_s', 'wind_speed_m_s', 'pet_hargreaves_mm_day']
SNOW = ['snow_water_equivalent_mm', 'snowmelt_mm']
DERIVED = ['relative_humidity_pct', 'vapor_pressure_deficit_kPa', 'shortwave_down_mean_W_m2',
           'net_radiation_mean_W_m2', 'cloud_cover_fraction', 'actual_evapotranspiration_mm',
           'freezing_level_above_ground_m', 'freezing_level_above_sea_level_m', 'soil_temperature_layer1_degC',
           'snow_depth_m']
PERSISTED = ['soil_moisture_layer1_m3_m3', 'root_zone_soil_moisture_0_100cm_m3_m3', 'soil_moisture_layer4_m3_m3',
             'soil_temperature_layer2_degC', 'soil_temperature_layer3_degC', 'soil_temperature_layer4_degC']
PREDICTORS = (('tmean_degC', 'tmax_degC', 'tmin_degC', 'precipitation_mm', 'relative_humidity_pct',
               'shortwave_down_mean_W_m2', 'snow_water_equivalent_mm'),
              ('tmean_degC', 'tmax_degC', 'tmin_degC', 'precipitation_mm', 'snow_water_equivalent_mm'))
BOUNDS = {'relative_humidity_pct': (0., 100.), 'cloud_cover_fraction': (0., 1.),
          **{column: (0., None) for column in ('vapor_pressure_deficit_kPa', 'shortwave_down_mean_W_m2',
                                                'actual_evapotranspiration_mm', 'freezing_level_above_ground_m',
                                                'snow_depth_m', *PERSISTED[:3])}}
MELT_FACTORS = np.arange(1, 8.01, .5)
THRESHOLDS = np.arange(-2, 2.01, .5)
WINDOW = GAP + 22
SMOOTH = 15
MIN_FIT = 100
KEYS = ['init_time', 'system', 'member', 'lead_day', 'valid_date', 'project_id']
CHECK = ['system', 'column', 'lead_day', 'cases', 'history_mean', 'bias', 'mae', 'crps']


def load_history(path, columns):
    """{'projects', 'first', 'grid': {column: projects x days}} of the history table; NaN where it has no row."""
    import pyarrow.parquet as pq
    table = pq.read_table(path, columns=['date', 'project_id', *columns])
    days = table['date'].to_numpy(zero_copy_only=False).astype('datetime64[D]')
    projects, codes = np.unique(table['project_id'].to_numpy(zero_copy_only=False), return_inverse=True)
    first = days.min()
    index = (days - first).astype(int)
    grid = {}
    for column in columns:
        grid[column] = np.full((len(projects), index.max() + 1), np.nan)
        grid[column][codes, index] = table[column].to_numpy(zero_copy_only=False).astype(float)
    return dict(projects=projects.tolist(), first=first, grid=grid)


def at(history, column, dates):
    """History values (dates x projects) on these dates; NaN outside the table."""
    index = (np.asarray(dates, 'datetime64[D]') - history['first']).astype(int)
    grid = history['grid'][column]
    inside = (index >= 0) & (index < grid.shape[1])
    out = np.full((len(index), grid.shape[0]), np.nan)
    out[inside] = grid[:, index[inside]].T
    return out


def design(values, predictors):
    """Regression terms (..., 1 + len(predictors)): one, then each predictor; precipitation as log(1 + P)."""
    terms = [np.ones_like(values[predictors[0]])]
    for name in predictors:
        x = values[name]
        terms.append(np.log1p(np.maximum(x, 0.)) if name == 'precipitation_mm' else x)
    return np.stack(terms, axis=-1)


def degree_day(swe0, snowfall, tmean, factor, threshold):
    """(SWE after each day, melt of each day), both (days, ...), from swe0 (...) and the daily snowfall and
    mean temperature (days, ...): melt = factor (mm/degC/day) times the degrees above threshold, up to the snow."""
    swe, swes, melts = swe0, [], []
    for snow, t in zip(snowfall, tmean):
        swe = swe + snow
        melt = np.minimum(swe, factor * np.maximum(t - threshold, 0.))
        swe = swe - melt
        swes.append(swe)
        melts.append(melt)
    return np.array(swes), np.array(melts)


def fit_regression(history, use, month):
    """{column: [coefficients (projects x 12 x terms) per predictor set, None where the column is a predictor]},
    fitted on the history days flagged in use, per catchment and month with the months either side."""
    grid = history['grid']
    out = {}
    for column in DERIVED:
        sets = []
        for predictors in PREDICTORS:
            if column in predictors:
                sets.append(None)
                continue
            x, y = design(grid, predictors), grid[column]
            ok = use & np.isfinite(x).all(axis=-1) & np.isfinite(y)
            coefs = np.full((len(y), 12, x.shape[-1]), np.nan)
            for m in range(12):
                near = np.isin(month, (m + np.arange(-1, 2)) % 12)
                for p in range(len(coefs)):
                    rows = ok[p] & near
                    if rows.sum() >= MIN_FIT:
                        coefs[p, m] = np.linalg.lstsq(x[p, rows], y[p, rows], rcond=None)[0]
            sets.append(coefs)
        out[column] = sets
    return out


def fit_snow(history, use):
    """(melt factor, threshold, RMSE) per catchment: the grid point whose WINDOW-day runs from the history SWE
    on every other anchor day in use, driven by the history snowfall and tmean, stay closest to the history SWE."""
    grid = history['grid']
    swe, snow, t = (grid[name] for name in ('snow_water_equivalent_mm', 'snowfall_mm', 'tmean_degC'))
    anchors = np.flatnonzero(use)[:-WINDOW:2]
    windows = anchors[:, None] + np.arange(1, WINDOW + 1)
    swe0 = swe[:, anchors].T
    snowfall, tmean, target = (values[:, windows].transpose(2, 1, 0) for values in (snow, t, swe))
    count = len(swe)
    best, factor, threshold = np.full(count, np.inf), np.full(count, 3.), np.zeros(count)
    for k in MELT_FACTORS:
        for t0 in THRESHOLDS:
            error = degree_day(swe0, snowfall, tmean, k, t0)[0] - target
            known = np.isfinite(error)
            with np.errstate(invalid='ignore', divide='ignore'):
                rmse = np.sqrt((np.where(known, error, 0.) ** 2).sum(axis=(0, 1)) / known.sum(axis=(0, 1)))
            better = rmse < best
            best[better], factor[better], threshold[better] = rmse[better], k, t0
    return factor, threshold, best


def climatology(values, doy):
    """Mean of values (projects x days) per day of year, smoothed over SMOOTH days either side around the year."""
    known = np.isfinite(values)
    total = np.stack([np.bincount(doy[ok], row[ok], minlength=366) for row, ok in zip(values, known)])
    count = np.stack([np.bincount(doy[ok], minlength=366) for ok in known]).astype(float)
    around = lambda a: np.concatenate([a[:, -SMOOTH:], a, a[:, :SMOOTH]], axis=1)
    smooth = lambda a: np.apply_along_axis(lambda row: np.convolve(row, np.ones(2 * SMOOTH + 1), 'valid'), 1, around(a))
    with np.errstate(invalid='ignore', divide='ignore'):
        return smooth(total) / smooth(count)


def day_of_year(dates):
    dates = np.asarray(dates, 'datetime64[D]')
    return (dates - dates.astype('datetime64[Y]').astype('datetime64[D]')).astype(int)


def fit(history_path, before, output):
    history = load_history(history_path, sorted({*sum(PREDICTORS, ()), *DERIVED, *PERSISTED, *SNOW, 'snowfall_mm'}))
    days = history['first'] + np.arange(history['grid']['tmean_degC'].shape[1])
    use = days < np.datetime64(f'{before}-01-01')
    if use.sum() < 365 * 5:
        raise ValueError(f'{history_path} has {use.sum()} days before {before}; at least five years are needed')
    doy = day_of_year(days)
    print(f'Fitting on {use.sum():,} history days before {before}, {len(history["projects"])} catchments', flush=True)
    regression = fit_regression(history, use, days.astype('datetime64[M]').astype(int) % 12)
    factor, threshold, rmse = fit_snow(history, use)
    for project, k, t0, error in zip(history['projects'], factor, threshold, rmse):
        print(f'{project}: melt {k:.1f} mm/degC/day above {t0:+.1f} degC, SWE RMSE {error:.1f} mm', flush=True)
    result = dict(
        info=dict(history=history_path.name, before=before, anchor=ANCHOR, window=WINDOW, predictors=PREDICTORS),
        projects=history['projects'],
        regression={column: [None if coefs is None else coefs.tolist() for coefs in sets]
                    for column, sets in regression.items()},
        snow=dict(melt_factor=factor.tolist(), threshold=threshold.tolist()),
        climatology={column: climatology(history['grid'][column][:, use], doy[use]).tolist() for column in PERSISTED})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result))
    print(f'Wrote {output}', flush=True)


def load_fit(path):
    result = json.loads(path.read_text())
    info = result['info']
    if info['anchor'] != ANCHOR or [tuple(p) for p in info['predictors']] != list(PREDICTORS):
        raise ValueError(f'{path} was fitted with another anchor or other predictors')
    result['regression'] = {column: [None if coefs is None else np.array(coefs) for coefs in sets]
                            for column, sets in result['regression'].items()}
    result['snow'] = {name: np.array(values) for name, values in result['snow'].items()}
    result['climatology'] = {column: np.array(values) for column, values in result['climatology'].items()}
    return result


def read_start(path):
    """Rows of an adjusted start table sorted by member, lead day and catchment, as arrays; init_time a scalar."""
    import pyarrow.parquet as pq
    table = pq.read_table(path).sort_by([('member', 'ascending'), ('lead_day', 'ascending'),
                                         ('project_id', 'ascending')])
    rows = {name: table[name].to_numpy(zero_copy_only=False) for name in table.column_names}
    rows['valid_date'] = rows['valid_date'].astype('datetime64[D]')
    rows['init_time'] = table['init_time'][0]
    return rows


def gap_weather(folder, start, projects):
    """{weather column: GAP x projects} of the GAP days before start, from the first member's first lead day of
    each day's table in folder."""
    out = {}
    for i, day in enumerate(start - GAP + np.arange(GAP)):
        path = folder / f'{str(day).replace("-", "")}00.parquet'
        if not path.exists():
            raise FileNotFoundError(f'No gap table {path.name} for the start {start}')
        rows = read_start(path)
        pick = (rows['member'] == rows['member'][0]) & (rows['lead_day'] == 1)
        if not np.array_equal(rows['project_id'][pick], projects):
            raise ValueError(f'{path.name} has other catchments than the fit')
        for column in WEATHER:
            if column in rows:
                out.setdefault(column, np.full((GAP, len(projects)), np.nan))[i] = rows[column][pick]
    return out


def complete(system, rows, gap, fitted, history):
    """(members, days, {column: members x days x projects}) of one start's rows completed into every column of
    OUTPUT, the GAP days first."""
    projects = fitted['projects']
    members = np.unique(rows['member'])
    leads = int(rows['lead_day'].max())
    shape = (len(members), leads, len(projects))
    if len(rows['project_id']) != np.prod(shape) or not np.array_equal(rows['project_id'][:len(projects)], projects):
        raise ValueError(f'{system}: the rows are not every member, lead day and catchment of the fit')
    own = [column for column in WEATHER if column in rows]
    missing = (set(WEATHER) - set(DERIVED) - set(own)) | (set(own) - set(gap))
    if missing:
        raise ValueError(f'{system}: no {", ".join(sorted(missing))} in the start or the gap tables')
    start = np.datetime64(rows['init_time'].as_py().strftime('%Y-%m-%d'), 'D')
    days = start - GAP + np.arange(GAP + leads)
    anchor = start - ANCHOR
    values = {column: np.concatenate([np.broadcast_to(gap[column], (len(members), GAP, len(projects))),
                                      rows[column].reshape(shape)], axis=1) for column in own}
    swe0 = at(history, 'snow_water_equivalent_mm', [anchor])[0]
    if not np.isfinite(swe0).all():
        raise ValueError(f'{system}: the history has no snow water equivalent on {anchor}')
    swe, melt = degree_day(np.broadcast_to(swe0, (len(members), len(projects))),
                           values['snowfall_mm'].transpose(1, 0, 2), values['tmean_degC'].transpose(1, 0, 2),
                           fitted['snow']['melt_factor'], fitted['snow']['threshold'])
    values['snow_water_equivalent_mm'], values['snowmelt_mm'] = swe.transpose(1, 0, 2), melt.transpose(1, 0, 2)
    doy, anchor_doy = day_of_year(days), day_of_year([anchor])[0]
    for column in PERSISTED:
        clim = fitted['climatology'][column]
        series = at(history, column, [anchor]) + clim[:, doy].T - clim[:, anchor_doy]
        values[column] = np.broadcast_to(series, (len(members), GAP + leads, len(projects)))
    available = set(own) | set(SNOW)
    month = days.astype('datetime64[M]').astype(int) % 12
    for column in DERIVED:
        if column in own:
            continue
        choice = next((i for i, p in enumerate(PREDICTORS) if set(p) <= available and column not in p), None)
        if choice is None:
            raise ValueError(f'{system}: no predictors for {column}')
        coefs = fitted['regression'][column][choice][:, month].transpose(1, 0, 2)
        if np.isnan(coefs).any():
            raise ValueError(f'No fit for {column} in some catchment and month of {start}')
        values[column] = np.einsum('mdpk,dpk->mdp', design(values, PREDICTORS[choice]), coefs)
    for column, (low, high) in BOUNDS.items():
        values[column] = np.clip(values[column], low, high)
    values['snow_depth_m'] = np.where(values['snow_water_equivalent_mm'] > 0, values['snow_depth_m'], 0.)
    return members, days, {column: values[column] for column in OUTPUT}


def tally(sums, system, days, values, history):
    """Add one start's members to the sums behind check.csv, on the valid dates the history has."""
    lead = np.arange(len(days)) - GAP + 1
    for column, members in values.items():
        if column not in history['grid']:
            continue
        truth = at(history, column, days)
        ok = np.isfinite(truth) & np.isfinite(members).all(axis=0)
        if not ok.any():
            continue
        total = sums.setdefault((system, column), {key: {} for key in CHECK[3:]})
        error = members.mean(axis=0) - truth
        score = crps(members.reshape(len(members), -1), truth.ravel()).reshape(truth.shape)
        for i in np.flatnonzero(ok.any(axis=1)):
            for key, amount in (('cases', ok[i].sum()), ('history_mean', truth[i][ok[i]].sum()),
                                ('bias', error[i][ok[i]].sum()), ('mae', np.abs(error[i][ok[i]]).sum()),
                                ('crps', score[i][ok[i]].sum())):
                total[key][int(lead[i])] = total[key].get(int(lead[i]), 0.) + amount


def write_check(sums, path):
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(CHECK)
        for (system, column), total in sums.items():
            for day, cases in sorted(total['cases'].items()):
                writer.writerow([system, column, day, int(cases)]
                                + [f'{total[key][day] / cases:.6g}' for key in CHECK[4:]])


def write_start(parts, projects, path):
    """One product table: the parts (system, init_time, members, days, values) of every system, in KEYS order."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    tables = []
    for system, init_time, members, days, values in parts:
        count = len(members) * len(days) * len(projects)
        table = {'init_time': pa.array([init_time.as_py()] * count, pa.timestamp('s', tz='UTC')),
                 'system': pa.array([system] * count),
                 'member': pa.array(np.repeat(members, len(days) * len(projects))),
                 'lead_day': pa.array(np.tile(np.repeat(np.arange(len(days)) - GAP + 1, len(projects)), len(members)),
                                      pa.int16()),
                 'valid_date': pa.array(np.tile(np.repeat(days, len(projects)), len(members)).astype(object),
                                        pa.date32()),
                 'project_id': pa.array(np.tile(np.array(projects, dtype=object), len(members) * len(days)))}
        for column in OUTPUT:
            table[column] = pa.array(values[column].reshape(-1))
        tables.append(pa.table(table))
    pq.write_table(pa.concat_tables(tables), path.with_suffix('.part'), compression='zstd')
    path.with_suffix('.part').replace(path)


def run(fit_path, history_path, forecasts, gap, output, first=None, last=None):
    import pyarrow.parquet as pq
    fitted = load_fit(fit_path)
    available = set(pq.read_schema(history_path).names)
    missing = {'snow_water_equivalent_mm', *PERSISTED} - available
    if missing:
        raise ValueError(f'{history_path} lacks {", ".join(sorted(missing))}')
    history = load_history(history_path, sorted(set(OUTPUT) & available))
    if history['projects'] != fitted['projects']:
        raise ValueError(f'{history_path} and {fit_path} have different catchments')
    systems = {folder.name.removesuffix('_adjusted'): folder for folder in forecasts}
    names = sorted({path.name for folder in forecasts for path in folder.glob(START)})
    paths = [Path(name) for name in names
             if (first is None or name[:8] >= f'{first:%Y%m%d}') and (last is None or name[:8] <= f'{last:%Y%m%d}')]
    if not paths:
        raise ValueError(f'No start tables in {", ".join(str(f) for f in forecasts)} between {first} and {last}')
    output.mkdir(parents=True, exist_ok=True)
    sums, written = {}, 0
    for path in paths:
        target = output / path.name
        if target.exists():
            continue
        start = np.datetime64(f'{path.stem[:4]}-{path.stem[4:6]}-{path.stem[6:8]}', 'D')
        weather = gap_weather(gap, start, fitted['projects'])
        parts = []
        for system, folder in systems.items():
            if not (folder / path.name).exists():
                print(f'{path.stem}: no {system} table', flush=True)
                continue
            rows = read_start(folder / path.name)
            members, days, values = complete(system, rows, weather, fitted, history)
            tally(sums, system, days, values, history)
            parts.append((system, rows['init_time'], members, days, values))
        if parts:
            write_start(parts, fitted['projects'], target)
            written += 1
            print(f'{path.stem}: {", ".join(f"{len(p[2])} {p[0]}" for p in parts)} members, {len(parts[0][3])} days',
                  flush=True)
    if sums:
        write_check(sums, output / 'check.csv')
    print(f'Wrote {written} product tables to {output}', flush=True)


def main(argv=None):
    from datetime import date
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    fit_parser = commands.add_parser('fit', help='Fit the snow model, regressions and climatology on the history')
    fit_parser.add_argument('--history', required=True, type=Path, help='Delivered history table (Parquet)')
    fit_parser.add_argument('--before', required=True, type=int, help='Fit on years before this one')
    fit_parser.add_argument('--output', required=True, type=Path, help='The completion file (JSON)')
    run_parser = commands.add_parser('run', help='Complete adjusted starts into product tables')
    run_parser.add_argument('--fit', required=True, type=Path, help='The completion file of fit')
    run_parser.add_argument('--history', required=True, type=Path)
    run_parser.add_argument('--forecast', required=True, nargs='+', type=Path,
                            help='Folders of adjusted start tables, one per system (SYSTEM_adjusted)')
    run_parser.add_argument('--gap', required=True, type=Path, help='Folder of adjusted day-1 tables, every day')
    run_parser.add_argument('--output', required=True, type=Path)
    run_parser.add_argument('--start', type=date.fromisoformat, help='First start date')
    run_parser.add_argument('--end', type=date.fromisoformat, help='Last start date')
    args = parser.parse_args(argv)
    if args.command == 'fit':
        fit(args.history, args.before, args.output)
    else:
        run(args.fit, args.history, args.forecast, args.gap, args.output, args.start, args.end)
    return 0


if __name__ == '__main__':
    sys.exit(main())
