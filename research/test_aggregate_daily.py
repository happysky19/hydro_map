"""Check strict daily UTC statistics from hourly catchment means."""

import csv
import gzip
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from aggregate_daily import aggregate_daily


FIELDS = ['source', 'project_id', 'time_utc', 'variable', 'value', 'units',
          'temporal_kind', 'valid_area_fraction', 'qc']


def hourly(variable, values, start='2025-12-31T00:00:00+00:00',
           units='degC', kind='instantaneous', source='aorc', project='A'):
    start = datetime.fromisoformat(start)
    return [dict(zip(FIELDS, [source, project, (start + timedelta(hours=i)).isoformat(),
                             variable, value, units, kind, 1., 'valid']))
            for i, value in enumerate(values)]


class DailyAggregationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.input = Path(self.folder.name) / 'hourly.csv'
        self.output = Path(self.folder.name) / 'daily.csv'

    def write(self, rows):
        with self.input.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def run_daily(self, rows, start=date(2025, 12, 31), end=date(2025, 12, 31)):
        self.write(rows)
        aggregate_daily(self.input, self.output, start, end)
        with self.output.open(newline='') as stream:
            return list(csv.DictReader(stream))

    def test_year_end_precipitation_uses_next_midnight_temperature_does_not(self):
        rows = hourly('precipitation_mm', [999.] + [2.] * 24, units='mm', kind='hour_ending_amount')
        rows += hourly('temperature_c', list(range(24)) + [999.])
        result = {r['variable']: r for r in self.run_daily(rows)}
        self.assertEqual(float(result['precipitation_mm']['value']), 48.)
        self.assertEqual(float(result['tmean_c']['value']), 11.5)
        self.assertEqual(float(result['tmin_c']['value']), 0.)
        self.assertEqual(float(result['tmax_c']['value']), 23.)
        self.assertTrue(all(r['qc'] == 'valid' and r['valid_hours'] == '24' for r in result.values()))

    def test_missing_year_end_boundary_retains_all_three_days(self):
        rows = hourly('precipitation_mm', [1.] * 71, '2025-12-29T01:00:00+00:00', 'mm', 'hour_ending_amount')
        result = self.run_daily(rows, date(2025, 12, 29), date(2025, 12, 31))
        self.assertEqual([r['date'] for r in result], ['2025-12-29', '2025-12-30', '2025-12-31'])
        self.assertEqual([r['value'] for r in result[:2]], ['24.0', '24.0'])
        self.assertEqual(result[2]['value'], '')
        self.assertEqual(result[2]['valid_hours'], '23')
        self.assertEqual(result[2]['qc'], 'missing_hours')

    def test_missing_spatial_area_nan_or_bad_qc_never_renormalizes(self):
        for changes in [{'valid_area_fraction': .75}, {'value': 'nan'}, {'qc': 'source_missing'}]:
            with self.subTest(changes=changes):
                rows = hourly('temperature_c', [10.] * 24)
                rows[12].update(changes)
                result = self.run_daily(rows)
                self.assertTrue(all(r['value'] == '' and r['valid_hours'] == '23' for r in result))
                self.assertTrue(all(r['qc'] == 'invalid_hours' for r in result))

    def test_sources_stay_separate_and_leap_day_is_explicit(self):
        rows = hourly('snow_water_equivalent_mm', [1.] * 24, '2024-02-28T00:00:00+00:00', 'mm', source='aorc')
        rows += hourly('snow_water_equivalent_mm', [8.] * 24, '2024-02-28T00:00:00+00:00', 'mm', source='era5')
        result = self.run_daily(rows, date(2024, 2, 28), date(2024, 3, 1))
        self.assertEqual(len(result), 6)
        self.assertEqual([r['value'] for r in result], ['1.0', '', '', '8.0', '', ''])
        self.assertEqual([r['date'] for r in result[:3]], ['2024-02-28', '2024-02-29', '2024-03-01'])

    def test_radiation_timing_controls_daily_mean_and_energy(self):
        for kind, expected in [('instantaneous', 0.), ('hour_ending_mean', 100.)]:
            with self.subTest(kind=kind):
                rows = hourly('shortwave_down_wm2', [0.] * 24 + [2400.], units='W/m2', kind=kind)
                result = {r['variable']: r for r in self.run_daily(rows)}
                self.assertEqual(float(result['shortwave_down_mean_wm2']['value']), expected)
                self.assertAlmostEqual(float(result['shortwave_down_energy_mjm2']['value']), expected * .0864)

    def test_duplicate_order_units_and_metadata_errors_preserve_previous_output(self):
        base = hourly('temperature_c', [1.] * 24)
        scenarios = [base + [base[-1]], list(reversed(base))]
        for changes in [{'units': 'K'}, {'time_utc': '2025-12-31T12:30:00Z'},
                        {'time_utc': '2025-12-31T12:00:00'}, {'temporal_kind': 'hour_ending_mean'},
                        {'valid_area_fraction': 'nan'}, {'valid_area_fraction': 1.1}]:
            modified = [dict(row) for row in base]
            modified[12].update(changes)
            scenarios.append(modified)
        for rows in scenarios:
            with self.subTest(rows=rows[12]):
                self.write(rows)
                self.output.write_text('previous complete output')
                with self.assertRaises(ValueError):
                    aggregate_daily(self.input, self.output, date(2025, 12, 31), date(2025, 12, 31))
                self.assertEqual(self.output.read_text(), 'previous complete output')

    def test_duplicate_outside_requested_dates_still_rejected(self):
        rows = hourly('temperature_c', [2.] * 25)
        rows.append(rows[-1])
        self.write(rows)
        with self.assertRaises(ValueError):
            aggregate_daily(self.input, self.output, date(2025, 12, 31), date(2025, 12, 31))

    def test_gzip_roundtrip_and_unavailable_year_end_hour(self):
        rows = hourly('precipitation_mm', [1.] * 24, '2025-12-31T01:00:00+00:00', 'mm', 'hour_ending_amount')
        rows[-1].update(value='', valid_area_fraction=0., qc='source_unavailable')
        compressed_input, compressed_output = self.input.with_suffix('.csv.gz'), self.output.with_suffix('.csv.gz')
        with gzip.open(compressed_input, 'wt', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        aggregate_daily(compressed_input, compressed_output, date(2025, 12, 31), date(2025, 12, 31))
        with gzip.open(compressed_output, 'rt', newline='') as stream:
            result = list(csv.DictReader(stream))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['value'], '')
        self.assertEqual(result[0]['valid_hours'], '23')
        self.assertEqual(result[0]['min_valid_area_fraction'], '0.0')

    def test_entire_thirty_year_calendar_has_requested_end_date(self):
        rows = hourly('snow_water_equivalent_mm', [1.] * 24, '2025-12-31T00:00:00+00:00', 'mm')
        result = self.run_daily(rows, date(1996, 1, 1), date(2025, 12, 31))
        self.assertEqual(len(result), 10958)
        self.assertEqual(result[0]['date'], '1996-01-01')
        self.assertEqual(result[-1]['date'], '2025-12-31')
        self.assertEqual(sum(r['date'].endswith('-02-29') for r in result), 8)
        self.assertEqual(result[-1]['value'], '1.0')
        self.assertEqual(sum(r['value'] == '' for r in result), 10957)

    def test_refuses_overwriting_input_or_reversed_dates(self):
        self.write(hourly('temperature_c', [1.] * 24))
        original = self.input.read_bytes()
        with self.assertRaises(ValueError):
            aggregate_daily(self.input, self.input, date(2025, 12, 31), date(2025, 12, 31))
        self.assertEqual(self.input.read_bytes(), original)
        with self.assertRaises(ValueError):
            aggregate_daily(self.input, self.output, date(2026, 1, 1), date(2025, 12, 31))


if __name__ == '__main__':
    unittest.main()
