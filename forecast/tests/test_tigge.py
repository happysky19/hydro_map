from datetime import date

from catchment_forecast import tigge


def test_start_dates_follow_the_ai_hindcast_schedule():
    days = tigge.start_dates(2024)
    assert len(days) == 95
    assert [d.day for d in days if d.month == 2] == [1, 5, 9, 13, 17, 21, 25]
    assert days[0] == date(2024, 1, 1) and days[-1] == date(2024, 12, 29)
    assert len(tigge.start_dates(2023)) == 95


def test_plan_has_quarterly_controls_then_one_request_per_start():
    jobs = tigge.plan(2024)
    controls = [job for job in jobs if job[1] == 'cf']
    assert [name for name, _, _ in controls] == [f'cf_2024_q{q}.grib' for q in range(1, 5)]
    assert sum(len(dates) for _, _, dates in controls) == 95
    assert jobs[4] == ('pf_20240101.grib', 'pf', [date(2024, 1, 1)])
    assert len(jobs) == 4 + 95


def test_requests_crop_the_area_and_ask_members_only_for_perturbed_forecasts():
    control = tigge.request('cf', [date(2024, 1, 1), date(2024, 1, 5)], ['2t', 'tp'])
    assert control['param'] == '167/228228'
    assert control['date'] == ['2024-01-01', '2024-01-05']
    assert control['area'] == tigge.AREA and control['grid'] == '0.25/0.25'
    assert 'number' not in control
    perturbed = tigge.request('pf', [date(2024, 1, 1)], ['2t'])
    assert perturbed['type'] == 'pf' and perturbed['number'] == '1/to/50'
