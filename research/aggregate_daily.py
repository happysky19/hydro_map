"""Stream sorted hourly catchment means into strict UTC daily statistics.

Input CSV must be sorted by (source, project_id, variable, time_utc). Values
must already use the units below; .gz input/output paths use gzip text streams.
Only complete 24-hour, full-area days yield
values. Temperature extrema are extrema of the HOURLY CATCHMENT MEAN, not
areal means of cellwise extrema. Radiation energy from instantaneous samples
is a rectangular hourly integration estimate. Hourly nonlinear fields must be
derived on native cells before spatial averaging. Optional daily PET uses
catchment temperature statistics and the supplied polygon centroid latitude.
"""

import argparse
import csv
import gzip
from datetime import date, datetime, timedelta
from itertools import groupby
import math
from pathlib import Path
import tempfile

from meteorology import DERIVED_FIELDS, hargreaves_pet


HOURLY_FIELDS = ['source', 'project_id', 'time_utc', 'variable', 'value', 'units',
                 'temporal_kind', 'valid_area_fraction', 'qc']
DAILY_FIELDS = ['date', 'source', 'project_id', 'variable', 'value', 'units',
                'expected_hours', 'valid_hours', 'min_valid_area_fraction', 'qc']
RULES = {
    'precipitation_mm': ('mm', {'hour_ending_amount'}),
    'temperature_c': ('degC', {'instantaneous'}),
    'specific_humidity_kgkg': ('kg/kg', {'instantaneous'}),
    'surface_pressure_pa': ('Pa', {'instantaneous'}),
    'u_wind_ms': ('m/s', {'instantaneous'}),
    'v_wind_ms': ('m/s', {'instantaneous'}),
    'shortwave_down_wm2': ('W/m2', {'instantaneous', 'hour_ending_mean'}),
    'longwave_down_wm2': ('W/m2', {'instantaneous', 'hour_ending_mean'}),
    'snow_water_equivalent_mm': ('mm', {'instantaneous'}),
}
RULES.update({name: (units, {kind}) for name, (units, kind) in DERIVED_FIELDS.items()})
AMOUNTS = {'precipitation_mm', 'rainfall_mm', 'snowfall_mm'}
AREA_TOLERANCE = 1e-9


def _hourly_rows(stream):
    reader = csv.DictReader(stream)
    if reader.fieldnames is None or not set(HOURLY_FIELDS) <= set(reader.fieldnames):
        raise ValueError('Hourly CSV is missing required columns')
    previous = previous_metadata = None
    for row in reader:
        key = tuple(row[field] for field in ['source', 'project_id', 'variable'])
        if any(not value or value != value.strip() for value in key):
            raise ValueError('Source, project_id and variable must be nonempty without surrounding spaces')
        variable, kind = row['variable'], row['temporal_kind']
        if variable not in RULES or (row['units'] != RULES[variable][0]
                                    or kind not in RULES[variable][1]):
            raise ValueError(f'Unsupported variable, units or temporal_kind: {variable}')
        stamp = datetime.fromisoformat(row['time_utc'].replace('Z', '+00:00'))
        if stamp.utcoffset() != timedelta(0) or stamp.minute or stamp.second or stamp.microsecond:
            raise ValueError('Timestamps must be exact UTC hours with an explicit UTC offset')
        order = (*key, stamp)
        if previous is not None and order <= previous:
            raise ValueError('Hourly CSV must be sorted by source, project_id, variable, time; duplicates are forbidden')
        metadata = (row['units'], kind)
        if previous is not None and key == previous[:3] and metadata != previous_metadata:
            raise ValueError('Units and temporal_kind must stay constant within each series')
        previous, previous_metadata = order, metadata
        area = float(row['valid_area_fraction'])
        if not math.isfinite(area) or not 0 <= area <= 1 + AREA_TOLERANCE:
            raise ValueError('valid_area_fraction must be finite and between zero and one')
        if not row['qc']:
            raise ValueError('Each hourly value requires a QC flag')
        value = float(row['value']) if row['value'] else math.nan
        row.update(key=key, day=(stamp - timedelta(hours=kind.startswith('hour_ending'))).date(),
                   number=value, area=min(area, 1.),
                   valid=row['qc'] == 'valid' and math.isfinite(value) and abs(area - 1) <= AREA_TOLERANCE)
        yield row


def _statistics(variable, numbers):
    if variable == 'temperature_c':
        return [('tmean_c', 'degC', math.fsum(numbers) / 24),
                ('tmin_c', 'degC', min(numbers)), ('tmax_c', 'degC', max(numbers))]
    total = math.fsum(numbers)
    if variable in {'shortwave_down_wm2', 'longwave_down_wm2'}:
        prefix = variable.removesuffix('_wm2')
        return [(prefix + '_mean_wm2', 'W/m2', total / 24),
                (prefix + '_energy_mjm2', 'MJ/m2', total * .0036)]
    return [(variable, RULES[variable][0], total if variable in AMOUNTS else total / 24)]


def daily_schema(variables, include_pet=False):
    """Serializable names, units and statistics for the exact emitted daily fields."""
    result = {}
    for variable in sorted(variables):
        for name, units, _ in _statistics(variable, [0.] * 24):
            statistic = ('sum' if variable in AMOUNTS else
                         'minimum_of_hourly_catchment_means' if name == 'tmin_c' else
                         'maximum_of_hourly_catchment_means' if name == 'tmax_c' else
                         'hourly_rectangular_energy_integral' if name.endswith('_energy_mjm2') else
                         'mean_of_hourly_catchment_means')
            result[name] = dict(units=units, statistic=statistic)
    if include_pet:
        result['pet_hargreaves_mm'] = dict(units='mm/day', statistic='daily_hargreaves_samani_estimate')
    return result


def _daily_rows(key, rows, start, end, pet_latitude=None):
    source, project, variable = key
    rows = iter(rows)
    current = next(rows, None)
    day = start
    while day <= end:
        while current is not None and current['day'] < day:
            current = next(rows, None)
        bucket = []
        while current is not None and current['day'] == day:
            bucket.append(current)
            current = next(rows, None)
        valid = sum(row['valid'] for row in bucket)
        complete = valid == 24
        # Missing dates still emit every statistic, with blank values.
        numbers = [row['number'] for row in bucket] if complete else [0.] * 24
        statistics = _statistics(variable, numbers)
        if variable == 'temperature_c' and pet_latitude is not None:
            value = hargreaves_pet(*(stat[2] for stat in statistics), pet_latitude, day) if complete else 0.
            statistics.append(('pet_hargreaves_mm', 'mm/day', value))
        for name, units, value in statistics:
            if complete and not math.isfinite(value):
                raise ValueError('Daily statistic is not finite')
            yield dict(date=day.isoformat(), source=source, project_id=project,
                       variable=name, value=value if complete else '', units=units,
                       expected_hours=24, valid_hours=valid,
                       min_valid_area_fraction=min(row['area'] for row in bucket) if len(bucket) == 24 else 0.,
                       qc='valid' if complete else ('missing_hours' if len(bucket) < 24 else 'invalid_hours'))
        day += timedelta(days=1)
    # Validate remaining out-of-window input, including duplicate boundary hours.
    for _ in rows:
        pass


def aggregate_daily(hourly_csv, daily_csv, start, end, pet_latitudes=None):
    """Write a complete date grid per seen series; atomically replace output.

    Source groups never blend. At most one day's 24 observations are retained.
    Missing timestamps imply zero minimum area coverage for that day. A failed
    run leaves an existing output unchanged and removes its temporary file.
    pet_latitudes enables a daily Hargreaves estimate for temperature series;
    its QC and coverage are exactly those of the required temperature statistics.
    """
    hourly_csv, daily_csv = Path(hourly_csv), Path(daily_csv)
    if start > end:
        raise ValueError('Start date must not follow end date')
    if hourly_csv.resolve() == daily_csv.resolve():
        raise ValueError('Output must not overwrite the hourly input')
    daily_csv.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    count = 0
    input_open = gzip.open if hourly_csv.suffix == '.gz' else open
    output_open = gzip.open if daily_csv.suffix == '.gz' else open
    try:
        with tempfile.NamedTemporaryFile(dir=daily_csv.parent, prefix='.daily-', delete=False) as pending:
            temporary = Path(pending.name)
        with input_open(hourly_csv, 'rt', newline='') as stream, output_open(temporary, 'wt', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=DAILY_FIELDS)
            writer.writeheader()
            for key, rows in groupby(_hourly_rows(stream), key=lambda row: row['key']):
                latitude = None
                if pet_latitudes is not None and key[2] == 'temperature_c':
                    latitude = pet_latitudes.get(key[1])
                    if latitude is None or not math.isfinite(latitude) or not -90 <= latitude <= 90:
                        raise ValueError(f'PET needs a finite centroid latitude for {key[1]}')
                for row in _daily_rows(key, rows, start, end, latitude):
                    writer.writerow(row)
                    count += 1
            if not count:
                raise ValueError('Hourly CSV contains no series')
        temporary.replace(daily_csv)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    args = parser.parse_args()
    count = aggregate_daily(args.input, args.output, args.start, args.end)
    print(f'Wrote {count} daily rows; incomplete days retain blank values.')


if __name__ == '__main__':
    main()
