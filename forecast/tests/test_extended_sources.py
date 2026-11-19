"""The readers and downloaders of IFS open data, CFSv2, EC46 (S2S) and SEAS5, and the extended-range table."""
from datetime import date, datetime, timezone
import json

import numpy as np
import pytest

from catchment_forecast import adjust, aggregate, cfs, extended, ifs, s2s, seas5
from catchment_history.meteorology import saturation_vapor_pressure_kpa


MATRIX = np.array([[1., 1., 0.], [0., 1., 1.]])
LATITUDES = [52., 46.]


def cfs_fields(first_hour, last_hour):
    """CFSv2 point fields every 6 h: 10 degC with 6 h extremes 8 and 13 degC, half-saturated air, 2 mm a day of
    precipitation rate, and fluxes giving 50 W/m2 of net radiation."""
    fields = {}
    vapour = 0.5 * saturation_vapor_pressure_kpa(10.) * 1000   # Pa
    q = 0.622 * vapour / (90000 - 0.378 * vapour)
    for step in range(first_hour, last_hour + 1, 6):
        for name, value in (('2t', 283.15), ('tmax', 286.15), ('tmin', 281.15), ('q', q), ('sp', 90000.),
                            ('10u', 3.), ('10v', 4.), ('prate', 2 / 86400), ('dswrf', 150.), ('uswrf', 30.),
                            ('dlwrf', 280.), ('ulwrf', 350.), ('tsoil0-0.1', 275.15)):
            fields[name, step] = np.full(3, value)
    fields['tmax', 18] = np.full(3, 290.15)   # the warmest window of the first day of a 00 UTC run
    return fields


CFS_UNITS = {'2t': 'K', 'tmax': 'K', 'tmin': 'K'}


def test_cfs_days_take_the_utc_day_of_each_run():
    out = aggregate.daily(cfs_fields(6, 72), CFS_UNITS, 'cfs', MATRIX, date(2026, 11, 15), LATITUDES)
    assert len(out['tmean_degC']) == 3
    np.testing.assert_allclose(out['tmean_degC'], 10.)
    np.testing.assert_allclose(out['tmax_degC'][0], 17.)
    np.testing.assert_allclose(out['tmax_degC'][1:], 13.)
    np.testing.assert_allclose(out['tmin_degC'], 8.)
    np.testing.assert_allclose(out['relative_humidity_pct'], 50., rtol=1e-6)
    np.testing.assert_allclose(out['vapor_pressure_deficit_kPa'], 0.5 * saturation_vapor_pressure_kpa(10.))
    np.testing.assert_allclose(out['precipitation_mm'], 2.)
    np.testing.assert_allclose(out['net_radiation_mean_W_m2'], 50.)
    np.testing.assert_allclose(out['shortwave_down_mean_W_m2'], 150.)
    np.testing.assert_allclose(out['soil_temperature_layer1_degC'], 2.)
    np.testing.assert_allclose(out['wind_speed_m_s'], 5.)
    # A 06 UTC run: its first lead day starts 18 h in, so step 18 is the first instant of that day.
    fields = cfs_fields(6, 90)
    fields['2t', 18] = np.full(3, 293.15)
    out = aggregate.daily(fields, CFS_UNITS, 'cfs', MATRIX, date(2026, 11, 16), LATITUDES, shift=18)
    assert len(out['tmean_degC']) == 3
    np.testing.assert_allclose(out['tmean_degC'][0], 12.5)   # (20 + 10 + 10 + 10) / 4
    np.testing.assert_allclose(out['tmean_degC'][1:], 10.)


def test_net_radiation_adds_signed_amounts_and_needs_both():
    fields = {}
    for day in range(3):
        fields['2t', 24 * day], fields['2d', 24 * day] = np.full(3, 283.15), np.full(3, 278.15)
        fields['10u', 24 * day], fields['10v', 24 * day] = np.full(3, 3.), np.full(3, 4.)
        fields['tp', 24 * day] = np.full(3, 2. * day)   # kg m-2 from the start
        fields['ssrd', 24 * day] = np.full(3, 100. * 86400 * day)
        fields['ssr', 24 * day] = np.full(3, 80. * 86400 * day)
        fields['str', 24 * day] = np.full(3, -60. * 86400 * day)
        fields['st20', 24 * day] = np.array([276.15, 276.15, np.nan])
    for step in range(6, 49, 6):
        fields['mx2t6', step], fields['mn2t6', step] = np.full(3, 285.15 + step / 6), np.full(3, 280.15 - step / 6)
    units = {'2t': 'K', 'tp': 'kg m**-2', 'ssrd': 'J m**-2', 'ssr': 'J m**-2', 'str': 'J m**-2', 'mx2t6': 'K',
             'mn2t6': 'K'}
    out = aggregate.daily(fields, units, 'ec46', MATRIX, date(2026, 11, 12), LATITUDES)
    np.testing.assert_allclose(out['precipitation_mm'], 2.)
    np.testing.assert_allclose(out['net_radiation_mean_W_m2'], 20.)
    np.testing.assert_allclose(out['shortwave_down_mean_W_m2'], 100.)
    np.testing.assert_allclose(out['tmax_degC'], [[16., 16.], [20., 20.]])   # the last window of each day
    np.testing.assert_allclose(out['tmin_degC'], [[3., 3.], [-1., -1.]])
    np.testing.assert_allclose(out['tmean_degC'], 10.)
    np.testing.assert_allclose(out['soil_temperature_layer1_degC'], 3.)   # land points only
    expected = 100 * saturation_vapor_pressure_kpa(5.) / saturation_vapor_pressure_kpa(10.)
    np.testing.assert_allclose(out['relative_humidity_pct'], expected)
    del units['str']
    assert 'net_radiation_mean_W_m2' not in aggregate.daily(fields, units, 'ec46', MATRIX, date(2026, 11, 12),
                                                            LATITUDES)


def test_seas5_extremes_come_from_the_24_h_ending_each_day():
    fields = {}
    for step in range(6, 49, 6):
        for name, value in (('2t', 283.15), ('2d', 278.15), ('10u', 3.), ('10v', 4.), ('stl1', 274.15)):
            fields[name, step] = np.full(3, value)
        for name, rate in (('tp', .002 / 24), ('ssrd', 100. * 3600), ('ssr', 90. * 3600), ('str', -70. * 3600)):
            fields[name, step] = np.full(3, rate * step)
        fields['mx2t24', step], fields['mn2t24', step] = np.full(3, 288.15 + step), np.full(3, 278.15 - step)
    units = {'2t': 'K', 'tp': 'm', 'ssrd': 'J m**-2', 'ssr': 'J m**-2', 'str': 'J m**-2', 'mx2t24': 'K',
             'mn2t24': 'K'}
    out = aggregate.daily(fields, units, 'seas5', MATRIX, date(2026, 11, 1), LATITUDES)
    np.testing.assert_allclose(out['tmax_degC'], [[39., 39.], [63., 63.]])
    np.testing.assert_allclose(out['tmin_degC'], [[-19., -19.], [-43., -43.]])
    np.testing.assert_allclose(out['precipitation_mm'], 2.)
    np.testing.assert_allclose(out['net_radiation_mean_W_m2'], 20.)
    np.testing.assert_allclose(out['tmean_degC'], 10.)   # three samples on the first day


def test_cfs_index_ranges_cover_each_field_to_the_hours():
    lines = [f'{i + 1}:{1000 * i}:d=2026111600:UGRD:10 m above ground:{6 * (i + 1)} hour fcst:' for i in range(8)]
    lines += [f'{i + 9}:{1000 * (i + 8)}:d=2026111600:VGRD:10 m above ground:{6 * (i + 1)} hour fcst:'
              for i in range(8)]
    assert cfs.step_ranges('\n'.join(lines), 24) == [(0, 3999), (8000, 11999)]
    assert cfs.step_ranges('\n'.join(lines), 48) == [(0, None)]
    with pytest.raises(ValueError, match='reaches 48 h'):
        cfs.step_ranges('\n'.join(lines), 54)
    starts = cfs.runs(date(2026, 11, 15), date(2026, 11, 16), [0, 6, 12, 18], every_day=True)
    assert len(starts) == 8 and starts[1] == datetime(2026, 11, 15, 6, tzinfo=timezone.utc)
    assert cfs.url(starts[1], 'prate').endswith('cfs.20261115/06/time_grib_01/prate.01.2026111506.daily.grb2')


def test_ifs_index_picks_the_wanted_members_and_fields():
    entry = lambda param, kind, number, offset: json.dumps(dict(param=param, type=kind, _offset=offset, _length=10,
                                                                **({'number': str(number)} if number else {})))
    index = '\n'.join([entry('2t', 'cf', None, 0), entry('2d', 'cf', None, 10), entry('10u', 'cf', None, 20),
                       entry('10v', 'cf', None, 30), entry('tp', 'cf', None, 40), entry('ssrd', 'cf', None, 50),
                       entry('msl', 'cf', None, 60), entry('2t', 'pf', 1, 70), entry('2d', 'pf', 1, 80),
                       entry('10u', 'pf', 1, 100), entry('10v', 'pf', 1, 110), entry('tp', 'pf', 1, 120),
                       entry('ssrd', 'pf', 1, 130), entry('2t', 'pf', 2, 140)])
    found = ifs.wanted(index, 24, ifs.members(2))
    assert {member for _, _, member in found} == {'c00', 'p01'} and len(found) == 12
    assert ifs.check(found, 24, ifs.members(2)) == ['c00', 'p01']
    runs = ifs.runs_of(found)
    assert [first for first, _ in runs] == [0, 70, 100]   # msl at 60 and the gap at 90 split the requests
    assert len(ifs.wanted(index, 6, ifs.members(2))) == 8   # no accumulations off the whole days
    with pytest.raises(ValueError, match='p02 lack fields'):
        ifs.check(ifs.wanted(index, 24, ifs.members(3)), 24, ifs.members(3))
    assert ifs.url(date(2026, 11, 15), 24).endswith('/20261115/00z/ifs/0p25/enfo/20261115000000-24h-enfo-ef')


def test_s2s_requests_and_seas5_months():
    request = s2s.body(date(2026, 1, 17), ['pf'], years=range(2006, 2026))
    assert request['hyear'][0] == '2006' and request['hmonth'] == '01' and request['hday'] == '17'
    assert request['forecast_type'] == ['perturbed_forecast']
    leads = s2s.leadtimes()
    assert leads[:2] == ['0', '6'] and '1104' in leads and leads[-1] == '1080_1104' and len(leads) == 185 + 46
    assert s2s.parts() == [['cf', 'pf']] and s2s.parts(range(2006, 2026)) == [['cf'], ['pf']]
    assert 'hyear' not in s2s.body(date(2026, 11, 12), ['cf', 'pf'])
    assert [f'{m:%Y-%m}' for m in seas5.months(date(2025, 11, 1), date(2026, 2, 1))] == \
        ['2025-11', '2025-12', '2026-01', '2026-02']
    assert seas5.body(date(2026, 11, 1), 48)['leadtime_hour'] == ['6', '12', '18', '24', '30', '36', '42', '48']


def test_extended_table_names_cfs_members_by_run(tmp_path):
    pa = pytest.importorskip('pyarrow')
    pq = pytest.importorskip('pyarrow.parquet')

    def write(path, init, members, leads=3):
        rows = len(members) * leads
        pq.write_table(pa.table({
            'init_time': pa.array([init] * rows, pa.timestamp('s', tz='UTC')),
            'member': [m for m in members for _ in range(leads)],
            'lead_day': pa.array(list(range(1, leads + 1)) * len(members), pa.int16()),
            'valid_date': pa.array([date(init.year, init.month, init.day + d) for d in range(leads)] * len(members),
                                   pa.date32()),
            'project_id': ['MICA'] * rows, 'precipitation_mm': np.ones(rows), 'tmean_degC': np.zeros(rows)}), path)
    write(tmp_path / 'a.parquet', datetime(2026, 11, 16, 6, tzinfo=timezone.utc), ['01'])
    write(tmp_path / 'b.parquet', datetime(2026, 11, 16, 12, tzinfo=timezone.utc), ['01'])
    write(tmp_path / 'c.parquet', datetime(2026, 11, 16, tzinfo=timezone.utc), ['c00', 'p001'])
    table = extended.assemble(date(2026, 11, 17), [('cfs', tmp_path / 'a.parquet'), ('cfs', tmp_path / 'b.parquet'),
                                                   ('ec46', tmp_path / 'c.parquet')]).to_pydict()
    assert sorted(set(table['member'])) == ['2026111606_01', '2026111612_01', 'c00', 'p001']
    assert min(table['valid_date']) == date(2026, 11, 17)
    assert list(table)[:6] == extended.KEYS and list(table)[6:] == extended.COLUMNS
    assert np.isnan(table['soil_temperature_layer1_degC']).all()


def test_a_mapping_keeps_its_lead_groups(tmp_path):
    rng = np.random.default_rng(3)
    n = 40000
    rows = dict(project_id=np.array(['ALPHA'] * n), lead_day=rng.integers(1, 181, n),
                valid_date=np.datetime64('2024-01-01') + rng.integers(0, 730, n), tmean_degC=rng.normal(5, 8, n))
    history = dict(tmean_degC=rows['tmean_degC'] - 2)
    mapping = adjust.fit(rows, history, ['tmean_degC'], ['ALPHA'], {'ALPHA': (0., 1.)}, {'ALPHA': 50.}, {},
                         adjust.EXTENDED_GROUPS)
    adjust.save(mapping, tmp_path / 'mapping.parquet')
    loaded = adjust.load(tmp_path / 'mapping.parquet')
    assert loaded['lead_groups'] == adjust.EXTENDED_GROUPS
    probe = dict(project_id=np.array(['ALPHA'] * 3), lead_day=np.array([1, 50, 180]),
                 valid_date=np.array(['2024-07-01'] * 3, dtype='datetime64[D]'), tmean_degC=np.array([5., 5., 5.]),
                 precipitation_mm=np.zeros(3), tmax_degC=np.full(3, 9.), tmin_degC=np.full(3, 1.))
    mapping['quantiles']['precipitation_mm'] = (np.tile(np.linspace(0, 10, len(adjust.LEVELS)),
                                                        (len(mapping['quantiles']['tmean_degC'][0]), 1)),) * 2
    for name in ('tmax_degC', 'tmin_degC'):
        mapping['quantiles'][name] = mapping['quantiles']['precipitation_mm']
    np.testing.assert_allclose(adjust.adjust(probe, mapping)['tmean_degC'], 3., atol=.3)


eccodes = pytest.importorskip('eccodes')


def test_s2s_messages_are_sorted_by_start_and_member(tmp_path):
    raw = tmp_path / 'raw.grib'
    with open(raw, 'wb') as stream:
        for day, number in ((20061117, 0), (20061117, 3), (20071117, 3)):
            handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
            eccodes.codes_set(handle, 'dataDate', day)
            eccodes.codes_set(handle, 'dataTime', 0)
            eccodes.codes_set(handle, 'productDefinitionTemplateNumber', 1)
            eccodes.codes_set(handle, 'perturbationNumber', number)
            eccodes.codes_write(handle, stream)
            eccodes.codes_release(handle)
    assert s2s.split([raw], tmp_path / 'archive') == ['2006111700', '2007111700']
    assert sorted(p.name for p in (tmp_path / 'archive' / '2006111700').iterdir()) == ['ec46_c00.grib',
                                                                                        'ec46_p003.grib']
    assert aggregate.ec46_member('ec46_p003.grib') == 'p003' and aggregate.ec46_member('seas5_c00.grib') == 'c00'


@pytest.mark.parametrize('day, expected', [
    (date(2026, 11, 6), date(2026, 10, 1)),
    (date(2026, 11, 7), date(2026, 11, 1)),
    (date(2027, 1, 1), date(2026, 12, 1)),
])
def test_seas5_uses_only_months_released_before_the_product_day(day, expected):
    assert seas5.latest_month(day) == expected


def test_extended_skips_expired_cfs_runs(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    for name, valid in [('old', date(2026, 1, 1)), ('current', date(2026, 11, 19))]:
        pq.write_table(pa.table({
            'init_time': pa.array([datetime(2026, 1, 1, tzinfo=timezone.utc)], pa.timestamp('s', tz='UTC')),
            'member': ['01'], 'lead_day': pa.array([1], pa.int16()),
            'valid_date': pa.array([valid], pa.date32()), 'project_id': ['MICA']}), tmp_path / f'{name}.parquet')
    table = extended.assemble(date(2026, 11, 19), [('cfs', tmp_path / 'old.parquet'),
                                                ('gefs', tmp_path / 'current.parquet')])
    assert table['system'].to_pylist() == ['gefs']
    with pytest.raises(ValueError, match='No forecast rows'):
        extended.assemble(date(2026, 11, 19), [('cfs', tmp_path / 'old.parquet')])


def test_ec46_reads_daily_average_temperature_names(tmp_path):
    from shapely.geometry import box
    path = tmp_path / 'ec46_c00.grib'
    with path.open('wb') as stream:
        for param in [167, 168]:
            handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
            eccodes.codes_set(handle, 'paramId', param)
            eccodes.codes_set(handle, 'stepType', 'avg')
            eccodes.codes_set(handle, 'stepRange', '0-24')
            eccodes.codes_set_values(handle, np.full(eccodes.codes_get(handle, 'numberOfPoints'), 283.15))
            eccodes.codes_write(handle, stream)
            eccodes.codes_release(handle)
    catchments = aggregate.Catchments.__new__(aggregate.Catchments)
    catchments.geometries, catchments.grids = [box(1., 1., 2., 2.)], {}
    fields, units, _ = aggregate.read_member(path, {'2t', '2d'}, catchments,
                                            aggregate.SYSTEMS['ec46'].get('naming'), required=('2t', '2d'))
    assert set(fields) == {('2t', 24), ('2d', 24)}
    assert units == {'2t': 'K', '2d': 'K'}
