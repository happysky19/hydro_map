import csv
from datetime import date, datetime, timedelta, timezone
import json

import numpy as np
import pytest

from catchment_forecast import adjust, complete
from catchment_history.meteorology import hargreaves_pet, saturation_vapor_pressure_kpa

pa = pytest.importorskip('pyarrow')
pq = pytest.importorskip('pyarrow.parquet')

PROJECTS = {'ALPHA': 49.5, 'BETA': 46.2, 'GAMMA': 55.8}
MELT = {'ALPHA': 2.5, 'BETA': 3., 'GAMMA': 4.}
THRESHOLD = {'ALPHA': 0., 'BETA': .5, 'GAMMA': -.5}
AIFS = complete.WEATHER
WN2 = [column for column in complete.WEATHER if column not in complete.DERIVED]
LEADS = 8


def smooth(values, width):
    return np.convolve(np.pad(values, width, mode='edge'), np.ones(2 * width + 1) / (2 * width + 1), 'valid')


def history_table(path, first='2008-01-01', last='2025-12-31'):
    """Daily values with known relations: a degree-day snowpack, freezing levels 160 m per degree, humidity and
    cloud from the diurnal range and rain, slow soil, and the rest as in the adjust tests."""
    rng = np.random.default_rng(3)
    days = np.arange(np.datetime64(first), np.datetime64(last) + 1)
    n = len(days)
    season = -np.cos(2 * np.pi * (days - np.datetime64('2008-01-15')).astype(int) / 365.25)
    frames = []
    for project, latitude in PROJECTS.items():
        tmean = 3 + 12 * season + rng.normal(0, 3, n) + (latitude - 50) * -.3
        tmax, tmin = tmean + 4 + abs(rng.normal(0, 2, n)), tmean - 3 - abs(rng.normal(0, 2, n))
        precipitation = np.where(rng.random(n) < .45, rng.gamma(.8, 7, n), 0.)
        snowfall = precipitation / (1 + np.exp(tmean / 1.5))
        swe, melt = complete.degree_day(np.array(0.), snowfall, tmean, MELT[project], THRESHOLD[project])
        rh = np.clip(75 - 2.5 * (tmax - tmin) + 8 * (precipitation > 0) + rng.normal(0, 5, n), 5, 100)
        cloud = np.clip(.35 + .04 * precipitation - .025 * (tmax - tmin) + rng.normal(0, .1, n), 0, 1)
        shortwave = np.maximum(170 + 110 * season - 90 * cloud + rng.normal(0, 10, n), 0)
        u, v = rng.normal(1, 2, n), rng.normal(0, 2, n)
        columns = {
            'precipitation_mm': precipitation, 'rainfall_mm': precipitation - snowfall, 'snowfall_mm': snowfall,
            'tmean_degC': tmean, 'tmax_degC': tmax, 'tmin_degC': tmin, 'relative_humidity_pct': rh,
            'vapor_pressure_deficit_kPa': saturation_vapor_pressure_kpa(tmean) * (1 - rh / 100),
            'shortwave_down_mean_W_m2': shortwave, 'u_wind_m_s': u, 'v_wind_m_s': v,
            'wind_speed_m_s': np.hypot(u, v) + .5,
            'pet_hargreaves_mm_day': np.array([hargreaves_pet(a, c, b, latitude, d) for a, b, c, d in
                                               zip(tmean, tmax, tmin, days.astype(object))]),
            'net_radiation_mean_W_m2': .7 * shortwave - 45 + rng.normal(0, 8, n),
            'cloud_cover_fraction': cloud,
            'actual_evapotranspiration_mm': np.maximum(.012 * shortwave + .08 * np.maximum(tmean, 0)
                                                       + rng.normal(0, .2, n), 0),
            'snow_water_equivalent_mm': swe, 'snow_depth_m': swe / 250, 'snowmelt_mm': melt,
            'freezing_level_above_ground_m': np.maximum(160 * tmean + 400 + rng.normal(0, 100, n), 0),
            'freezing_level_above_sea_level_m': 1200 + 160 * tmean + rng.normal(0, 100, n),
            'soil_moisture_layer1_m3_m3': np.clip(.25 - .1 * season + smooth(rng.normal(0, .05, n), 10), .05, .5),
            'root_zone_soil_moisture_0_100cm_m3_m3': .3 - .08 * season + smooth(rng.normal(0, .03, n), 30),
            'soil_moisture_layer4_m3_m3': .3 - .05 * season,
            'soil_temperature_layer1_degC': smooth(tmean, 3) + 1,
            'soil_temperature_layer2_degC': smooth(tmean, 15),
            'soil_temperature_layer3_degC': smooth(tmean, 45),
            'soil_temperature_layer4_degC': smooth(tmean, 90) + 2,
        }
        frames.append(pa.table({'date': pa.array(days.astype(object), pa.date32()), 'project_id': [project] * n,
                                **{name: values.astype(float) for name, values in columns.items()}}))
    table = pa.concat_tables(frames)
    pq.write_table(table, path)
    return table


def lookup(table):
    rows = table.to_pydict()
    return {(d, p): {name: rows[name][i] for name in table.column_names[2:]}
            for i, (d, p) in enumerate(zip(rows['date'], rows['project_id']))}


def write_adjusted(folder, columns, members, leads, starts, index, noise=.05, seed=4):
    """Adjusted start tables: the history on the valid dates with a little noise per member."""
    rng = np.random.default_rng(seed)
    folder.mkdir(parents=True, exist_ok=True)
    for start in starts:
        rows = {name: [] for name in adjust.KEYS + columns}
        init = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        for member in members:
            for lead in range(1, leads + 1):
                valid = start + timedelta(days=lead - 1)
                for project in PROJECTS:
                    truth = index[valid, project]
                    rows['init_time'].append(init)
                    rows['member'].append(member)
                    rows['lead_day'].append(lead)
                    rows['valid_date'].append(valid)
                    rows['project_id'].append(project)
                    for name in columns:
                        rows[name].append(truth[name] * (1 + rng.normal(0, noise)))
        table = pa.table({**rows, 'init_time': pa.array(rows['init_time'], pa.timestamp('s', tz='UTC')),
                          'lead_day': pa.array(rows['lead_day'], pa.int16()),
                          'valid_date': pa.array(rows['valid_date'], pa.date32())})
        pq.write_table(table, folder / f'{start:%Y%m%d}00.parquet')


def every(first, last, step=1):
    days = []
    while first <= last:
        days.append(first)
        first += timedelta(days=step)
    return days


@pytest.fixture(scope='module')
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp('complete')
    index = lookup(history_table(root / 'history.parquet'))
    starts = every(date(2024, 3, 1), date(2024, 5, 29), 4)
    write_adjusted(root / 'aifs_adjusted', AIFS, ['p00', 'p01', 'p02'], LEADS, starts, index)
    write_adjusted(root / 'wn2_adjusted', WN2, ['ckpt1', 'ckpt2'], LEADS, starts, index, seed=5)
    write_adjusted(root / 'gefs_day1_adjusted', AIFS, ['c00', 'p01'], 1,
                   every(date(2024, 2, 20), date(2024, 6, 5)), index, seed=6)
    complete.main(['fit', '--history', str(root / 'history.parquet'), '--before', '2023',
                   '--output', str(root / 'completion.json')])
    complete.main(['run', '--fit', str(root / 'completion.json'), '--history', str(root / 'history.parquet'),
                   '--forecast', str(root / 'aifs_adjusted'), str(root / 'wn2_adjusted'),
                   '--gap', str(root / 'gefs_day1_adjusted'), '--output', str(root / 'product')])
    return root, index


def test_the_four_groups_cover_every_history_column():
    groups = [set(complete.WEATHER), set(complete.SNOW), set(complete.DERIVED), set(complete.PERSISTED)]
    assert set().union(*groups) == set(adjust.OUTPUT) and len(adjust.OUTPUT) == 28
    assert set(complete.SNOW) & set(complete.DERIVED) == set() and set(complete.PERSISTED).isdisjoint(complete.WEATHER)


def test_fit_recovers_the_snow_model_and_the_relations(world):
    root, index = world
    fitted = complete.load_fit(root / 'completion.json')
    assert fitted['projects'] == list(PROJECTS)
    for i, project in enumerate(PROJECTS):
        assert fitted['snow']['melt_factor'][i] == pytest.approx(MELT[project], abs=.5)
        assert fitted['snow']['threshold'][i] == pytest.approx(THRESHOLD[project], abs=.5)
    # The freezing level follows tmean at 160 m per degree: the regression on a held-out year is close.
    history = complete.load_history(root / 'history.parquet', ['tmean_degC', 'tmax_degC', 'tmin_degC',
                                                               'precipitation_mm', 'snow_water_equivalent_mm',
                                                               'freezing_level_above_sea_level_m'])
    days = history['first'] + np.arange(history['grid']['tmean_degC'].shape[1])
    held = (days >= np.datetime64('2024-01-01')) & (days < np.datetime64('2025-01-01'))
    month = days[held].astype('datetime64[M]').astype(int) % 12
    coefs = fitted['regression']['freezing_level_above_sea_level_m'][1][:, month]
    x = complete.design({name: grid[:, held] for name, grid in history['grid'].items()}, complete.PREDICTORS[1])
    predicted = np.einsum('pdk,pdk->pd', x, coefs)
    truth = history['grid']['freezing_level_above_sea_level_m'][:, held]
    assert np.sqrt(np.mean((predicted - truth) ** 2)) < 120
    assert fitted['regression']['relative_humidity_pct'][0] is None
    clim = fitted['climatology']['soil_temperature_layer4_degC']
    assert clim.shape == (3, 366) and np.isfinite(clim).all() and clim[:, 200].mean() > clim[:, 20].mean()


def test_product_tables_hold_every_member_as_one_series(world):
    root, index = world
    paths = sorted((root / 'product').glob(adjust.START))
    assert len(paths) == 23
    table = pq.read_table(root / 'product' / '2024040200.parquet')
    assert table.column_names == complete.KEYS + adjust.OUTPUT
    rows = table.to_pydict()
    assert set(rows['system']) == {'aifs', 'wn2'} and len(set(rows['member'])) == 5
    assert sorted(set(rows['lead_day'])) == list(range(1 - complete.GAP, LEADS + 1))
    assert table.num_rows == 5 * (complete.GAP + LEADS) * 3
    assert not any(np.isnan(np.array(rows[column])).any() for column in adjust.OUTPUT)
    # The gap days are shared: the first lead day is 2024-04-02, the day after the anchor 2024-03-28.
    gap = table.filter(pa.compute.equal(table['lead_day'], 1 - complete.GAP)).to_pydict()
    assert set(gap['valid_date']) == {date(2024, 3, 28)}
    for column in ('precipitation_mm', 'tmean_degC', 'wind_speed_m_s'):
        values = np.array(gap[column]).reshape(5, 3)
        np.testing.assert_allclose(values, np.broadcast_to(values[0], values.shape))
    # Every member's SWE starts from the history on the anchor day and follows its own snowfall and tmean.
    anchor = index[date(2024, 3, 27), 'BETA']['snow_water_equivalent_mm']
    member = table.filter(pa.compute.and_(pa.compute.equal(table['member'], 'p01'),
                                          pa.compute.equal(table['project_id'], 'BETA'))).to_pydict()
    swe, melt = complete.degree_day(np.array(anchor), np.array(member['snowfall_mm']), np.array(member['tmean_degC']),
                                    MELT['BETA'], THRESHOLD['BETA'])
    fitted = complete.load_fit(root / 'completion.json')
    if fitted['snow']['melt_factor'][1] == MELT['BETA'] and fitted['snow']['threshold'][1] == THRESHOLD['BETA']:
        np.testing.assert_allclose(member['snow_water_equivalent_mm'], swe)
        np.testing.assert_allclose(member['snowmelt_mm'], melt)
    assert member['snow_water_equivalent_mm'][0] == pytest.approx(anchor + member['snowfall_mm'][0] - member['snowmelt_mm'][0])
    # Persisted columns move with the climatology from the anchor value.
    clim = fitted['climatology']['soil_moisture_layer4_m3_m3'][1]
    expected = index[date(2024, 3, 27), 'BETA']['soil_moisture_layer4_m3_m3'] + clim[complete.day_of_year(
        [np.datetime64('2024-04-09')])[0]] - clim[complete.day_of_year([np.datetime64('2024-03-27')])[0]]
    assert member['soil_moisture_layer4_m3_m3'][member['valid_date'].index(date(2024, 4, 9))] == pytest.approx(expected)
    # AIFS keeps its own humidity; WN2's is derived, within bounds, and so are the other derived columns.
    aifs = np.array(rows['relative_humidity_pct'])[np.array(rows['system']) == 'aifs']
    wn2 = np.array(rows['relative_humidity_pct'])[np.array(rows['system']) == 'wn2']
    assert 0 <= wn2.min() and wn2.max() <= 100 and abs(aifs.mean() - wn2.mean()) < 8
    assert max(rows['cloud_cover_fraction']) <= 1 and min(rows['freezing_level_above_ground_m']) >= 0
    depth = np.array(rows['snow_depth_m'])
    assert (depth[np.array(rows['snow_water_equivalent_mm']) == 0] == 0).all()


def test_check_shows_the_derived_columns_close_to_the_history(world):
    root, _ = world
    with (root / 'product' / 'check.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row['system'] for row in rows} == {'aifs', 'wn2'}
    assert {int(row['lead_day']) for row in rows} == set(range(1 - complete.GAP, LEADS + 1))
    by = {(row['system'], row['column'], int(row['lead_day'])): row for row in rows}
    freezing = by[('wn2', 'freezing_level_above_sea_level_m', 3)]
    assert abs(float(freezing['bias'])) < 60 and float(freezing['mae']) < 150
    assert float(by[('aifs', 'snow_water_equivalent_mm', 1)]['mae']) < .3 * float(by[('aifs', 'snow_water_equivalent_mm', 1)]['history_mean']) + 1
    assert float(by[('aifs', 'tmean_degC', 5)]['crps']) < 1


def test_operations_run_one_start_with_other_members(world, tmp_path):
    root, index = world
    write_adjusted(tmp_path / 'aifs_adjusted', AIFS, [f'p{i:02d}' for i in range(7)], LEADS, [date(2024, 5, 1)], index)
    complete.main(['run', '--fit', str(root / 'completion.json'), '--history', str(root / 'history.parquet'),
                   '--forecast', str(tmp_path / 'aifs_adjusted'), '--gap', str(root / 'gefs_day1_adjusted'),
                   '--output', str(tmp_path / 'product')])
    table = pq.read_table(tmp_path / 'product' / '2024050100.parquet')
    assert len(set(table['member'].to_pylist())) == 7 and table.column_names == complete.KEYS + adjust.OUTPUT
    # A start that only the second system has is made too, from that system alone.
    write_adjusted(tmp_path / 'wn2_adjusted', WN2, ['ckpt1'], LEADS, [date(2024, 5, 5)], index)
    complete.main(['run', '--fit', str(root / 'completion.json'), '--history', str(root / 'history.parquet'),
                   '--forecast', str(tmp_path / 'aifs_adjusted'), str(tmp_path / 'wn2_adjusted'),
                   '--gap', str(root / 'gefs_day1_adjusted'), '--output', str(tmp_path / 'product')])
    later = pq.read_table(tmp_path / 'product' / '2024050500.parquet').to_pydict()
    assert set(later['system']) == {'wn2'} and set(later['member']) == {'ckpt1'}
    # A start whose gap tables are missing stops with the missing day.
    write_adjusted(tmp_path / 'late' / 'aifs_adjusted', AIFS, ['p00'], LEADS, [date(2024, 7, 1)], index)
    with pytest.raises(FileNotFoundError, match='2024062600'):
        complete.main(['run', '--fit', str(root / 'completion.json'), '--history', str(root / 'history.parquet'),
                       '--forecast', str(tmp_path / 'late' / 'aifs_adjusted'), '--gap', str(root / 'gefs_day1_adjusted'),
                       '--output', str(tmp_path / 'late' / 'product')])


def test_a_fit_with_other_settings_is_refused(world, tmp_path):
    root, _ = world
    fitted = json.loads((root / 'completion.json').read_text())
    fitted['info']['anchor'] = 5
    (tmp_path / 'other.json').write_text(json.dumps(fitted))
    with pytest.raises(ValueError, match='another anchor'):
        complete.load_fit(tmp_path / 'other.json')
