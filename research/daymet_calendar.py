"""Align Daymet local-day series to Gregorian dates without filling omitted days."""
from calendar import isleap
from datetime import date, timedelta
from math import isfinite, nan


def daymet_date(year, year_day):
    """Decode actual Gregorian day-of-year; Daymet never stores day 366."""
    if isinstance(year, bool) or int(year) != year:
        raise ValueError('Daymet year must be an integer')
    if isinstance(year_day, bool) or int(year_day) != year_day or not 1 <= year_day <= 365:
        raise ValueError('Daymet day-of-year must be an integer from 1 to 365')
    return date(int(year), 1, 1) + timedelta(days=int(year_day) - 1)


def calendar_gap(day):
    return isleap(day.year) and day.month == 12 and day.day == 31


def validate_window(year, first, last, dates, year_days):
    """Require an exact requested sequence of zero-based native day indices."""
    if not 0 <= first <= last < 365:
        raise ValueError('Invalid native Daymet window')
    expected = [daymet_date(year, index + 1) for index in range(first, last + 1)]
    decoded = [daymet_date(year, day) for day in year_days]
    if list(dates) != expected or decoded != expected:
        raise ValueError('Returned dates do not match the requested Daymet window')


def align_daymet_series(dates, values, start, end):
    """Return one row per Gregorian date; absent/invalid values remain NaN.

    Input dates must be date objects and already represent Daymet local days.
    This function does not convert local-day totals to UTC-day totals.
    """
    if start > end or len(dates) != len(values):
        raise ValueError('Invalid date interval or unequal input lengths')
    observed = {}
    for day, value in zip(dates, values):
        if not isinstance(day, date) or day < start or day > end:
            raise ValueError('Every input date must lie inside the requested interval')
        if day in observed:
            raise ValueError(f'Duplicate Daymet date: {day}')
        if calendar_gap(day):
            raise ValueError(f'Daymet cannot contain December 31 in a leap year: {day}')
        observed[day] = float(value) if isfinite(float(value)) else nan
    result = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        value = observed.get(day, nan)
        qc = 'daymet_calendar_gap' if calendar_gap(day) else ('valid' if isfinite(value) else 'source_missing')
        result.append({'date': day.isoformat(), 'value': value, 'qc': qc})
    return result
