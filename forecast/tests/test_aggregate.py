from datetime import date
import json

import numpy as np
import pytest
import shapely
from shapely.geometry import box, mapping

from catchment_forecast import aggregate, grids
from catchment_history.meteorology import saturation_vapor_pressure_kpa


CATCHMENTS = {'MICA': box(-118.9, 51.6, -118.1, 52.4), 'SWIFT': box(-122.4, 45.9, -121.9, 46.3)}


def test_regular_cells_cover_catchments_exactly_and_means_skip_nothing_silently():
    latitudes, longitudes = np.arange(90, -90.25, -0.25), np.arange(0, 360, 0.25)
    cells = grids.regular_cells(latitudes, longitudes)
    points, matrix = grids.weights(list(CATCHMENTS.values()), cells)
    assert matrix.shape == (2, len(points))
    values = np.ones(len(points))
    np.testing.assert_allclose(grids.area_mean(matrix, values), [1, 1])
    values[np.flatnonzero(matrix[0] > 0)[0]] = np.nan
    means = grids.area_mean(matrix, values)
    assert np.isnan(means[0]) and means[1] == 1


def test_reduced_gaussian_rows_meet_halfway_and_wrap_longitudes():
    lat, lon, (west, south, east, north) = grids.reduced_gaussian_cells([60., 40., 20.], [4, 8, 8])
    assert len(lat) == 20
    assert north[0] == 90 and south[0] == 50 and north[4] == 50 and south[4] == 30 and south[-1] == -90
    np.testing.assert_allclose(lon[:4], [0, 90, -180, -90])
    np.testing.assert_allclose(east[:4] - west[:4], 90)


def test_member_names_come_from_file_names():
    assert aggregate.aifs_member('aifs_f000-f528_p07.grib1') == 'p07'
    assert aggregate.aifs_member('aifs_p31_N320.grib') == 'p31'
    assert aggregate.wn2_member('predictions_hres_ckpt3.grib') == 'ckpt3_s000'
    assert aggregate.wn2_member('predictions_hres_s004_ckpt1.grib') == 'ckpt1_s004'


# AIFS (GRIB1) accumulates tp in m and ssrd in J/m2 from the start; WeatherNext 2 (GRIB2) gives 6 h tp in kg/m2.
UNITS = {'aifs': {'2t': 'K', 'tp': 'm', 'ssrd': 'J m**-2'}, 'wn2': {'2t': 'K', 'tp': 'kg m**-2'}}


def synthetic(system, points, days=2):
    """Point fields: 10 degC, dewpoint 5 degC, wind 3/4 m/s, 1 mm and 100 W/m2 every 6 h."""
    fields = {}
    first = aggregate.SYSTEMS[system]['first_step']
    for step in range(first, 24 * days + 1, 6):
        fields['2t', step] = np.full(points, 283.15)
        fields['10u', step], fields['10v', step] = np.full(points, 3.), np.full(points, 4.)
        if system == 'aifs':
            fields['2d', step] = np.full(points, 278.15)
            if step:
                fields['tp', step] = np.full(points, 0.001 * step / 6)
                fields['ssrd', step] = np.full(points, 100. * 3600 * step)
        else:
            fields['tp', step] = np.full(points, 1.)
    return fields


@pytest.mark.parametrize('system', ['aifs', 'wn2'])
def test_daily_values_follow_the_history_conventions(system):
    matrix = np.array([[1., 2., 0.], [0., 1., 1.]])
    out = aggregate.daily(synthetic(system, 3), UNITS[system], system, matrix, date(2024, 1, 1), [52., 46.])
    np.testing.assert_allclose(out['precipitation_mm'], 4.)
    np.testing.assert_allclose(out['tmean_c'], 10.)
    np.testing.assert_allclose(out['wind_speed_ms'], 5.)
    assert out['pet_hargreaves_mm'].shape == (2, 2) and np.isfinite(out['pet_hargreaves_mm']).all()
    if system == 'aifs':
        np.testing.assert_allclose(out['shortwave_down_mean_wm2'], 100.)
        expected = 100 * saturation_vapor_pressure_kpa(5.) / saturation_vapor_pressure_kpa(10.)
        np.testing.assert_allclose(out['relative_humidity_pct'], expected)
    else:
        assert 'relative_humidity_pct' not in out and 'shortwave_down_mean_wm2' not in out


def test_a_missing_step_blanks_that_day_only():
    fields = synthetic('aifs', 3)
    del fields['2t', 30]
    out = aggregate.daily(fields, UNITS['aifs'], 'aifs', np.array([[1., 1., 1.]]), date(2024, 1, 1), [50.])
    assert np.isfinite(out['tmean_c'][0]).all() and np.isnan(out['tmean_c'][1]).all()
    assert np.isfinite(out['precipitation_mm']).all()


def test_amounts_in_unknown_units_are_refused():
    with pytest.raises(ValueError, match='no conversion'):
        aggregate.daily(synthetic('wn2', 3), dict(UNITS['wn2'], tp='mm h**-1'), 'wn2', np.ones((1, 3)),
                        date(2024, 1, 1), [50.])


eccodes = pytest.importorskip('eccodes')
PARAMS = {'2t': 167, '2d': 168, '10u': 165, '10v': 166, 'tp': 228, 'ssrd': 169}


def write_grib(path, sample, setup, fields):
    with open(path, 'wb') as stream:
        for (name, step), value in fields.items():
            handle = eccodes.codes_grib_new_from_samples(sample)
            setup(handle)
            eccodes.codes_set(handle, 'paramId', PARAMS[name] if 'grib1' in sample or name != 'tp' else 228228)
            if name in ('tp', 'ssrd') and 'grib1' in sample:
                eccodes.codes_set(handle, 'stepType', 'accum')
                eccodes.codes_set(handle, 'stepRange', f'0-{step}')
            else:
                eccodes.codes_set(handle, 'step', step)
            regular = eccodes.codes_get(handle, 'gridType') == 'regular_ll'
            size = (eccodes.codes_get(handle, 'Ni') * eccodes.codes_get(handle, 'Nj') if regular
                    else eccodes.codes_get(handle, 'numberOfDataPoints'))
            eccodes.codes_set_values(handle, np.full(size, value[0]))
            eccodes.codes_write(handle, stream)
            eccodes.codes_release(handle)


def regular_quarter_degree(handle):
    for key, value in (('Ni', 1440), ('Nj', 721), ('latitudeOfFirstGridPointInDegrees', 90.),
                       ('longitudeOfFirstGridPointInDegrees', 0.), ('latitudeOfLastGridPointInDegrees', -90.),
                       ('longitudeOfLastGridPointInDegrees', 359.75), ('iDirectionIncrementInDegrees', .25),
                       ('jDirectionIncrementInDegrees', .25)):
        eccodes.codes_set(handle, key, value)


@pytest.mark.parametrize('system', ['aifs', 'wn2'])
def test_archive_to_parquet(tmp_path, system):
    pq = pytest.importorskip('pyarrow.parquet')
    start = tmp_path / 'archive' / '2024010100'
    start.mkdir(parents=True)
    fields = synthetic(system, 1)
    if system == 'aifs':
        files, sample, setup = ['aifs_f000-f528_p00.grib1', 'aifs_f000-f528_p01.grib1'], 'reduced_gg_pl_320_grib1', \
            (lambda handle: None)
    else:
        files, sample, setup = ['predictions_hres_ckpt1.grib', 'predictions_hres_s001_ckpt1.grib'], \
            'regular_ll_sfc_grib2', regular_quarter_degree
    for name in files:
        write_grib(start / name, sample, setup, fields)
    geojson = tmp_path / 'catchments.geojson'
    geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[
        dict(type='Feature', properties=dict(id=key, part='local'), geometry=mapping(geometry))
        for key, geometry in CATCHMENTS.items()])))
    assert aggregate.main(['--system', system, '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                           '--output', str(tmp_path / 'out')]) == 0
    table = pq.read_table(tmp_path / 'out' / system / '2024010100.parquet')
    assert table.schema.field('init_time').type.tz == 'UTC'
    assert str(table.schema.field('valid_date').type) == 'date32[day]'
    table = table.to_pydict()
    assert len(table['member']) == 2 * 2 * 2   # members x days x catchments
    assert sorted(set(table['member'])) == (['p00', 'p01'] if system == 'aifs' else ['ckpt1_s000', 'ckpt1_s001'])
    assert table['valid_date'][:4] == [date(2024, 1, 1)] * 2 + [date(2024, 1, 2)] * 2
    np.testing.assert_allclose(table['precipitation_mm'], 4., rtol=1e-5)
    np.testing.assert_allclose(table['tmean_c'], 10., atol=1e-3)
    assert shapely.__version__
