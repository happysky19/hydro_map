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
    np.testing.assert_allclose(out['tmean_degC'], 10.)
    np.testing.assert_allclose(out['wind_speed_m_s'], 5.)
    assert out['pet_hargreaves_mm_day'].shape == (2, 2) and np.isfinite(out['pet_hargreaves_mm_day']).all()
    if system == 'aifs':
        np.testing.assert_allclose(out['shortwave_down_mean_W_m2'], 100.)
        expected = 100 * saturation_vapor_pressure_kpa(5.) / saturation_vapor_pressure_kpa(10.)
        np.testing.assert_allclose(out['relative_humidity_pct'], expected)
    else:
        assert 'relative_humidity_pct' not in out and 'shortwave_down_mean_W_m2' not in out


def test_a_missing_step_blanks_that_day_only():
    fields = synthetic('aifs', 3)
    del fields['2t', 30]
    out = aggregate.daily(fields, UNITS['aifs'], 'aifs', np.array([[1., 1., 1.]]), date(2024, 1, 1), [50.])
    assert np.isfinite(out['tmean_degC'][0]).all() and np.isnan(out['tmean_degC'][1]).all()
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
    np.testing.assert_allclose(table['tmean_degC'], 10., atol=1e-3)
    assert shapely.__version__


def test_operational_member_files_one_level_down_are_found(tmp_path):
    pq = pytest.importorskip('pyarrow.parquet')
    start = tmp_path / 'archive' / '2026110200' / 'FCST'
    start.mkdir(parents=True)
    for name in ('aifs_p00_N320.grib', 'aifs_p01_N320.grib', 'aifs_p02_N320.grib'):
        write_grib(start / name, 'reduced_gg_pl_320_grib1', lambda handle: None, synthetic('aifs', 1))
    # Beside them, one file per member and step, which are not members.
    post = start.parent / 'POST'
    post.mkdir()
    for member in range(3):
        for step in (6, 24):
            write_grib(post / f'aifs_p{member:02d}_f{step:03d}.grib', 'reduced_gg_pl_320_grib1', lambda handle: None,
                       {key: value for key, value in synthetic('aifs', 1).items() if key[1] == step})
    geojson = tmp_path / 'catchments.geojson'
    geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[
        dict(type='Feature', properties=dict(id=key, part='local'), geometry=mapping(geometry))
        for key, geometry in CATCHMENTS.items()])))
    aggregate.main(['--system', 'aifs', '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                    '--output', str(tmp_path / 'out'), '--start', '2026-11-02', '--end', '2026-11-02'])
    table = pq.read_table(tmp_path / 'out' / 'aifs' / '2026110200.parquet').to_pydict()
    assert sorted(set(table['member'])) == ['p00', 'p01', 'p02'] and len(table['member']) == 3 * 2 * 2


def test_member_files_take_hindcast_and_operational_names_only(tmp_path):
    names = ['aifs_f000-f528_p00.grib1', 'aifs_f000-f528_p20.grib1', 'FCST/aifs_p07_N320.grib',
             'POST/aifs_p07_f024.grib', 'POST/aifs_p07_f000.grib', 'aifs_p07_f528.grib']
    for name in names:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).touch()
    patterns = aggregate.SYSTEMS['aifs']['patterns']
    assert [path.name for path in aggregate.member_files(tmp_path, patterns)] == names[:2]
    (tmp_path / names[0]).unlink(), (tmp_path / names[1]).unlink()
    assert [path.name for path in aggregate.member_files(tmp_path, patterns)] == ['aifs_p07_N320.grib']
    assert aggregate.member_files(tmp_path / 'POST', patterns) == []


def test_gefs_member_names_and_land_means():
    assert aggregate.gefs_member('gefs_c00.grib2') == 'c00' and aggregate.gefs_member('gefs_p30.grib2') == 'p30'
    matrix = np.array([[1., 3., 0.], [0., 0., 2.]])
    np.testing.assert_allclose(grids.land_mean(matrix, [2., np.nan, 5.]), [2., 5.])
    assert np.isnan(grids.land_mean(matrix, [2., 4., np.nan])[1])


def gefs_fields(days=2):
    """GEFS point fields at three points; the third is water (no land fields), the first a glacier 1800 m up."""
    land = lambda value: np.array([value, value, np.nan])
    fields = {('orog', 6): np.array([1800., 1000., 1000.])}
    for step in range(0, 24 * days + 1, 6):
        fields['2t', step], fields['2d', step] = np.full(3, 283.15), np.full(3, 278.15)
        fields['10u', step], fields['10v', step] = np.full(3, 3.), np.full(3, 4.)
        fields['weasd', step], fields['snod', step] = np.array([6000., 100., np.nan]), np.array([9., .5, np.nan])
        fields['hgt0c', step] = np.full(3, 1500.)
        for i, layer in enumerate(aggregate.LAYERS, 1):
            fields[f'soilw{layer}', step] = land(i / 10)
            fields[f'tsoil{layer}', step] = land(273.15 + i)
        if step:
            for name, value in (('tp', 1.), ('dswrf', 200.), ('uswrf', 50.), ('dlwrf', 300.), ('ulwrf', 350.),
                                ('tcc', 40.), ('lhtfl', aggregate.VAPORIZATION / 86400)):
                fields[name, step] = np.full(3, value)
            fields['snohf', step] = land(2 * aggregate.FUSION / 86400)
    return fields


GEFS_EXPECTED = {
    'precipitation_mm': [4., 4.], 'tmean_degC': [10., 10.], 'wind_speed_m_s': [5., 5.],
    'shortwave_down_mean_W_m2': [200., 200.], 'net_radiation_mean_W_m2': [100., 100.],
    'cloud_cover_fraction': [.4, .4], 'actual_evapotranspiration_mm': [1., 1.], 'snowmelt_mm': [2., 2.],
    'snow_water_equivalent_mm': [100., 100.], 'snow_depth_m': [.5, .5],
    'freezing_level_above_ground_m': [250., 500.], 'freezing_level_above_sea_level_m': [1650., 1500.],
    'soil_moisture_layer1_m3_m3': [.1, .1], 'root_zone_soil_moisture_0_100cm_m3_m3': [.01 + .06 + .18] * 2,
    'soil_moisture_layer4_m3_m3': [.4, .4], 'soil_temperature_layer3_degC': [3., 3.],
}


def test_gefs_daily_values_follow_the_history_definitions():
    matrix = np.array([[1., 1., 0.], [0., 1., 1.]])
    out = aggregate.daily(gefs_fields(), {'2t': 'K', 'tp': 'kg m**-2'}, 'gefs', matrix, date(2024, 1, 1),
                          [52., 46.])
    for column, expected in GEFS_EXPECTED.items():
        np.testing.assert_allclose(out[column], [expected] * 2, err_msg=column)
    assert set(out) == set(aggregate.COLUMNS)


def test_a_gefs_day_missing_a_field_is_blank():
    fields = gefs_fields()
    del fields['dswrf', 36]
    out = aggregate.daily(fields, {'2t': 'K', 'tp': 'kg m**-2'}, 'gefs', np.ones((1, 3)), date(2024, 1, 1), [50.])
    assert np.isfinite(out['net_radiation_mean_W_m2'][0]).all() and np.isnan(out['net_radiation_mean_W_m2'][1]).all()
    assert np.isfinite(out['cloud_cover_fraction']).all()


GEFS_WEATHER = ('2t', '2d', '10u', '10v', 'tp', 'dswrf')
WEATHER_COLUMNS = {'precipitation_mm', 'tmean_degC', 'tmax_degC', 'tmin_degC', 'relative_humidity_pct',
                   'vapor_pressure_deficit_kPa', 'shortwave_down_mean_W_m2', 'u_wind_m_s', 'v_wind_m_s',
                   'wind_speed_m_s', 'pet_hargreaves_mm_day'}


def test_gefs_weather_fields_alone_give_the_weather_columns():
    fields = {key: value for key, value in gefs_fields().items() if key[0] in GEFS_WEATHER}
    out = aggregate.daily(fields, {'2t': 'K', 'tp': 'kg m**-2'}, 'gefs', np.ones((1, 3)), date(2024, 1, 1), [50.])
    assert set(out) == WEATHER_COLUMNS
    assert all(np.isfinite(out[column]).all() for column in out)


GEFS_PERIOD = {'tp': 1, 'dswrf': 0, 'uswrf': 0, 'dlwrf': 0, 'ulwrf': 0, 'tcc': 0, 'lhtfl': 0, 'snohf': 0}


def write_gefs(path, fields, window=6):
    """GRIB2 messages with the GEFS codes on a 1 degree grid; land fields carry a bitmap where they are missing."""
    codes = {name: code for code, name in aggregate.GEFS_CODES.items()}
    with open(path, 'wb') as stream:
        for (name, step), value in fields.items():
            base = name.rstrip('0123456789.-')
            code = codes[base]
            handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
            eccodes.codes_set(handle, 'centre', 'kwbc')
            for key, setting in (('Ni', 360), ('Nj', 181), ('latitudeOfFirstGridPointInDegrees', 90.),
                                 ('longitudeOfFirstGridPointInDegrees', 0.), ('latitudeOfLastGridPointInDegrees', -90.),
                                 ('longitudeOfLastGridPointInDegrees', 359.), ('iDirectionIncrementInDegrees', 1.),
                                 ('jDirectionIncrementInDegrees', 1.)):
                eccodes.codes_set(handle, key, setting)
            if base in GEFS_PERIOD:
                eccodes.codes_set(handle, 'productDefinitionTemplateNumber', 8)
                eccodes.codes_set(handle, 'typeOfStatisticalProcessing', GEFS_PERIOD[base])
            for key, setting in zip(('discipline', 'parameterCategory', 'parameterNumber', 'typeOfFirstFixedSurface'),
                                    code):
                eccodes.codes_set(handle, key, setting)
            if code[3] == 103:
                eccodes.codes_set(handle, 'scaleFactorOfFirstFixedSurface', 0)
                eccodes.codes_set(handle, 'scaledValueOfFirstFixedSurface', code[4])
            if code[3] == 106:
                top, bottom = (float(depth) for depth in name[len(base):].split('-'))
                for which, depth in (('First', top), ('Second', bottom)):
                    eccodes.codes_set(handle, f'typeOf{which}FixedSurface', 106)
                    eccodes.codes_set(handle, f'scaleFactorOf{which}FixedSurface', 2)
                    eccodes.codes_set(handle, f'scaledValueOf{which}FixedSurface', round(depth * 100))
            if base in GEFS_PERIOD:
                eccodes.codes_set(handle, 'stepRange', f'{step - window}-{step}')
            else:
                eccodes.codes_set(handle, 'step', step)
            values = np.full(360 * 181, value[1])
            if np.isnan(value).any():   # water along 46 N, under SWIFT
                eccodes.codes_set(handle, 'bitmapPresent', 1)
                values[44 * 360:45 * 360] = eccodes.codes_get(handle, 'missingValue')
            eccodes.codes_set_values(handle, values)
            eccodes.codes_write(handle, stream)
            eccodes.codes_release(handle)


def test_gefs_archive_to_parquet(tmp_path):
    pq = pytest.importorskip('pyarrow.parquet')
    start = tmp_path / 'archive' / '2024010100'
    start.mkdir(parents=True)
    write_gefs(start / 'gefs_c00.grib2', gefs_fields())
    geojson = tmp_path / 'catchments.geojson'
    geojson.write_text(json.dumps(dict(type='FeatureCollection', features=[
        dict(type='Feature', properties=dict(id=key, part='local'), geometry=mapping(geometry))
        for key, geometry in CATCHMENTS.items()])))
    assert aggregate.main(['--system', 'gefs', '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                           '--output', str(tmp_path / 'out')]) == 0
    table = pq.read_table(tmp_path / 'out' / 'gefs' / '2024010100.parquet').to_pydict()
    assert len(table['member']) == 2 * 2 and set(table['member']) == {'c00'}
    # Every point has the second point's values; SWIFT (rows 2 and 4) lies on water for the land fields.
    expected = dict(shortwave_down_mean_W_m2=200., net_radiation_mean_W_m2=100., cloud_cover_fraction=.4,
                    freezing_level_above_ground_m=500., snowmelt_mm=[2., np.nan] * 2,
                    snow_water_equivalent_mm=[100., np.nan] * 2, soil_temperature_layer2_degC=[2., np.nan] * 2)
    for column, value in expected.items():
        np.testing.assert_allclose(table[column], value, rtol=1e-4, err_msg=column)   # GRIB packing
    write_gefs(start / 'gefs_c00.grib2', gefs_fields(), window=12)
    (tmp_path / 'out' / 'gefs' / '2024010100.parquet').unlink()
    with pytest.raises(ValueError, match='not the 6 h before'):
        aggregate.main(['--system', 'gefs', '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                        '--output', str(tmp_path / 'out')])
    # A file of the weather fields only (gefs --weather) gives the weather columns; one short of them is refused.
    fields = {key: value for key, value in gefs_fields().items() if key[0] in GEFS_WEATHER}
    write_gefs(start / 'gefs_c00.grib2', fields)
    assert aggregate.main(['--system', 'gefs', '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                           '--output', str(tmp_path / 'out')]) == 0
    table = pq.read_table(tmp_path / 'out' / 'gefs' / '2024010100.parquet')
    assert set(table.column_names) - {'init_time', 'member', 'lead_day', 'valid_date', 'project_id'} == WEATHER_COLUMNS
    write_gefs(start / 'gefs_c00.grib2', {key: value for key, value in fields.items() if key[0] != 'dswrf'})
    (tmp_path / 'out' / 'gefs' / '2024010100.parquet').unlink()
    with pytest.raises(ValueError, match='no dswrf'):
        aggregate.main(['--system', 'gefs', '--archive', str(tmp_path / 'archive'), '--geojson', str(geojson),
                        '--output', str(tmp_path / 'out')])
