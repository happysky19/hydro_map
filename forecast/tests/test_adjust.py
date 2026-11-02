import csv
from datetime import date, datetime, timedelta, timezone
import json

import numpy as np
import pytest

from catchment_forecast import adjust
from catchment_history.meteorology import hargreaves_pet

pa = pytest.importorskip('pyarrow')
pq = pytest.importorskip('pyarrow.parquet')

PROJECTS = {'ALPHA': 49.5, 'BETA': 46.2, 'GAMMA': 55.8}
LEADS = 6


def history_table(path):
    """Daily values 2022-12 to 2026-03: a seasonal temperature, showers, snow below 0 degC."""
    rng = np.random.default_rng(1)
    days = np.arange(np.datetime64('2022-12-01'), np.datetime64('2026-03-01'))
    rows = {name: [] for name in ('date', 'project_id', 'precipitation_mm', 'rainfall_mm', 'snowfall_mm',
                                  'tmean_degC', 'tmax_degC', 'tmin_degC', 'wind_speed_m_s', 'u_wind_m_s',
                                  'v_wind_m_s', 'pet_hargreaves_mm_day')}
    for project, latitude in PROJECTS.items():
        season = -np.cos(2 * np.pi * (days - np.datetime64('2023-01-15')).astype(int) / 365.25)
        tmean = 5 + 12 * season + rng.normal(0, 3, len(days))
        precipitation = np.where(rng.random(len(days)) < .5, rng.gamma(.8, 6, len(days)), 0.)
        snow = precipitation / (1 + np.exp(tmean / 1.5))
        u, v = rng.normal(1, 2, len(days)), rng.normal(0, 2, len(days))
        tmax, tmin = tmean + 5, tmean - 4
        rows['date'] += list(days.astype(object))
        rows['project_id'] += [project] * len(days)
        for name, values in [('precipitation_mm', precipitation), ('rainfall_mm', precipitation - snow),
                             ('snowfall_mm', snow), ('tmean_degC', tmean), ('tmax_degC', tmax),
                             ('tmin_degC', tmin), ('wind_speed_m_s', np.hypot(u, v) + .5), ('u_wind_m_s', u),
                             ('v_wind_m_s', v)]:
            rows[name] += list(values)
        rows['pet_hargreaves_mm_day'] += [hargreaves_pet(a, c, b, latitude, d) for a, b, c, d in
                                          zip(tmean, tmax, tmin, days.astype(object))]
    table = pa.table(rows)
    pq.write_table(table, path)
    return {(d, p): i for i, (d, p) in enumerate(zip(rows['date'], rows['project_id']))}, rows


def write_starts(folder, index, history, members=8, first=date(2023, 1, 1), last=date(2025, 12, 29)):
    """Start tables every 4 days: the history plus a warm, wet and windy bias that grows with lead."""
    rng = np.random.default_rng(2)
    folder.mkdir(parents=True, exist_ok=True)
    start = first
    while start <= last:
        rows = {name: [] for name in adjust.KEYS + ['precipitation_mm', 'tmean_degC', 'tmax_degC', 'tmin_degC',
                                                    'u_wind_m_s', 'v_wind_m_s', 'wind_speed_m_s',
                                                    'pet_hargreaves_mm_day']}
        init = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        for member in range(members):
            for lead in range(1, LEADS + 1):
                valid = start + timedelta(days=lead - 1)
                for project in PROJECTS:
                    i = index[valid, project]
                    noise = rng.normal(0, .4 * lead)
                    tmean = history['tmean_degC'][i] + 2 + noise
                    rows['init_time'].append(init)
                    rows['member'].append(f'm{member:02d}')
                    rows['lead_day'].append(lead)
                    rows['valid_date'].append(valid)
                    rows['project_id'].append(project)
                    rows['precipitation_mm'].append(1.5 * history['precipitation_mm'][i] * rng.lognormal(0, .2))
                    rows['tmean_degC'].append(tmean)
                    rows['tmax_degC'].append(tmean + 2)
                    rows['tmin_degC'].append(tmean - 2)
                    rows['u_wind_m_s'].append(history['u_wind_m_s'][i] + .3)
                    rows['v_wind_m_s'].append(history['v_wind_m_s'][i])
                    rows['wind_speed_m_s'].append(1.3 * history['wind_speed_m_s'][i])
                    rows['pet_hargreaves_mm_day'].append(1.)
        table = pa.table({**rows, 'init_time': pa.array(rows['init_time'], pa.timestamp('s', tz='UTC')),
                          'lead_day': pa.array(rows['lead_day'], pa.int16()),
                          'valid_date': pa.array(rows['valid_date'], pa.date32())})
        pq.write_table(table, folder / f'{start:%Y%m%d}00.parquet')
        start += timedelta(days=4) if start.day < 29 and not (start.month == 2 and start.day == 25) else \
            timedelta(days=(date(start.year + start.month // 12, start.month % 12 + 1, 1) - start).days)


@pytest.fixture(scope='module')
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp('adjust')
    index, history = history_table(root / 'history.parquet')
    write_starts(root / 'raw' / 'wn2', index, history)
    geojson = root / 'catchments.geojson'
    geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[
        dict(type='Feature', properties=dict(id=project, part='local'), geometry=dict(
            type='Polygon', coordinates=[[[-120, lat - .2], [-119.6, lat - .2], [-119.6, lat + .2],
                                          [-120, lat + .2], [-120, lat - .2]]]))
        for project, lat in PROJECTS.items()])))
    adjust.main(['hindcast', '--forecast', str(root / 'raw' / 'wn2'), '--history', str(root / 'history.parquet'),
                 '--geojson', str(geojson), '--output', str(root / 'adjusted' / 'wn2')])
    return root, index, history


def check_rows(root):
    with (root / 'adjusted' / 'wn2' / 'check.csv').open() as stream:
        return [row for row in csv.DictReader(stream) if row['project_id'] == 'ALL']


def test_held_out_years_lose_the_bias(run):
    root, _, _ = run
    rows = check_rows(root)
    for row in rows:
        if row['column'] == 'tmean_degC':
            assert abs(float(row['raw_bias']) - 2) < .3
            assert abs(float(row['adjusted_bias'])) < .3
            assert float(row['adjusted_crps']) < float(row['raw_crps'])
        if row['column'] in ('precipitation_mm', 'wind_speed_m_s'):
            assert abs(float(row['adjusted_bias'])) < .1 * float(row['history_mean'])
            assert abs(float(row['raw_bias'])) > .25 * float(row['history_mean'])
    assert {row['column'] for row in rows} == {'precipitation_mm', 'rainfall_mm', 'snowfall_mm', 'tmean_degC',
                                               'tmax_degC', 'tmin_degC', 'u_wind_m_s', 'v_wind_m_s',
                                               'wind_speed_m_s', 'pet_hargreaves_mm_day'}
    assert {int(row['lead_day']) for row in rows} == set(range(1, LEADS + 1))


def test_adjusted_tables_are_consistent(run):
    root, _, _ = run
    paths = sorted((root / 'adjusted' / 'wn2').glob(adjust.START))
    assert len(paths) == len(list((root / 'raw' / 'wn2').glob(adjust.START)))
    table = pq.read_table(paths[100]).to_pydict()
    assert list(table)[:5] == adjust.KEYS
    p, rain, snow = (np.array(table[name]) for name in ('precipitation_mm', 'rainfall_mm', 'snowfall_mm'))
    np.testing.assert_allclose(rain + snow, p)
    assert (snow >= 0).all() and (rain >= 0).all()
    t, high, low = (np.array(table[name]) for name in ('tmean_degC', 'tmax_degC', 'tmin_degC'))
    assert (low <= t).all() and (t <= high).all()
    i = 7
    expected = hargreaves_pet(t[i], low[i], high[i], PROJECTS[table['project_id'][i]], table['valid_date'][i])
    assert table['pet_hargreaves_mm_day'][i] == pytest.approx(expected, rel=1e-9)


def test_operations_use_the_same_mapping_and_code(run, tmp_path):
    root, index, history = run
    mapping = adjust.load(root / 'adjusted' / 'wn2' / 'mapping.parquet')
    paths = sorted((root / 'raw' / 'wn2').glob(adjust.START))
    tables, rows = adjust.read_starts(paths)
    in_memory = adjust.fit(rows, adjust.lookup(root / 'history.parquet', rows['project_id'], rows['valid_date'],
                                               list(mapping['quantiles'])),
                           list(mapping['quantiles']), mapping['projects'], mapping['curves'],
                           mapping['latitudes'], {})
    for column, (xq, yq) in in_memory['quantiles'].items():
        np.testing.assert_array_equal(mapping['quantiles'][column][0], xq)
        np.testing.assert_array_equal(mapping['quantiles'][column][1], yq)
    # An operational start with a different member count goes through apply unchanged.
    write_starts(tmp_path / 'op', index, history, members=13, first=date(2025, 12, 29), last=date(2025, 12, 29))
    adjust.main(['apply', '--mapping', str(root / 'adjusted' / 'wn2' / 'mapping.parquet'), '--forecast',
                 str(tmp_path / 'op' / '2025122900.parquet'), '--output', str(tmp_path / 'out')])
    table = pq.read_table(tmp_path / 'out' / '2025122900.parquet')
    assert len(set(table['member'].to_pylist())) == 13
    _, op_rows = adjust.read_starts([tmp_path / 'op' / '2025122900.parquet'])
    expected = adjust.adjust(op_rows, in_memory)
    for column, values in expected.items():
        np.testing.assert_array_equal(table[column].to_numpy(), values)


def test_a_group_without_a_fit_stops_the_run():
    rows = dict(project_id=np.array(['ALPHA']), lead_day=np.array([1]),
                valid_date=np.array(['2024-01-10'], dtype='datetime64[D]'), tmean_degC=np.array([1.]))
    quantiles = np.full((len(adjust.LEAD_GROUPS) * 12, len(adjust.LEVELS)), np.nan)
    with pytest.raises(ValueError, match='No fit for ALPHA, lead days from 1, month 1'):
        adjust.adjust(rows, dict(projects=['ALPHA'], quantiles={'tmean_degC': (quantiles, quantiles)},
                                 curves={'ALPHA': (0., 1.)}, latitudes={'ALPHA': 50.}))
